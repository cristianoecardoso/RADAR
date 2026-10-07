"""Aeroportos dos instrumentos e mapa-base (pedido do usuário, 30/09/2026).

- etapa do instrumento: CONCLUÍDO (encerrado/rescindido) · PREPARATÓRIO (sem assinatura do instrumento inicial) · EM EXECUÇÃO (demais;
  vigência vencida continua EM EXECUÇÃO, com o aviso)
- aeroportos de cada instrumento, com a 1ª linha "AEROPORTO / UF - ICAO": Base de Dados - COMARA (comara_empreendimentos) > DINV
  (EMPREENDIMENTOS, pelo processo SEI) > localidade/UF do cadastro; coordenadas: ANAC (aerodromos_anac) > complemento (militares,
  aproximado) > DINV
- GET /api/mapa/base: malhas do IBGE (estados, países da América do Sul, capitais) gravadas em mapa_base (scripts/baixar_mapa_ibge.py)
"""
from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache

from flask import Blueprint, jsonify

from radar_backend.radar_config import DINV_DB, INSTRUMENTOS_DB
from radar_backend.radar_util import resposta_erro

bp = Blueprint("aeroportos", __name__)


ETAPAS = ["ESTRUTURAÇÃO", "FORMALIZAÇÃO", "EXECUÇÃO", "PRESTAÇÃO DE CONTAS", "CONCLUÍDO"]   # ciclo do instrumento (usuário, 30/09/2026; radar8)


def etapa(situacao: str | None, assinatura: str | None, vigencia: str | None = None, hoje: str | None = None, ultimo_ano_mov: int | None = None) -> str:
    """ESTRUTURAÇÃO (cartão do usuário) · FORMALIZAÇÃO (sem assinatura do instrumento inicial) · EXECUÇÃO · PRESTAÇÃO DE CONTAS (assinado, vigência
    vencida e não encerrado) · CONCLUÍDO (encerrado/rescindido). A etapa definida pelo usuário (botão ➜ / editar) prevalece — instrumentos_etapa_manual."""
    s = (situacao or "").strip().upper()
    if s.startswith("ESTRUTURA"):
        return "ESTRUTURAÇÃO"
    if s.startswith("ENCERRADO") or s == "RESCINDIDO":
        return "CONCLUÍDO"
    if not assinatura:
        # sem data de assinatura, mas com execução financeira (empenho/OB/PF) — aportes, indenizações, TEDs só com nº SIAFI (usuário, 30/09/2026):
        # movimento nos últimos 3 anos = EXECUÇÃO; movimento só antigo = CONCLUÍDO; sem movimento (ou em contratação) = FORMALIZAÇÃO
        if ultimo_ano_mov and not s.startswith("EM CONTRATA") and hoje:
            return "EXECUÇÃO" if ultimo_ano_mov >= int(hoje[:4]) - 2 else "CONCLUÍDO"
        return "FORMALIZAÇÃO"
    if vigencia and hoje and str(vigencia)[:10] < hoje:
        return "PRESTAÇÃO DE CONTAS"
    return "EXECUÇÃO"


def _etapa_comara(sit: str | None, etapa_inst: str) -> str:
    s = (sit or "").upper()
    if "CANCEL" in s:
        return "CANCELADO"
    if "CONCLU" in s:
        return "CONCLUÍDO"
    if "EXECU" in s:
        return "EXECUÇÃO"
    return etapa_inst


def rotulo(aeroporto: str | None, uf: str | None, icao: str | None) -> str:
    a = (aeroporto or "").strip()
    u = (uf or "").strip()
    i = (icao or "").strip().upper()
    txt = a.upper()
    if u and u != "BR":
        txt += f" / {u}"
    if i and i != "BRASIL":
        txt += f" - {i}"
    return txt


