"""Botões dos cartões do KANBAN (usuário, 30/09/2026; imagem radar8): ✏️ editar · 🗑️ lixeira · ➜ etapa seguinte.

  POST /api/instrumentos/editar   {processo, campos: {campo: valor}}   altera instrumentos e registra em instrumentos_edicoes
  POST /api/instrumentos/etapa    {processo, etapa} ou {processo, avancar: true}   instrumentos_etapa_manual
  POST /api/instrumentos/excluir  {processo, motivo}                     instrumentos_excluidos (o registro fica no banco)
  POST /api/instrumentos/restaurar {processo}                            tira da lixeira
  POST /api/instrumentos/novo                                          ✋ do KANBAN: instrumento EM BRANCO (CARTAO-NOVO-…) em ESTRUTURAÇÃO
  POST /api/instrumentos/novo/cancelar {processo}                      DESCARTAR na ficha do novo: apaga (só se nada foi gravado nele)
Todas as tabelas são preservadas no rebuild e as edições são reaplicadas (scripts/criar_base_completa.py).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import INSTRUMENTOS_DB
from radar_backend.radar_util import escrever_log, resposta_erro

_RE_PROC = re.compile(r"^\d{5}\.\d{6}/\d{4}-\d{2}$")      # nº de processo SEI

bp = Blueprint("instrumentos_edicao", __name__)

CAMPOS_EDITAVEIS = ["tipo_instrumento", "numero_instrumento", "numero_siafi", "localidades", "regioes", "objeto", "valor_total", "valor_atual", "contrapartida",
                    "data_assinatura_instrumento", "vigencia_mais_futura", "situacao"]
NUMERICOS = {"valor_total", "valor_atual", "contrapartida"}
CATEGORIAS = ["TED", "TC", "CONVÊNIO", "CONTRATO", "OUTROS"]      # TIPO DE INSTRUMENTO do ✏️ (usuário, 01/10/2026)
ETAPAS = ["ESTRUTURAÇÃO", "FORMALIZAÇÃO", "EXECUÇÃO", "PRESTAÇÃO DE CONTAS", "CONCLUÍDO"]
# NÃO REALIZADO (usuário, 04/10/2026): não assinado e sem empenho, só histórico — fora do ciclo (o ➜ não leva até ela)
ETAPA_NAO_REALIZADO = "NÃO REALIZADO"


def garantir_tabelas(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_edicoes (id INTEGER PRIMARY KEY AUTOINCREMENT, processo_sei TEXT, campo TEXT, valor_anterior TEXT, "
                "valor_novo TEXT, alterado_em TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_etapa_manual (processo_sei TEXT PRIMARY KEY, etapa TEXT, etapa_anterior TEXT, alterado_em TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_excluidos (processo_sei TEXT PRIMARY KEY, motivo TEXT, excluido_em TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_gestores_radar (processo_sei TEXT NOT NULL, nome TEXT NOT NULL, incluido_em TEXT, "
                "PRIMARY KEY (processo_sei, nome))")                    # GESTOR RADAR do ✏️: pessoas que acompanham o instrumento (01/10/2026)
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_situacao_prazo (processo_sei TEXT PRIMARY KEY, data_estimada TEXT, alterado_em TEXT)")
    # NÚMERO GOV (usuário, 02/10/2026): nº do instrumento no Transferegov (o "Nº do Instrumento" do DINV), ao lado do Número do instrumento
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_numero_gov (processo_sei TEXT PRIMARY KEY, numero_gov TEXT, fonte TEXT, alterado_em TEXT)")
    # NOME NO CARTÃO (usuário, 02/10/2026): título do cartão do KANBAN definido pelo usuário (ex.: "INFRAERO/ITACOATIARA-MAUÉS-FONTE_BOA"
    # no lugar de "3 AEROPORTOS"); vazio = volta ao automático (aeroporto ou "N AEROPORTOS")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_nome_cartao (processo_sei TEXT PRIMARY KEY, nome TEXT, alterado_em TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_processo_informado (processo_sei TEXT PRIMARY KEY, processo TEXT, alterado_em TEXT)")
    # CLASSIFICAÇÃO ORÇAMENTÁRIA informada na ficha (usuário, 02/10/2026): só para os campos que as fontes não localizaram
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_classificacao_manual (processo_sei TEXT PRIMARY KEY, programa_trabalho TEXT, acao TEXT, po TEXT, "
                "ptres TEXT, alterado_em TEXT)")
    # DATA ESTIMADA para cumprir a SITUAÇÃO REGISTRADA (✏️ do cartão; tarja no DETALHE — usuário, 01/10/2026)
    con.execute("CREATE TABLE IF NOT EXISTS gestores_radar_nomes (nome TEXT PRIMARY KEY, criado_em TEXT)")   # cadastro de nomes (filtro GESTOR_RADAR)
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_categoria_manual (processo_sei TEXT PRIMARY KEY, categoria TEXT NOT NULL, "
                "categoria_anterior TEXT, alterado_em TEXT)")
    # tick dos botões PLANO DE TRABALHO / METAS/ETAPAS da ficha ✏️ (usuário, 03/10/2026): linha = botão DESMARCADO (instrumento sem PT);
    # o botão fica desabilitado na ficha e oculto em todo o RADAR (DETALHE do KANBAN, MAPA…). tipo: cronograma | metas_etapas
    con.execute(DDL_PT_OCULTOS)
    # CONDIÇÃO SUSPENSIVA (usuário, 03/10/2026): cláusula + data limite (prazo vigente: o prorrogado, se houver); botão SUSPENSIVA no cartão
    con.execute(DDL_SUSPENSIVA)
    # LOG da SITUAÇÃO REGISTRADA (usuário, 03/10/2026): cada gravação/baixa no ✏️ do DETALHE; botão "log" lista; "limpar histórico" apaga.
    # Tabela própria (o rebuild reaplica instrumentos_edicoes como valor atual — apagar lá desfaria a situação). 1ª criação: semeada
    # com as edições de situação que já existiam em instrumentos_edicoes.
    novo_log = not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instrumentos_situacao_log'").fetchone()
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_situacao_log (id INTEGER PRIMARY KEY AUTOINCREMENT, processo_sei TEXT NOT NULL, "
                "situacao TEXT, data_estimada TEXT, acao TEXT, registrado_em TEXT)")
    if novo_log:
        con.execute("INSERT INTO instrumentos_situacao_log (processo_sei, situacao, data_estimada, acao, registrado_em) "
                    "SELECT processo_sei, valor_novo, NULL, CASE WHEN coalesce(valor_novo,'')='' THEN 'BAIXA' ELSE 'REGISTRO' END, alterado_em "
                    "FROM instrumentos_edicoes WHERE campo='situacao' ORDER BY id")
    cols = {r[1] for r in con.execute("PRAGMA table_info(instrumentos_suspensiva)")}
    for c in ("data_retirada", "gov"):                                  # Transferegov (SICONV): retirada da cláusula e resumo da API
        if c not in cols:
            con.execute(f"ALTER TABLE instrumentos_suspensiva ADD COLUMN {c} TEXT")


DDL_SUSPENSIVA = ("CREATE TABLE IF NOT EXISTS instrumentos_suspensiva (processo_sei TEXT PRIMARY KEY, clausula TEXT, data TEXT, "
                  "data_original TEXT, data_prorrogada TEXT, fonte TEXT, alterado_em TEXT)")


def suspensivas(con: sqlite3.Connection) -> dict:
    garantir_tabelas(con)
    return {p: {"clausula": c, "data": d, "data_original": o, "data_prorrogada": pr, "fonte": f, "data_retirada": ret, "gov": gov}
            for p, c, d, o, pr, f, ret, gov in con.execute("SELECT processo_sei, clausula, data, data_original, data_prorrogada, fonte, "
                                                            "data_retirada, gov FROM instrumentos_suspensiva")}


def suspensivas_do_transferegov(con: sqlite3.Connection) -> dict:
    """Cláusula suspensiva registrada no SICONV (transferegov_siconv_convenio: data_suspensiva = prazo, dias_clausula_suspensiva,
    data_retirada_suspensiva = cláusula retirada/cumprida) dos instrumentos do RADAR (nº do convênio = nº SIAFI). Instrumento com
    cláusula da planilha/usuário: mantém texto e data, acrescenta o resumo do Transferegov e a data de retirada. Sem cláusula: cria
    a partir do Transferegov. Usuário, 03/10/2026; roda depois de cada coleta do API-GOV."""
    garantir_tabelas(con)
    iso = lambda t: (f"{t[6:10]}-{t[3:5]}-{t[0:2]}" if re.match(r"\d{2}/\d{2}/\d{4}", str(t or "")) else (str(t)[:10] if t else None))
    br = lambda d: f"{d[8:10]}/{d[5:7]}/{d[:4]}" if d else "?"
    try:
        linhas = con.execute("SELECT trim(nr_convenio), data_suspensiva, data_retirada_suspensiva, dias_clausula_suspensiva, sit_convenio, dia_assin_conv "
                             "FROM transferegov_siconv_convenio WHERE coalesce(data_suspensiva,'')<>'' OR coalesce(data_retirada_suspensiva,'')<>''").fetchall()
    except sqlite3.Error:
        return {"criados": 0, "atualizados": 0}
    por_nr = {}
    for proc, siafi, tipo in con.execute("SELECT processo_sei, numero_siafi, tipo_instrumento FROM instrumentos"):
        for n in re.findall(r"\d{6}", f"{siafi or ''} {tipo or ''}"):
            por_nr.setdefault(n, proc)
    atual = suspensivas(con)
    agora, cri, atu = datetime.now().isoformat(timespec="seconds"), 0, 0
    for nr, dsus, dret, dias, sit, ass in linhas:
        proc = por_nr.get(nr)
        if not proc:
            continue
        dsus_i, dret_i = iso(dsus), iso(dret)
        gov = (f"Transferegov (SICONV), convênio {nr} ({sit or '—'}, assinado em {ass or '?'}): "
               + (f"cláusula suspensiva RETIRADA em {br(dret_i)}" if dret_i else f"cláusula suspensiva com prazo até {br(dsus_i)}"
                  + (f" ({dias} dias)" if dias and str(dias) not in ("0", "") else "")) + ".")
        ex = atual.get(proc)
        if ex and (ex.get("gov") == gov) and (ex.get("data_retirada") == (dret_i or ex.get("data_retirada"))):
            continue
        if ex:   # SICONV sem retirada não apaga a DATA DE RETIRADA informada pelo usuário no popup
            con.execute("UPDATE instrumentos_suspensiva SET gov=?, data_retirada=coalesce(?, data_retirada), alterado_em=? WHERE processo_sei=?",
                        (gov, dret_i, agora, proc))
            atu += 1
        else:
            con.execute("INSERT INTO instrumentos_suspensiva (processo_sei, clausula, data, data_original, data_prorrogada, fonte, alterado_em, "
                        "data_retirada, gov) VALUES (?,?,?,?,?,?,?,?,?)",
                        (proc, gov, dsus_i or dret_i, dsus_i, None, "Transferegov (SICONV) — API-GOV", agora, dret_i, gov))
            cri += 1
    con.commit()
    return {"criados": cri, "atualizados": atu}


DDL_PT_OCULTOS = ("CREATE TABLE IF NOT EXISTS instrumentos_pt_ocultos (processo_sei TEXT NOT NULL, tipo TEXT NOT NULL, alterado_em TEXT, "
                  "PRIMARY KEY (processo_sei, tipo))")
PT_TIPOS = ("cronograma", "metas_etapas")


def pt_ocultos(con: sqlite3.Connection, proc: str | None = None) -> dict:
    """{processo: [tipos desmarcados]} (ou só o do processo)."""
    con.execute(DDL_PT_OCULTOS)
    out: dict = {}
    sql, par = ("SELECT processo_sei, tipo FROM instrumentos_pt_ocultos" + (" WHERE processo_sei=?" if proc else "") + " ORDER BY tipo"), ((proc,) if proc else ())
    for p_, t_ in con.execute(sql, par):
        out.setdefault(p_, []).append(t_)
    return out


def _con():
    con = sqlite3.connect(INSTRUMENTOS_DB)
    con.row_factory = sqlite3.Row
    garantir_tabelas(con)
    return con


def editar_campo_instrumento(con: sqlite3.Connection, proc: str, campo: str, valor) -> bool:
    """Altera UM campo do instrumento como o ✏️ do cartão (instrumentos + instrumentos_edicoes, reaplicado no rebuild). Usado pelos campos
    VINCULADOS do popup do título do DETALHE (Nº do Instrumento, Término da Vigência do Instrumento — usuário, 01/10/2026)."""
    garantir_tabelas(con)
    atual = con.execute(f"SELECT {campo} FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone()
    if atual is None:
        return False
    valor = None if valor in (None, "") else str(valor).strip()
    if (atual[0] if atual[0] != "" else None) == valor:
        return False
    con.execute(f"UPDATE instrumentos SET {campo}=? WHERE processo_sei=?", (valor, proc))
    con.execute("INSERT INTO instrumentos_edicoes (processo_sei, campo, valor_anterior, valor_novo, alterado_em) VALUES (?,?,?,?,?)",
                (proc, campo, None if atual[0] is None else str(atual[0]), valor, datetime.now().isoformat(timespec="seconds")))
    return True


@bp.get("/api/instrumentos/registro")
def api_registro():
    """Registro completo do instrumento para o popup ✏️ editar (a lista manda o objeto cortado)."""
    proc = (request.args.get("processo") or "").strip()
    try:
        with _con() as con:
            r = con.execute("SELECT " + ", ".join(CAMPOS_EDITAVEIS) + " FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone()
        reg = dict(r) if r else None
        if reg is not None:
            from radar_backend.apigov import _categoria
            reg["categoria"] = _categoria(reg["tipo_instrumento"], proc)
            pz = con.execute("SELECT data_estimada FROM instrumentos_situacao_prazo WHERE processo_sei=?", (proc,)).fetchone()
            reg["situacao_data_estimada"] = pz[0] if pz else None
            ng = con.execute("SELECT numero_gov FROM instrumentos_numero_gov WHERE processo_sei=?", (proc,)).fetchone()
            reg["numero_gov"] = ng[0] if ng else None
            nc = con.execute("SELECT nome FROM instrumentos_nome_cartao WHERE processo_sei=?", (proc,)).fetchone()
            reg["nome_cartao"] = nc[0] if nc else None
            pi_ = con.execute("SELECT processo FROM instrumentos_processo_informado WHERE processo_sei=?", (proc,)).fetchone()
            reg["processo_informado"] = pi_[0] if pi_ else None
            reg["processo_editavel"] = not _RE_PROC.match(proc)          # chave sem nº SEI (SEM-PROCESSO/…, CARTAO-…): Processo editável
            reg["processos_relacionados"] = []                          # ✏️ Processos relacionados (02/10/2026): os do usuário têm ×
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='processos_relacionados'").fetchone():
                reg["processos_relacionados"] = [{"processo": x[0], "motivo": x[1], "fonte": x[2], "usuario": (x[2] or "").startswith("usuário")}
                                                 for x in con.execute("SELECT processo_relacionado, motivo, fonte FROM processos_relacionados "
                                                                      "WHERE processo_principal=? ORDER BY id", (proc,))]
            reg["pt_oculto"] = pt_ocultos(con, proc).get(proc, [])
            reg["suspensiva"] = suspensivas(con).get(proc)        # tick PLANO DE TRABALHO / METAS/ETAPAS (03/10/2026)
            reg["gestores_radar"] = [r[0] for r in con.execute("SELECT nome FROM instrumentos_gestores_radar WHERE processo_sei=? ORDER BY nome", (proc,))]
            from radar_backend.aeroportos import aeroportos_efetivos      # coordenada ÚNICA: a do 1º aeroporto (popup do título, mapa, listas)
            a0 = (aeroportos_efetivos(con, proc) or [{}])[0]
            reg["coordenadas"] = f"{float(a0['lat']):.6f}, {float(a0['lon']):.6f}" if a0.get("precisao") == "editada pelo usuário" and a0.get("lat") is not None else None
            try:                                                       # os mesmos PIs do botão PI do DETALHE (documentos ligados ao instrumento)
                from radar_backend.apigov import pis_do_instrumento
                reg["pis"] = [x["pi"] for x in pis_do_instrumento(con, proc).get("pis") or [] if x.get("pi")]
            except Exception:  # noqa: BLE001
                reg["pis"] = []
            try:                                                       # CLASSIFICAÇÃO ORÇAMENTÁRIA: Programa de Trabalho, Ação e PO (02/10/2026)
                from radar_backend.classificacao_orc import classificacao_do_instrumento
                reg["classificacao"] = classificacao_do_instrumento(con, proc, reg["pis"])
            except Exception as exc:  # noqa: BLE001
                reg["classificacao"] = {"linhas": [], "erro": str(exc)}
        return jsonify({"sucesso": r is not None, "registro": reg, "categorias": CATEGORIAS})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/editar")
def api_editar():
    d = request.get_json(silent=True) or {}
    proc, campos = (d.get("processo") or "").strip(), dict(d.get("campos") or {})
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():          # o rebuild copia as edições no início: uma edição gravada agora se perderia
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild, ~3 min). Aguarde terminar e grave de novo."}), 409
    try:
        with _con() as con:
            atual = con.execute("SELECT * FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone()
            if atual is None:
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            agora, n = datetime.now().isoformat(timespec="seconds"), 0
            rebuild_pinf = None
            _pz = con.execute("SELECT data_estimada FROM instrumentos_situacao_prazo WHERE processo_sei=?", (proc,)).fetchone()
            sit_antes = (atual["situacao"] or None, _pz[0] if _pz else None)
            cat = campos.pop("categoria", None)
            if "situacao_data_estimada" in campos:                      # AAAA-MM-DD ou vazio
                de = str(campos.pop("situacao_data_estimada") or "").strip()[:10]
                ant = con.execute("SELECT data_estimada FROM instrumentos_situacao_prazo WHERE processo_sei=?", (proc,)).fetchone()
                if (ant[0] if ant else None) != (de or None):
                    if de:
                        if not re.match(r"\d{4}-\d{2}-\d{2}$", de):
                            return jsonify({"sucesso": False, "erro": "Data estimada inválida (use DD/MM/AAAA)."}), 400
                        con.execute("INSERT OR REPLACE INTO instrumentos_situacao_prazo VALUES (?,?,?)", (proc, de, agora))
                    else:
                        con.execute("DELETE FROM instrumentos_situacao_prazo WHERE processo_sei=?", (proc,))
                    n += 1
            if "numero_gov" in campos:                                  # texto livre (ex.: 915290/2021); vazio apaga
                ng = re.sub(r"\s+", " ", str(campos.pop("numero_gov") or "")).strip() or None
                ant = con.execute("SELECT numero_gov FROM instrumentos_numero_gov WHERE processo_sei=?", (proc,)).fetchone()
                if (ant[0] if ant else None) != ng:
                    if ng:
                        con.execute("INSERT OR REPLACE INTO instrumentos_numero_gov VALUES (?,?,?,?)", (proc, ng, "informado pelo usuário", agora))
                    else:
                        con.execute("DELETE FROM instrumentos_numero_gov WHERE processo_sei=?", (proc,))
                    n += 1
            if "processo_informado" in campos:                          # PROCESSO de instrumento sem processo (usuário, 02/10/2026); vazio apaga
                pinf = re.sub(r"\s+", "", str(campos.pop("processo_informado") or "")) or None
                if pinf is not None:
                    if _RE_PROC.match(proc):
                        return jsonify({"sucesso": False, "erro": "Este instrumento já tem processo SEI (não editável)."}), 400
                    if not _RE_PROC.match(pinf):
                        return jsonify({"sucesso": False, "erro": "PROCESSO: use o formato 00000.000000/0000-00."}), 400
                    dono = con.execute("SELECT processo_sei FROM instrumentos WHERE processo_sei=? UNION SELECT processo_sei FROM "
                                       "instrumentos_processo_informado WHERE processo=? AND processo_sei<>?", (pinf, pinf, proc)).fetchone()
                    if dono:
                        return jsonify({"sucesso": False, "erro": f"O processo {pinf} já é de outro instrumento ({dono[0]})."}), 400
                ant = con.execute("SELECT processo FROM instrumentos_processo_informado WHERE processo_sei=?", (proc,)).fetchone()
                if (ant[0] if ant else None) != pinf:
                    if pinf:
                        con.execute("INSERT OR REPLACE INTO instrumentos_processo_informado VALUES (?,?,?)", (proc, pinf, agora))
                    else:
                        con.execute("DELETE FROM instrumentos_processo_informado WHERE processo_sei=?", (proc,))
                    n += 1
                    rebuild_pinf = f"processo informado {pinf or '(apagado)'} -> {proc}"   # rebuild DEPOIS do commit (abaixo)
            if "classificacao" in campos:                               # Programa de Trabalho / Ação / PO / PTRES (só os vazios nas fontes)
                cl = campos.pop("classificacao") or {}
                v = {k: re.sub(r"\s+", "", str(cl.get(k) or "")).upper() or None for k in ("programa_trabalho", "acao", "po", "ptres")}
                if v["programa_trabalho"]:
                    pt = re.sub(r"[^0-9A-Z]", "", v["programa_trabalho"])
                    if not re.fullmatch(r"\d{9}[0-9A-Z]{4}\d{4}", pt):
                        return jsonify({"sucesso": False, "erro": "PROGRAMA DE TRABALHO: use 99.999.9999.XXXX.9999 (ex.: 26.781.2017.14UB.0001)."}), 400
                    v["programa_trabalho"] = f"{pt[:2]}.{pt[2:5]}.{pt[5:9]}.{pt[9:13]}.{pt[13:]}"
                for k, rx, ex in (("acao", r"[0-9A-Z]{4}", "14UB"), ("po", r"[0-9A-Z]{1,4}", "0009"), ("ptres", r"\d{5,6}", "139866")):
                    if v[k] and not re.fullmatch(rx, v[k]):
                        return jsonify({"sucesso": False, "erro": f"{k.upper()}: valor inválido (ex.: {ex})."}), 400
                if v["po"] and v["po"].isdigit():
                    v["po"] = v["po"].zfill(4)
                ant = con.execute("SELECT programa_trabalho, acao, po, ptres FROM instrumentos_classificacao_manual WHERE processo_sei=?", (proc,)).fetchone()
                if tuple(ant or (None,) * 4) != (v["programa_trabalho"], v["acao"], v["po"], v["ptres"]):
                    if any(v.values()):
                        con.execute("INSERT OR REPLACE INTO instrumentos_classificacao_manual VALUES (?,?,?,?,?,?)",
                                    (proc, v["programa_trabalho"], v["acao"], v["po"], v["ptres"], agora))
                    else:
                        con.execute("DELETE FROM instrumentos_classificacao_manual WHERE processo_sei=?", (proc,))
                    n += 1
            if "nome_cartao" in campos:                                 # título do cartão; vazio apaga (volta ao automático)
                nc = re.sub(r"\s+", " ", str(campos.pop("nome_cartao") or "")).strip() or None
                ant = con.execute("SELECT nome FROM instrumentos_nome_cartao WHERE processo_sei=?", (proc,)).fetchone()
                if (ant[0] if ant else None) != nc:
                    if nc:
                        con.execute("INSERT OR REPLACE INTO instrumentos_nome_cartao VALUES (?,?,?)", (proc, nc, agora))
                    else:
                        con.execute("DELETE FROM instrumentos_nome_cartao WHERE processo_sei=?", (proc,))
                    n += 1
            if "pt_oculto" in campos:                                   # tipos DESMARCADOS no tick da ficha; [] = os dois marcados
                novos = {str(x) for x in (campos.pop("pt_oculto") or []) if str(x) in PT_TIPOS}
                antes = set(pt_ocultos(con, proc).get(proc, []))
                if novos != antes:
                    con.execute("DELETE FROM instrumentos_pt_ocultos WHERE processo_sei=?", (proc,))
                    con.executemany("INSERT INTO instrumentos_pt_ocultos VALUES (?,?,?)", [(proc, t, agora) for t in sorted(novos)])
                    n += 1
            if "gestores_radar" in campos:                              # lista de nomes (MAIÚSCULAS, sem repetir); substitui a anterior
                nomes = sorted({re.sub(r"\s+", " ", str(x)).strip().upper() for x in (campos.pop("gestores_radar") or []) if str(x).strip()})
                antes = {r[0] for r in con.execute("SELECT nome FROM instrumentos_gestores_radar WHERE processo_sei=?", (proc,))}
                if set(nomes) != antes:
                    con.execute("DELETE FROM instrumentos_gestores_radar WHERE processo_sei=?", (proc,))
                    con.executemany("INSERT INTO instrumentos_gestores_radar VALUES (?,?,?)", [(proc, x, agora) for x in nomes])
                    con.executemany("INSERT OR IGNORE INTO gestores_radar_nomes VALUES (?,?)", [(x, agora) for x in nomes])
                    n += 1
            if "coordenadas" in campos:                                 # "lat, lon" em graus decimais; vazio = volta à coordenada do aeródromo
                # mesma coordenada do DETALHE do aeroporto (popup do título), do MAPA e das listas: aeroportos_edicoes do 1º aeroporto
                from radar_backend.aeroportos import DDL_EDICOES, aeroportos_efetivos
                con.execute(DDL_EDICOES)
                txt = str(campos.pop("coordenadas") or "").strip()
                a0 = (aeroportos_efetivos(con, proc) or [{}])[0]
                chave = a0.get("chave_edicao")
                if chave and not txt:
                    n += con.execute("DELETE FROM aeroportos_edicoes WHERE processo_sei=? AND chave=? AND campo IN ('lat','lon')", (proc, chave)).rowcount
                elif chave:
                    nums = [float(x.replace(",", ".")) for x in re.findall(r"-?\d+(?:[.,]\d+)?", txt.replace(", ", "; "))]
                    if len(nums) != 2 or not (-35 <= nums[0] <= 6 and -75 <= nums[1] <= -28):
                        return jsonify({"sucesso": False, "erro": "COORDENADAS: informe latitude, longitude em graus decimais (ex.: -4.0858, -63.1408)."}), 400
                    for campo_, v_ in (("lat", nums[0]), ("lon", nums[1])):
                        con.execute("INSERT OR REPLACE INTO aeroportos_edicoes VALUES (?,?,?,?,?,?)", (proc, chave, campo_, str(v_), None, agora))
                    n += 1
            if cat in CATEGORIAS:
                from radar_backend.apigov import _categoria
                ant = _categoria(atual["tipo_instrumento"], proc)
                if cat != ant:
                    con.execute("INSERT OR REPLACE INTO instrumentos_categoria_manual VALUES (?,?,?,?)", (proc, cat, ant, agora))
                    n += 1
            # VALOR TOTAL não é editável: é o próprio Valor atual, que JÁ INCLUI a contrapartida (decisão do usuário, 01/10/2026);
            # a União (Valor atual − Contrapartida) só é mostrada no popup, como em i.uniao da lista
            if "valor_atual" in campos:
                num = lambda x: None if x in (None, "") else float(str(x).replace(".", "").replace(",", ".")) if isinstance(x, str) and "," in x else float(x)
                campos["valor_total"] = num(campos["valor_atual"])
            for campo, valor in campos.items():
                if campo not in CAMPOS_EDITAVEIS:
                    continue
                if campo in NUMERICOS:
                    valor = None if valor in (None, "") else float(str(valor).replace(".", "").replace(",", ".")) if isinstance(valor, str) and "," in valor else (None if valor in (None, "") else float(valor))
                else:
                    valor = None if valor in (None, "") else str(valor).strip()
                if (atual[campo] if atual[campo] != "" else None) == valor:
                    continue
                if campo == "objeto" and valor and atual[campo] and str(atual[campo]).startswith(valor):
                    continue                                   # objeto cortado (a lista manda só 240 caracteres): não sobrescreve o texto completo
                con.execute(f"UPDATE instrumentos SET {campo}=? WHERE processo_sei=?", (valor, proc))
                con.execute("INSERT INTO instrumentos_edicoes (processo_sei, campo, valor_anterior, valor_novo, alterado_em) VALUES (?,?,?,?,?)",
                            (proc, campo, None if atual[campo] is None else str(atual[campo]), None if valor is None else str(valor), agora))
                n += 1
                # objeto num quadro só "1. <DINV/COMARA>\n2. <RADAR>" (usuário, 02/10/2026): a fonte continua com o texto dela (não propaga)
                if campo == "objeto" and valor and not re.match(r"\s*1\.\s", valor):  # vínculo: nova edição do OBJETO vale também no popup do título (01/10/2026)
                    from radar_backend.aeroportos import propagar_objeto
                    propagar_objeto(con, proc, valor)
            if d.get("etapa") in ETAPAS or d.get("etapa") == ETAPA_NAO_REALIZADO:
                con.execute("INSERT OR REPLACE INTO instrumentos_etapa_manual VALUES (?,?,?,?)", (proc, d["etapa"], d.get("etapa_anterior"), agora))
            _s = con.execute("SELECT situacao FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone()
            _pz = con.execute("SELECT data_estimada FROM instrumentos_situacao_prazo WHERE processo_sei=?", (proc,)).fetchone()
            sit_depois = ((_s[0] if _s else None) or None, _pz[0] if _pz else None)
            if sit_depois != sit_antes:                                 # LOG da SITUAÇÃO REGISTRADA
                acao = "BAIXA" if not any(sit_depois) else "REGISTRO"
                reg = sit_antes if acao == "BAIXA" else sit_depois     # baixa: guarda o que foi baixado
                con.execute("INSERT INTO instrumentos_situacao_log (processo_sei, situacao, data_estimada, acao, registrado_em) VALUES (?,?,?,?,?)",
                            (proc, reg[0], reg[1], acao, agora))
        if rebuild_pinf:                                               # NE/OB/NC que citam o processo informado entram no instrumento
            from radar_backend.arquivos_db import iniciar_rebuild
            iniciar_rebuild(rebuild_pinf)
        return jsonify({"sucesso": True, "alterados": n, "rebuild": bool(rebuild_pinf)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/situacao-log")
def api_situacao_log():
    proc = (request.args.get("processo") or "").strip()
    try:
        with _con() as con:
            garantir_tabelas(con)
            linhas = [{"situacao": s_, "data_estimada": de, "acao": a, "registrado_em": r} for s_, de, a, r in con.execute(
                "SELECT situacao, data_estimada, acao, registrado_em FROM instrumentos_situacao_log WHERE processo_sei=? ORDER BY id DESC", (proc,))]
        return jsonify({"sucesso": True, "log": linhas})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/situacao-log/limpar")
def api_situacao_log_limpar():
    """Limpar histórico: apaga TODO o log da situação registrada do instrumento (a situação atual não muda)."""
    proc = ((request.get_json(silent=True) or {}).get("processo") or "").strip()
    try:
        with _con() as con:
            garantir_tabelas(con)
            n = con.execute("DELETE FROM instrumentos_situacao_log WHERE processo_sei=?", (proc,)).rowcount
        escrever_log(f"SITUAÇÃO | histórico limpo: {proc} ({n} registro(s))")
        return jsonify({"sucesso": True, "apagados": n})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/suspensiva")
def api_suspensiva():
    """{processo, clausula, data (AAAA-MM-DD)}: grava a condição suspensiva do instrumento; cláusula e data vazias apagam."""
    d = request.get_json(silent=True) or {}
    proc = (d.get("processo") or "").strip()
    clausula = re.sub(r"[ \t]+", " ", str(d.get("clausula") or "")).strip()
    data = str(d.get("data") or "").strip()[:10] or None
    if data and not re.match(r"\d{4}-\d{2}-\d{2}$", data):
        return jsonify({"sucesso": False, "erro": "Data inválida (use o calendário ou DD/MM/AAAA)."}), 400
    tem_ret = "data_retirada" in d   # DATA DE RETIRADA editável no popup (usuário, 03/10/2026); ausente = mantém a gravada
    data_ret = str(d.get("data_retirada") or "").strip()[:10] or None
    if data_ret and not re.match(r"\d{4}-\d{2}-\d{2}$", data_ret):
        return jsonify({"sucesso": False, "erro": "Data de retirada inválida."}), 400
    try:
        with _con() as con:
            if not con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone():
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            if not clausula and not data:
                con.execute("DELETE FROM instrumentos_suspensiva WHERE processo_sei=?", (proc,))
            else:
                ant = con.execute("SELECT data_original, data_prorrogada, fonte, data_retirada, gov FROM instrumentos_suspensiva WHERE processo_sei=?", (proc,)).fetchone()
                fonte = (ant[2] if ant and ant[2] else "")
                if "editado pelo usuário" not in fonte:
                    fonte = (fonte + "; " if fonte else "") + "editado pelo usuário"
                # colunas nomeadas (a tabela ganhou data_retirada e gov — o INSERT posicional de 7 valores quebrava); retirada/gov preservados
                con.execute("INSERT OR REPLACE INTO instrumentos_suspensiva (processo_sei, clausula, data, data_original, data_prorrogada, fonte, "
                            "alterado_em, data_retirada, gov) VALUES (?,?,?,?,?,?,?,?,?)",
                            (proc, clausula, data, ant[0] if ant else None, ant[1] if ant else None, fonte, datetime.now().isoformat(timespec="seconds"),
                             data_ret if tem_ret else (ant[3] if ant else None), ant[4] if ant else None))
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/etapa")
def api_etapa():
    d = request.get_json(silent=True) or {}
    proc = (d.get("processo") or "").strip()
    try:
        nova = d.get("etapa")
        if d.get("avancar"):
            atual = d.get("etapa_atual")
            if atual not in ETAPAS or atual == ETAPAS[-1]:
                return jsonify({"sucesso": False, "erro": "Não há etapa seguinte."}), 400
            nova = ETAPAS[ETAPAS.index(atual) + 1]
        if nova not in ETAPAS and nova != ETAPA_NAO_REALIZADO:
            return jsonify({"sucesso": False, "erro": "Etapa inválida."}), 400
        with _con() as con:
            con.execute("INSERT OR REPLACE INTO instrumentos_etapa_manual VALUES (?,?,?,?)",
                        (proc, nova, d.get("etapa_atual"), datetime.now().isoformat(timespec="seconds")))
        return jsonify({"sucesso": True, "etapa": nova})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/excluir")
def api_excluir():
    d = request.get_json(silent=True) or {}
    try:
        with _con() as con:
            con.execute("INSERT OR REPLACE INTO instrumentos_excluidos VALUES (?,?,?)",
                        ((d.get("processo") or "").strip(), d.get("motivo") or "lixeira do cartão", datetime.now().isoformat(timespec="seconds")))
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/restaurar")
def api_restaurar():
    d = request.get_json(silent=True) or {}
    try:
        with _con() as con:
            con.execute("DELETE FROM instrumentos_excluidos WHERE processo_sei=?", ((d.get("processo") or "").strip(),))
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- GESTOR RADAR: cadastro de nomes (usuário, 01/10/2026)
# O filtro GESTOR_RADAR permite acrescentar um nome; ele fica em gestores_radar_nomes (preservada no rebuild) e aparece, junto com os
# nomes já usados nos instrumentos, na lista do filtro e no campo GESTOR RADAR do ✏️ de qualquer instrumento.
PREFIXO_NOVO = "CARTAO-NOVO-"


@bp.post("/api/instrumentos/novo")
def api_novo():
    """✋ ao lado do Mapa (usuário, 03/10/2026): cria um instrumento em branco (chave CARTAO-NOVO-AAAAMMDDHHMMSS, sem processo SEI — o
    Processo fica editável na ficha), na etapa ESTRUTURAÇÃO. Gravado também em instrumentos_estruturacao (sobrevive ao rebuild); os campos
    preenchidos na ficha entram por /api/instrumentos/editar (instrumentos_edicoes, reaplicadas no rebuild)."""
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild, ~3 min). Aguarde terminar e tente de novo."}), 409
    try:
        with _con() as con:
            agora = datetime.now()
            proc = PREFIXO_NOVO + agora.strftime("%Y%m%d%H%M%S")
            while con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone():
                proc += "X"
            cols = [r[1] for r in con.execute("PRAGMA table_info(instrumentos)")]
            reg = {c: None for c in cols}
            reg["processo_sei"] = proc
            con.execute(f"INSERT INTO instrumentos ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [reg[c] for c in cols])
            con.execute("CREATE TABLE IF NOT EXISTS instrumentos_estruturacao (processo_sei TEXT PRIMARY KEY, registro_json TEXT, criado_em TEXT, motivo TEXT)")
            con.execute("INSERT OR REPLACE INTO instrumentos_estruturacao VALUES (?,?,?,?)",
                        (proc, json.dumps(reg, ensure_ascii=False), agora.isoformat(timespec="seconds"), "novo instrumento criado na tela (✋ do KANBAN)"))
            con.execute("INSERT OR REPLACE INTO instrumentos_etapa_manual VALUES (?,?,?,?)", (proc, "ESTRUTURAÇÃO", None, agora.isoformat(timespec="seconds")))
        escrever_log(f"Novo instrumento em branco criado: {proc}")
        return jsonify({"sucesso": True, "processo": proc})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/novo/cancelar")
def api_novo_cancelar():
    """Apaga o instrumento em branco do ✋ quando a ficha é DESCARTADA sem nada gravado (com edições gravadas, fica — use a lixeira)."""
    proc = ((request.get_json(silent=True) or {}).get("processo") or "").strip()
    if not proc.startswith(PREFIXO_NOVO):
        return jsonify({"sucesso": False, "erro": "Só instrumentos novos (em branco) podem ser apagados assim."}), 400
    try:
        with _con() as con:
            if con.execute("SELECT 1 FROM instrumentos_edicoes WHERE processo_sei=? LIMIT 1", (proc,)).fetchone():
                return jsonify({"sucesso": True, "apagado": False})
            for t in ("instrumentos", "instrumentos_estruturacao", "instrumentos_etapa_manual"):
                con.execute(f"DELETE FROM {t} WHERE processo_sei=?", (proc,))
        escrever_log(f"Novo instrumento em branco descartado: {proc}")
        return jsonify({"sucesso": True, "apagado": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _nome_gestor(x) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip().upper()


@bp.get("/api/gestores-radar")
def api_gestores_radar():
    try:
        with _con() as con:
            n = {r[0]: 0 for r in con.execute("SELECT nome FROM gestores_radar_nomes")}
            for nome, q in con.execute("SELECT nome, COUNT(*) FROM instrumentos_gestores_radar GROUP BY nome"):
                n[nome] = q
        return jsonify({"sucesso": True, "nomes": [{"nome": k, "instrumentos": v} for k, v in sorted(n.items())]})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/gestores-radar")
def api_gestores_radar_adicionar():
    nome = _nome_gestor((request.get_json(silent=True) or {}).get("nome"))
    if not nome:
        return jsonify({"sucesso": False, "erro": "Informe o nome."}), 400
    try:
        with _con() as con:
            novo = con.execute("INSERT OR IGNORE INTO gestores_radar_nomes VALUES (?,?)", (nome, datetime.now().isoformat(timespec="seconds"))).rowcount
        return jsonify({"sucesso": True, "nome": nome, "novo": bool(novo)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/gestores-radar/excluir")
def api_gestores_radar_excluir():
    """🗑 do filtro GESTOR_RADAR (usuário, 01/10/2026): tira o nome de TODOS os instrumentos e do cadastro de nomes."""
    nome = _nome_gestor((request.get_json(silent=True) or {}).get("nome"))
    if not nome:
        return jsonify({"sucesso": False, "erro": "Informe o nome."}), 400
    try:
        with _con() as con:
            procs = [r[0] for r in con.execute("SELECT processo_sei FROM instrumentos_gestores_radar WHERE nome=?", (nome,))]
            con.execute("DELETE FROM instrumentos_gestores_radar WHERE nome=?", (nome,))
            con.execute("DELETE FROM gestores_radar_nomes WHERE nome=?", (nome,))
        return jsonify({"sucesso": True, "nome": nome, "instrumentos": procs})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- PROCESSOS RELACIONADOS (usuário, 02/10/2026)
# A ficha (✏️) permite vincular/desvincular processos a um instrumento. Gravados em processos_relacionados_usuario (preservada no rebuild) e
# já na processos_relacionados (a ficha mostra na hora); o financeiro (NE/OB/NC citando o processo) entra no próximo rebuild, disparado aqui.
def _proc_rel_ddl(con):
    con.execute("CREATE TABLE IF NOT EXISTS processos_relacionados_usuario (processo_principal TEXT NOT NULL, processo_relacionado TEXT NOT NULL, "
                "motivo TEXT, alterado_em TEXT, PRIMARY KEY (processo_principal, processo_relacionado))")


@bp.post("/api/instrumentos/processos/vincular")
def api_processo_vincular():
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar para vincular/desvincular processos."}), 409
    d = request.get_json(silent=True) or {}
    proc, rel = (d.get("processo") or "").strip(), (d.get("relacionado") or "").strip()
    motivo = (d.get("motivo") or "").strip() or "vinculado pelo usuário na ficha"
    if not _RE_PROC.match(rel):
        return jsonify({"sucesso": False, "erro": "Processo no formato 00000.000000/0000-00."}), 400
    if rel == proc:
        return jsonify({"sucesso": False, "erro": "É o próprio processo do instrumento."}), 400
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        if con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone() is None:
            con.close(); return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
        outro = con.execute("SELECT processo_sei FROM instrumentos WHERE processo_sei=?", (rel,)).fetchone()
        if outro:
            con.close(); return jsonify({"sucesso": False, "erro": f"{rel} já é o processo de outro instrumento."}), 400
        _proc_rel_ddl(con)
        agora = datetime.now().isoformat(timespec="seconds")
        con.execute("INSERT OR REPLACE INTO processos_relacionados_usuario VALUES (?,?,?,?)", (proc, rel, motivo, agora))
        if not con.execute("SELECT 1 FROM processos_relacionados WHERE processo_principal=? AND processo_relacionado=?", (proc, rel)).fetchone():
            con.execute("INSERT INTO processos_relacionados (processo_principal, processo_relacionado, motivo, fonte, coletado_em) VALUES (?,?,?,?,?)",
                        (proc, rel, motivo, "usuário (ficha do instrumento)", agora))
        con.commit(); con.close()
        from radar_backend.arquivos_db import iniciar_rebuild
        iniciar_rebuild(f"processo relacionado {rel} -> {proc}")
        return jsonify({"sucesso": True, "rebuild": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/processos/vincular-lista")
def api_processo_vincular_lista():
    """＋ Vincular da ficha (usuário, 03/10/2026): vários processos de uma vez, gravados no SALVAR; quadros vazios já vêm descartados.
    {processo, relacionados: [...]} — valida todos antes de gravar e dispara UM rebuild."""
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar para vincular processos."}), 409
    d = request.get_json(silent=True) or {}
    proc = (d.get("processo") or "").strip()
    rels = list(dict.fromkeys(str(x).strip() for x in (d.get("relacionados") or []) if str(x).strip()))
    if not rels:
        return jsonify({"sucesso": True, "vinculados": 0})
    for rel in rels:
        if not _RE_PROC.match(rel):
            return jsonify({"sucesso": False, "erro": f"{rel}: use o formato 00000.000000/0000-00."}), 400
        if rel == proc:
            return jsonify({"sucesso": False, "erro": f"{rel} é o próprio processo do instrumento."}), 400
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        if con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone() is None:
            con.close(); return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
        for rel in rels:
            if con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (rel,)).fetchone():
                con.close(); return jsonify({"sucesso": False, "erro": f"{rel} já é o processo de outro instrumento."}), 400
        _proc_rel_ddl(con)
        agora, motivo = datetime.now().isoformat(timespec="seconds"), "vinculado pelo usuário na ficha"
        for rel in rels:
            con.execute("INSERT OR REPLACE INTO processos_relacionados_usuario VALUES (?,?,?,?)", (proc, rel, motivo, agora))
            if not con.execute("SELECT 1 FROM processos_relacionados WHERE processo_principal=? AND processo_relacionado=?", (proc, rel)).fetchone():
                con.execute("INSERT INTO processos_relacionados (processo_principal, processo_relacionado, motivo, fonte, coletado_em) VALUES (?,?,?,?,?)",
                            (proc, rel, motivo, "usuário (ficha do instrumento)", agora))
        con.commit(); con.close()
        from radar_backend.arquivos_db import iniciar_rebuild
        iniciar_rebuild(f"processos relacionados {', '.join(rels)} -> {proc}")
        return jsonify({"sucesso": True, "vinculados": len(rels), "rebuild": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/processos/desvincular")
def api_processo_desvincular():
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar para vincular/desvincular processos."}), 409
    d = request.get_json(silent=True) or {}
    proc, rel = (d.get("processo") or "").strip(), (d.get("relacionado") or "").strip()
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        _proc_rel_ddl(con)
        n = con.execute("DELETE FROM processos_relacionados_usuario WHERE processo_principal=? AND processo_relacionado=?", (proc, rel)).rowcount
        con.execute("DELETE FROM processos_relacionados WHERE processo_principal=? AND processo_relacionado=? AND fonte LIKE 'usuário%'", (proc, rel))
        con.commit(); con.close()
        if not n:
            return jsonify({"sucesso": False, "erro": "Só os processos vinculados na ficha podem ser desvinculados aqui (os do script ficam em PROCESSOS_RELACIONADOS)."}), 400
        from radar_backend.arquivos_db import iniciar_rebuild
        iniciar_rebuild(f"processo relacionado {rel} desvinculado de {proc}")
        return jsonify({"sucesso": True, "rebuild": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def processos_do_instrumento(con: sqlite3.Connection, proc: str) -> list[dict]:
    """Processos a que um documento SEI do instrumento pode pertencer: o principal (nº SEI da chave ou o informado na ficha) e os
    RELACIONADOS (usuário, 02/10/2026)."""
    out = []
    principal = proc if _RE_PROC.match(proc) else None
    try:
        r = con.execute("SELECT processo FROM instrumentos_processo_informado WHERE processo_sei=?", (proc,)).fetchone()
        principal = principal or (r[0] if r else None)
    except sqlite3.Error:
        pass
    if principal:
        out.append({"processo": principal, "papel": "principal"})
    try:
        for (rel,) in con.execute("SELECT DISTINCT processo_relacionado FROM processos_relacionados WHERE processo_principal=? ORDER BY id", (proc,)):
            if rel and rel not in [x["processo"] for x in out]:
                out.append({"processo": rel, "papel": "relacionado"})
    except sqlite3.Error:
        pass
    return out


@bp.get("/api/instrumentos/processos-documento")
def api_processos_documento():
    proc = (request.args.get("processo") or "").strip()
    try:
        with _con() as con:
            return jsonify({"sucesso": True, "processos": processos_do_instrumento(con, proc)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