def mapas_de_apoio():
    """(coordenadas por ICAO, empreendimentos DINV por dígitos do processo) — lidos uma vez por chamada da lista."""
    with sqlite3.connect(INSTRUMENTOS_DB) as con:
        coords = {}
        tem = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "aerodromos_anac" in tem:
            for icao, nome, mun, uf, la, lo, tipo in con.execute("SELECT icao, nome, municipio, uf, latitude, longitude, tipo FROM aerodromos_anac"):
                coords[icao] = {"lat": la, "lon": lo, "nome": nome, "municipio": mun, "uf": uf, "precisao": "ANAC", "publico": tipo == "publico"}
        if "aerodromos_complemento" in tem:
            for icao, mun, uf, la, lo, prec in con.execute("SELECT icao, municipio, uf, latitude, longitude, precisao FROM aerodromos_complemento"):
                coords[icao] = {"lat": la, "lon": lo, "nome": None, "municipio": mun, "uf": uf, "precisao": prec}
        comara = {}
        if "comara_empreendimentos" in tem:
            con.row_factory = sqlite3.Row
            for r in con.execute("SELECT * FROM comara_empreendimentos WHERE processo_sei IS NOT NULL ORDER BY id"):
                comara.setdefault(r["processo_sei"], []).append(dict(r))
    dinv = {}
    try:
        with sqlite3.connect(DINV_DB) as d:
            for (j,) in d.execute("SELECT dados_json FROM dinv_registros WHERE aba IN ('EMPREENDIMENTOS','EMPREENDIMENTOS INFRAERO')"):
                r = json.loads(j)
                p = "".join(ch for ch in str(r.get("Processo SEI") or "") if ch.isdigit())
                if p:
                    dinv.setdefault(p, []).append(r)
    except sqlite3.Error:
        pass
    return coords, comara, dinv


def _norm(t: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower().strip()


def apoio_por_nome(coords: dict) -> dict:
    """município -> ICAOs de aeródromos PÚBLICOS da ANAC (para completar ICAO ausente na COMARA)."""
    out = {}
    for icao, k in coords.items():
        if k.get("precisao") == "ANAC" and k.get("municipio") and k.get("publico"):
            out.setdefault(_norm(k["municipio"]), []).append(icao)
    return out


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def aeroportos_do_instrumento(inst: dict, et: str, apoio) -> list[dict]:
    coords, comara, dinv = apoio
    out = []
    por_nome = apoio_por_nome(coords)
    for c in comara.get(inst["processo_sei"], []):
        icao = (c["icao"] or "").strip().upper()
        uf_c = (c["uf"] or "").strip()
        if not icao and c["aeroporto"]:                  # sem ICAO na COMARA (ex.: Breves, TED 002/2017): aeródromo público da ANAC de mesmo município
            cand = por_nome.get(_norm(str(c["aeroporto"]).replace("Projetos - ", "")), [])
            if len(cand) == 1:
                icao, uf_c = cand[0], coords[cand[0]]["uf"] or uf_c
        k = coords.get(icao, {})
        nome = (c["aeroporto"] or "").replace("TED NAV/2017 - ", "").replace("TED 977360/2025 - ", "")
        nome = nome.split("/")[0].strip() if "/" in nome and len(nome.split("/")[-1].strip()) == 2 else nome
        # todos os campos da Base de Dados - COMARA para o DETALHE do aeroporto (usuário, 30/09/2026)
        det = {k_: c[k_] for k_ in ("instrumento", "instrumento_numero", "aeroporto", "uf", "icao", "objeto", "valor_uniao", "contrapartida",
                                    "valor_total_instrumento", "data_inicio", "previsao_conclusao", "situacao_macro", "etapa_atual", "percentual_fisico",
                                    "tipo_objeto", "entregaveis", "informacoes", "pac", "nome_pac", "valor_pac", "cipi", "metodo_vinculo")}
        out.append({"aeroporto": nome, "uf": uf_c, "icao": icao, "linha": rotulo(nome, uf_c, icao),
                    "lat": k.get("lat"), "lon": k.get("lon"), "precisao": k.get("precisao"), "etapa": _etapa_comara(c["situacao_macro"], et),
                    "objeto": c["objeto"], "valor_uniao": c["valor_uniao"], "fonte": "COMARA", "comara": det,
                    "anac": {"nome": k.get("nome"), "municipio": k.get("municipio"), "uf": k.get("uf")} if k else None})
    if out:
        return out
    for r in dinv.get("".join(ch for ch in inst["processo_sei"] if ch.isdigit()), []):
        icao = (r.get("ICAO") or "").strip().upper()
        k = coords.get(icao, {})
        nome = r.get("Município") or str(r.get("Empreendimento") or "").replace("Aeroporto - ", "").split("/")[0]
        la, lo = k.get("lat") or _num(r.get("LATITUDE")), k.get("lon") or _num(r.get("LONGITUDE"))
        if any(o["icao"] == icao for o in out):
            continue
        campos_dinv = ("Empreendimento", "Título na Ficha\n(Resumo do Objeto)", "Objeto do Instrumento", "Município", "Estado", "Tipo do Instrumento", "CIPI",
                       "Etapa Atual", "Situação Macro", "Atualização", "Previsão de Início das Obras", "Previsão de Término das Obras", "Nº do Instrumento",
                       "Unidade Executora", "Data da Assinatura do Instrumento", "Término da Vigência do Instrumento", "Ganho Esperado")
        out.append({"aeroporto": nome, "uf": r.get("Estado") or inst.get("regioes"), "icao": icao, "linha": rotulo(nome, r.get("Estado"), icao),
                    "lat": la, "lon": lo, "precisao": k.get("precisao") or "DINV", "etapa": et, "objeto": r.get("Objeto do Instrumento"),
                    "valor_uniao": None, "fonte": "DINV", "dinv": {c_.replace("\n", " "): r.get(c_) for c_ in campos_dinv if r.get(c_) not in (None, "")},
                    "anac": {"nome": k.get("nome"), "municipio": k.get("municipio"), "uf": k.get("uf")} if k else None})
    if out:
        return out
    loc = (inst.get("localidades") or "").split("|")[0].strip()
    uf = (inst.get("regioes") or "").split(",")[0].strip()
    nome = loc.split("/")[0].split("-")[0].strip() if loc else ""
    import re as _re
    m = _re.search(r"\(([A-Z][A-Z0-9]{3})\)", loc)       # ICAO informado no cadastro, ex.: "Santa Magalhães / Serra Talhada/PE (SNHS)"
    icao = m.group(1) if m else ""
    if not nome and not icao:                               # instrumento em branco (✋ novo, 03/10/2026): sem aeroporto "—" fantasma
        return []
    k = coords.get(icao, {})
    return [{"aeroporto": nome, "uf": uf, "icao": icao, "linha": rotulo(nome, uf, icao), "lat": k.get("lat"), "lon": k.get("lon"),
             "precisao": k.get("precisao"), "etapa": et, "objeto": inst.get("objeto"), "valor_uniao": None, "fonte": "cadastro",
             "anac": {"nome": k.get("nome"), "municipio": k.get("municipio"), "uf": k.get("uf")} if k else None}]


@bp.get("/api/mapa/base")
def api_mapa_base():
    try:
        with sqlite3.connect(INSTRUMENTOS_DB) as con:
            camadas = {c: json.loads(g) for c, g in con.execute("SELECT camada, geojson FROM mapa_base")}
        return jsonify({"sucesso": True, **camadas, "fonte": "IBGE — malhas (estados) e Base Cartográfica Contínua 1:250.000 2025 (países, capitais)"})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- edição do DETALHE do aeroporto (usuário, 01/10/2026)
# No popup aberto pelo nome do instrumento (título do DETALHE), ✏️ habilita a edição de todos os campos; SALVAR grava, DESCARTAR volta.
# As fontes (COMARA, DINV, cadastro, ANAC) são recarregadas no rebuild, então a edição fica em aeroportos_edicoes (preservada) e é
# aplicada POR CIMA da fonte em aplicar_edicoes() — vale para o popup, o título do DETALHE, as listas e o MAPA.
#   chave do aeroporto: ICAO; sem ICAO, o nome do aeroporto na fonte (a chave é a da FONTE, antes da edição)
#   campo: o nome do campo na fonte (COMARA: instrumento_numero, objeto, ...; DINV: o cabeçalho da planilha; cadastro: localidade, uf,
#          objeto); "lat"/"lon" (coordenada) e "anac_nome" valem para qualquer fonte.
from datetime import datetime  # noqa: E402

from flask import request  # noqa: E402

DDL_EDICOES = ("CREATE TABLE IF NOT EXISTS aeroportos_edicoes (processo_sei TEXT NOT NULL, chave TEXT NOT NULL, campo TEXT NOT NULL, "
               "valor TEXT, valor_fonte TEXT, alterado_em TEXT, PRIMARY KEY (processo_sei, chave, campo))")
NUMERICOS_COMARA = {"valor_uniao", "contrapartida", "valor_total_instrumento", "valor_pac", "percentual_fisico"}
# vínculo N:N instrumento x aeroporto editado pelo usuário (01/10/2026): acao 'add' = aeroporto incluído no instrumento (＋ Adicionar
# Aeroporto na ficha do instrumento / ＋ Adicionar instrumento na ficha do aeroporto); 'del' = aeroporto da fonte retirado do instrumento.
# chave = chave_aeroporto (ICAO; sem ICAO, o nome). Preservada no rebuild (criar_base_completa.carregar_semente).
DDL_VINCULOS = ("CREATE TABLE IF NOT EXISTS aeroportos_vinculos (processo_sei TEXT NOT NULL, chave TEXT NOT NULL, acao TEXT NOT NULL, "
                "aeroporto TEXT, uf TEXT, icao TEXT, alterado_em TEXT, PRIMARY KEY (processo_sei, chave))")


def atribuir_chaves(aeros: list[dict]) -> None:
    """chave_edicao de cada linha da FONTE. Mesmo aeroporto em 2+ linhas do instrumento (ex.: COMARA obra + projeto em Lábrea/Oriximiná,
    TED 002/2017): a 2ª linha vira "ICAO#2", a 3ª "ICAO#3"… — cada linha tem a sua edição (valor, objeto…) sem afetar a outra (01/10/2026)."""
    vistos: dict = {}
    for a in aeros:
        ch = chave_aeroporto(a)
        vistos[ch] = vistos.get(ch, 0) + 1
        a["chave_edicao"] = ch if vistos[ch] == 1 else f"{ch}#{vistos[ch]}"


def chave_aeroporto(a: dict) -> str:
    return (a.get("icao") or "").strip().upper() or (a.get("aeroporto") or "").strip().upper() or "—"


def _num_ou_txt(campo: str, v):
    if v in (None, ""):
        return None
    if campo in NUMERICOS_COMARA or campo in ("lat", "lon", "valor_aeroporto"):
        try:
            return float(str(v).replace(",", ".")) if not isinstance(v, (int, float)) else float(v)
        except ValueError:
            return v
    return v


def aplicar_edicoes(con: sqlite3.Connection, processo: str, aeros: list[dict], edicoes: dict | None = None) -> list[dict]:
    if edicoes is None:
        con.execute(DDL_EDICOES)
        edicoes = {}
        for p_, ch, campo, valor in con.execute("SELECT processo_sei, chave, campo, valor FROM aeroportos_edicoes WHERE processo_sei=?", (processo,)):
            edicoes.setdefault((p_, ch), {})[campo] = valor
    for a in aeros:
        ed = edicoes.get((processo, a.get("chave_edicao") or chave_aeroporto(a)))
        if not ed:
            continue
        a["chave_edicao"] = a.get("chave_edicao") or chave_aeroporto(a)
        for campo, valor in ed.items():
            v = _num_ou_txt(campo, valor)
            if campo == "valor_aeroporto":                         # valor do instrumento PARA ESTE aeroporto (ficha do aeroporto)
                a["valor_aeroporto_usuario"] = v
            elif campo == "valor_aeroporto_fonte":                 # de onde veio o valor (documento SEI, PI…); vazio = usuário
                a["valor_aeroporto_fonte"] = v
            elif campo in ("lat", "lon"):
                a[campo] = v
                a["precisao"] = "editada pelo usuário"
            elif campo == "anac_nome":
                a["anac"] = {**(a.get("anac") or {}), "nome": v}
            elif campo in ("aeroporto", "uf", "icao") and a.get("comara") is None:   # nome/UF/ICAO do aeroporto valem em qualquer fonte
                a[campo] = v.upper() if campo == "icao" and v else v
            elif a.get("comara") is not None:
                a["comara"][campo] = v
                if campo in ("aeroporto", "uf", "icao", "objeto", "valor_uniao"):
                    a[campo] = v
            elif a.get("dinv") is not None:
                a["dinv"][campo] = v
            else:                                                  # cadastro: localidade / uf / objeto
                a["aeroporto" if campo == "localidade" else campo] = v
        if a.get("comara") is None and a.get("dinv") is not None:
            for campo, alvo in (("Município", "aeroporto"), ("Estado", "uf")):
                if campo in ed:
                    a[alvo] = ed[campo]
        a["linha"] = rotulo(a.get("aeroporto"), a.get("uf"), a.get("icao"))
        a["editado"] = True
    return aeros


def todos_vinculos(con: sqlite3.Connection) -> dict:
    con.execute(DDL_VINCULOS)
    out: dict = {}
    for p_, ch, acao, aer, uf, icao in con.execute("SELECT processo_sei, chave, acao, aeroporto, uf, icao FROM aeroportos_vinculos ORDER BY alterado_em"):
        out.setdefault(p_, []).append({"chave": ch, "acao": acao, "aeroporto": aer, "uf": uf, "icao": icao})
    return out


def aplicar_vinculos(processo: str, aeros: list[dict], vinculos: dict, coords: dict, et: str) -> list[dict]:
    """Tira os aeroportos da fonte retirados pelo usuário ('del') e acrescenta os incluídos ('add'); chave_edicao já deve estar preenchida."""
    vs = vinculos.get(processo) or []
    if not vs:
        return aeros
    fora = {v["chave"] for v in vs if v["acao"] == "del"}
    out = [a for a in aeros if a.get("chave_edicao") not in fora]
    tem = {a.get("chave_edicao") for a in out}
    for v in vs:
        if v["acao"] != "add" or v["chave"] in tem:
            continue
        icao = (v["icao"] or "").strip().upper()
        k = coords.get(icao, {})
        out.append({"aeroporto": v["aeroporto"], "uf": v["uf"], "icao": icao, "linha": rotulo(v["aeroporto"], v["uf"], icao),
                    "lat": k.get("lat"), "lon": k.get("lon"), "precisao": k.get("precisao"), "etapa": et, "objeto": None, "valor_uniao": None,
                    "fonte": "usuário", "manual": True, "chave_edicao": v["chave"],
                    "anac": {"nome": k.get("nome"), "municipio": k.get("municipio"), "uf": k.get("uf")} if k else None})
        tem.add(v["chave"])
    return out


# NACIONAL (usuário, 01/10/2026): instrumento sem aeroporto específico (universidades, órgãos, BNDES, SERPRO…) ou linha "Nacional"/"Brasil"
# da COMARA. No MAPA todos ficam num ponto só, locado no DF (bolinha amarela).
NACIONAL_COORD = (-15.95, -47.45)    # canto sudeste do DF (fora da bolha do estado)


def marcar_nacional(aeros: list[dict]) -> None:
    for a in aeros:
        if _norm(a.get("aeroporto")) in ("nacional", "brasil") or (a.get("icao") or "").upper() == "BRASIL":
            a["nacional"] = True
            a["icao"] = ""
            a["lat"], a["lon"] = NACIONAL_COORD
            a["precisao"] = "NACIONAL — locado no DF"
            a["linha"] = "SAC"                     # conjunto SAC: todos os instrumentos NACIONAL (usuário, 01/10/2026)


def completar_valor(aeros: list[dict], valor_inst) -> None:
    """valor_aeroporto = valor do instrumento PARA o aeroporto (soma do MAPA): informado pelo usuário > instrumento com 1 aeroporto: o valor do
    instrumento > COMARA (valor da União por aeroporto) > sem valor (o instrumento tem vários aeroportos e ninguém repartiu)."""
    for a in aeros:
        if a.get("valor_aeroporto_usuario") is not None:
            a["valor_aeroporto"], a["valor_aeroporto_origem"] = a["valor_aeroporto_usuario"], a.get("valor_aeroporto_fonte") or "informado pelo usuário"
        elif len(aeros) == 1 and valor_inst:
            a["valor_aeroporto"], a["valor_aeroporto_origem"] = valor_inst, "valor do instrumento (aeroporto único)"
        elif a.get("comara") is not None and a.get("valor_uniao") not in (None, ""):
            a["valor_aeroporto"], a["valor_aeroporto_origem"] = a["valor_uniao"], "COMARA (valor da União no aeroporto)"
        else:
            a["valor_aeroporto"], a["valor_aeroporto_origem"] = None, None


def todas_edicoes(con: sqlite3.Connection) -> dict:
    con.execute(DDL_EDICOES)
    out: dict = {}
    for p_, ch, campo, valor in con.execute("SELECT processo_sei, chave, campo, valor FROM aeroportos_edicoes"):
        out.setdefault((p_, ch), {})[campo] = valor
    return out


@bp.post("/api/aeroportos/editar")
def api_aeroportos_editar():
    """{processo, chave, campos: {campo: valor}, fonte: {campo: valor original}} — valor igual ao da fonte apaga a edição."""
    d = request.get_json(silent=True) or {}
    processo, chave = (d.get("processo") or "").strip(), (d.get("chave") or "").strip().upper()
    campos, fonte = d.get("campos") or {}, d.get("fonte") or {}
    if not processo or not chave or not isinstance(campos, dict):
        return jsonify({"sucesso": False, "erro": "Informe processo, chave do aeroporto e campos."}), 400
    try:
        from radar_backend.arquivos_db import base_em_atualizacao
        if base_em_atualizacao():
            return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar e grave de novo."}), 409
        agora = datetime.now().isoformat(timespec="seconds")
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL_EDICOES)
        n = 0
        # campos VINCULADOS ao ✏️ do cartão (usuário, 01/10/2026): Nº do Instrumento e Término da Vigência do Instrumento são do instrumento
        from radar_backend.instrumentos_edicao import editar_campo_instrumento
        for k, campo_inst in (("inst_numero", "numero_instrumento"), ("inst_vigencia", "vigencia_mais_futura"),
                              ("inst_assinatura", "data_assinatura_instrumento")):
            if k in campos:
                v = campos.pop(k)
                if campo_inst != "numero_instrumento" and v and not re.match(r"\d{4}-\d{2}-\d{2}$", str(v)):
                    con.close()
                    return jsonify({"sucesso": False, "erro": "Data do instrumento inválida (assinatura / término da vigência)."}), 400
                n += int(editar_campo_instrumento(con, processo, campo_inst, v))
        # executora (CNPJ / nome) é do INSTRUMENTO, não do aeroporto: vai para instrumentos_executora
        ex = {k: campos.pop(k) for k in ("executora_cnpj", "executora_nome") if k in campos}
        if ex:
            con.execute("CREATE TABLE IF NOT EXISTS instrumentos_executora (processo_sei TEXT PRIMARY KEY, cnpj TEXT, nome TEXT, alterado_em TEXT)")
            atual = con.execute("SELECT cnpj, nome FROM instrumentos_executora WHERE processo_sei=?", (processo,)).fetchone() or (None, None)
            cnpj = ex.get("executora_cnpj", atual[0])
            if cnpj and len(re.sub(r"\D", "", str(cnpj))) != 14:
                con.close()
                return jsonify({"sucesso": False, "erro": "CNPJ da executora deve ter 14 dígitos."}), 400
            if "fonte" not in {r[1] for r in con.execute("PRAGMA table_info(instrumentos_executora)")}:
                con.execute("ALTER TABLE instrumentos_executora ADD COLUMN fonte TEXT")
            con.execute("INSERT OR REPLACE INTO instrumentos_executora (processo_sei, cnpj, nome, alterado_em, fonte) VALUES (?,?,?,?,?)",
                        (processo, re.sub(r"\D", "", str(cnpj)) if cnpj else None, ex.get("executora_nome", atual[1]) or None, agora, "informado pelo usuário"))
            n += 1
        # OBJETO do aeroporto editado -> OBJETO do instrumento (✏️ do cartão) também (vínculo, 01/10/2026)
        # (só com 1 aeroporto: com vários, o objeto é o de cada aeroporto — ficha do aeroporto, 01/10/2026)
        um_aero = any(c in campos for c in ("objeto", "Objeto do Instrumento")) and len(aeroportos_efetivos(con, processo)) <= 1
        for campo_obj in ("objeto", "Objeto do Instrumento"):
            if um_aero and campo_obj in campos and campos[campo_obj] not in (None, ""):
                editar_campo_instrumento(con, processo, "objeto", campos[campo_obj])
        if "valor_aeroporto" in campos and "valor_aeroporto_fonte" not in campos:
            campos["valor_aeroporto_fonte"] = None
        for campo, valor in campos.items():
            valor = None if valor in (None, "") else str(valor).strip()
            orig = fonte.get(campo)
            orig = None if orig in (None, "") else str(orig).strip()
            if valor == orig:
                n += con.execute("DELETE FROM aeroportos_edicoes WHERE processo_sei=? AND chave=? AND campo=?", (processo, chave, campo)).rowcount
            else:
                con.execute("INSERT OR REPLACE INTO aeroportos_edicoes VALUES (?,?,?,?,?,?)", (processo, chave, campo, valor, orig, agora))
                n += 1
        con.commit()
        con.close()
        return jsonify({"sucesso": True, "alterados": n})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def aeroportos_efetivos(con: sqlite3.Connection, processo: str) -> list[dict]:
    """Aeroportos do instrumento como a lista os mostra (fonte + edições do usuário)."""
    r = con.execute("SELECT processo_sei, localidades, regioes, objeto FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    if r is None:
        return []
    inst = {"processo_sei": r[0], "localidades": r[1], "regioes": r[2], "objeto": r[3]}
    apoio = mapas_de_apoio()
    aeros = aeroportos_do_instrumento(inst, "", apoio)
    atribuir_chaves(aeros)
    aeros = aplicar_vinculos(processo, aeros, todos_vinculos(con), apoio[0], "")
    aplicar_edicoes(con, processo, aeros)
    marcar_nacional(aeros)
    return aeros


# campo "lógico" lido do documento SEI -> campo de cada fonte do DETALHE do aeroporto (pré-preenchimento, usuário 01/10/2026)
# (datas de OBRA — início/término/previsão — NÃO são datas do instrumento e nunca recebem assinatura/vigência; Nº do Instrumento e Término
#  da Vigência do Instrumento são campos do INSTRUMENTO, vinculados ao ✏️ do cartão — usuário, 01/10/2026)
CAMPOS_DOC = {
    "objeto": {"comara": "objeto", "dinv": "Objeto do Instrumento", "cadastro": "objeto"},
    "valor_total": {"comara": "valor_total_instrumento"},
    "contrapartida": {"comara": "contrapartida"},
}


CAMPO_OBJETO = {"comara": "objeto", "dinv": "Objeto do Instrumento", "cadastro": "objeto"}


def propagar_objeto(con: sqlite3.Connection, processo: str, valor) -> int:
    """OBJETO do ✏️ editado -> "Objeto do Instrumento" de todos os aeroportos do instrumento no popup do título (vínculo, 01/10/2026)."""
    con.execute(DDL_EDICOES)
    agora = datetime.now().isoformat(timespec="seconds")
    n = 0
    aeros = aeroportos_efetivos(con, processo)
    if len(aeros) > 1:                  # vários aeroportos: cada um tem o seu objeto (ficha do aeroporto, 01/10/2026)
        return 0
    for a in aeros:
        fonte = "comara" if a.get("comara") is not None else "dinv" if a.get("dinv") is not None else "cadastro"
        con.execute("INSERT OR REPLACE INTO aeroportos_edicoes VALUES (?,?,?,?,?,?)",
                    (processo, a["chave_edicao"], CAMPO_OBJETO[fonte], None if valor in (None, "") else str(valor), None, agora))
        n += 1
    return n


def preencher_vazios(con: sqlite3.Connection, processo: str, valores: dict, doc: str) -> list[str]:
    """Documento SEI carregado: preenche SÓ os campos VAZIOS do DETALHE dos aeroportos do instrumento (nunca sobrescreve)."""
    con.execute(DDL_EDICOES)
    agora = datetime.now().isoformat(timespec="seconds")
    feitos = []
    for a in aeroportos_efetivos(con, processo):
        fonte = "comara" if a.get("comara") is not None else "dinv" if a.get("dinv") is not None else "cadastro"
        atuais = a.get("comara") or a.get("dinv") or {"objeto": a.get("objeto"), "localidade": a.get("aeroporto"), "uf": a.get("uf")}
        for logico, v in valores.items():
            campo = CAMPOS_DOC.get(logico, {}).get(fonte)
            if not campo or v in (None, ""):
                continue
            if atuais.get(campo) not in (None, ""):
                continue
            con.execute("INSERT OR IGNORE INTO aeroportos_edicoes VALUES (?,?,?,?,?,?)",
                        (processo, a["chave_edicao"], campo, str(v), None, agora))
            feitos.append(f"{a.get('linha') or a['chave_edicao']}: {campo} (doc. {doc})")
    return feitos


# ---------------------------------------------------------------- vínculo instrumento x aeroporto (usuário, 01/10/2026)
@bp.get("/api/aeroportos/catalogo")
def api_aeroportos_catalogo():
    """Aeródromos cadastrados (ANAC + complemento) para o ＋ Adicionar Aeroporto da ficha do instrumento."""
    try:
        coords = mapas_de_apoio()[0]
        itens = [{"icao": ic, "aeroporto": (k.get("municipio") or k.get("nome") or ic), "nome": k.get("nome"), "uf": k.get("uf"),
                  "publico": bool(k.get("publico"))} for ic, k in coords.items() if ic]
        itens.sort(key=lambda x: (not x["publico"], _norm(x["aeroporto"])))
        for x in itens:
            x["linha"] = rotulo(x["aeroporto"], x["uf"], x["icao"])
        return jsonify({"sucesso": True, "aeroportos": itens})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _guard():
    from radar_backend.arquivos_db import base_em_atualizacao
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar e grave de novo."}), 409
    return None


@bp.post("/api/aeroportos/vincular")
def api_aeroportos_vincular():
    """{processo, aeroporto, uf, icao} — inclui o aeroporto no instrumento (se ele tinha sido retirado, só desfaz a retirada)."""
    d = request.get_json(silent=True) or {}
    processo = (d.get("processo") or "").strip()
    icao = (d.get("icao") or "").strip().upper()
    nome, uf = (d.get("aeroporto") or "").strip().upper(), (d.get("uf") or "").strip().upper()
    if icao and not re.match(r"^[A-Z0-9]{4}$", icao):
        return jsonify({"sucesso": False, "erro": "ICAO deve ter 4 caracteres (ex.: SBXX)."}), 400
    if not processo or not (icao or nome):
        return jsonify({"sucesso": False, "erro": "Informe o instrumento e o aeroporto."}), 400
    g = _guard()
    if g:
        return g
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL_VINCULOS)
        if con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone() is None:
            con.close()
            return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
        chave = chave_aeroporto({"icao": icao, "aeroporto": nome})
        if any(a.get("chave_edicao") == chave for a in aeroportos_efetivos(con, processo)):
            con.close()
            return jsonify({"sucesso": True, "alterados": 0, "aviso": "O aeroporto já está no instrumento."})
        ant = con.execute("SELECT acao FROM aeroportos_vinculos WHERE processo_sei=? AND chave=?", (processo, chave)).fetchone()
        if ant and ant[0] == "del":
            con.execute("DELETE FROM aeroportos_vinculos WHERE processo_sei=? AND chave=?", (processo, chave))
        else:
            con.execute("INSERT OR REPLACE INTO aeroportos_vinculos VALUES (?,?,?,?,?,?,?)",
                        (processo, chave, "add", nome or None, uf or None, icao or None, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        con.close()
        return jsonify({"sucesso": True, "alterados": 1, "chave": chave})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/aeroportos/desvincular")
def api_aeroportos_desvincular():
    """{processo, chave} — retira o aeroporto do instrumento (incluído pelo usuário: apaga; da fonte: grava a retirada)."""
    d = request.get_json(silent=True) or {}
    processo, chave = (d.get("processo") or "").strip(), (d.get("chave") or "").strip().upper()
    if not processo or not chave:
        return jsonify({"sucesso": False, "erro": "Informe o instrumento e o aeroporto."}), 400
    g = _guard()
    if g:
        return g
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL_VINCULOS)
        ant = con.execute("SELECT acao FROM aeroportos_vinculos WHERE processo_sei=? AND chave=?", (processo, chave)).fetchone()
        if ant and ant[0] == "add":
            con.execute("DELETE FROM aeroportos_vinculos WHERE processo_sei=? AND chave=?", (processo, chave))
        else:
            con.execute("INSERT OR REPLACE INTO aeroportos_vinculos (processo_sei, chave, acao, alterado_em) VALUES (?,?,?,?)",
                        (processo, chave, "del", datetime.now().isoformat(timespec="seconds")))
        con.commit()
        con.close()
        return jsonify({"sucesso": True, "alterados": 1})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
