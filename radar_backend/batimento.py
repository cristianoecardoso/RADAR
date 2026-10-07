"""BATIMENTO do TG na tela (usuário, 03/10/2026) — resultado de scripts/batimento_tg.py, rodado depois de cada API-TG.

  GET  /api/tg/batimento               última execução, pendências (com sugestões) e entradas novas vinculadas
  POST /api/tg/batimento/vincular      {documento, processo}: cadastra no instrumento a CHAVE do documento — o PI (instrumento_pis)
                                       ou, sem PI, o processo citado (processo relacionado) — e roda o join: o documento (e os
                                       outros com a mesma chave) entram com valor e data das bases
  POST /api/tg/batimento/ignorar       {documento, motivo}: "não é de instrumento" (sai das pendências)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import INSTRUMENTOS_DB, SCRIPTS_DIR, TG_DIR
from radar_backend.radar_util import agora_iso, escrever_log, resposta_erro

bp = Blueprint("batimento", __name__)
DDL_EXEC = "CREATE TABLE IF NOT EXISTS tg_batimento_exec (quando TEXT PRIMARY KEY, resumo TEXT)"


def _mod():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import importlib
    import batimento_tg
    importlib.reload(batimento_tg)
    return batimento_tg


def rodar(rodar_join) -> dict:
    """Chamado por tg_drive depois do join: batimento das entradas novas + registro do resumo."""
    B = _mod()
    con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
    try:
        res = B.executar(con, TG_DIR, rodar_join=rodar_join)
        con.execute(DDL_EXEC)
        con.execute("INSERT OR REPLACE INTO tg_batimento_exec VALUES (?,?)", (agora_iso(), json.dumps(res, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    escrever_log(f"BATIMENTO | {res.get('baseline', 0)} na base · {res.get('novos', 0)} novo(s): {res.get('vinculados', 0)} vinculado(s), "
                 f"{res.get('auto', 0)} por PI aprendido, {res.get('pendentes', 0)} pendente(s); {res.get('resolvidos', 0)} pendência(s) resolvida(s)")
    return res


def _rotulos(con) -> dict:
    return {p: f"{t or p}" + (f" — {(l or '').split('|')[0].strip()}" if l else "") for p, t, l in
            con.execute("SELECT processo_sei, tipo_instrumento, localidades FROM instrumentos")}


@bp.get("/api/tg/batimento")
def api_batimento():
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.row_factory = sqlite3.Row
        _mod()
        con.execute(_mod().DDL)
        con.execute(DDL_EXEC)
        rot = _rotulos(con)
        ult = con.execute("SELECT quando, resumo FROM tg_batimento_exec ORDER BY quando DESC LIMIT 1").fetchone()
        campos = "documento, tipo, valor, data, pi, processo_citado, favorecido, texto, primeira_vez, status, processo_sei, metodo, sugestoes"
        pend = [dict(r) for r in con.execute(f"SELECT {campos} FROM tg_batimento WHERE status='pendente' ORDER BY primeira_vez DESC, documento DESC")]
        rec = [dict(r) for r in con.execute(f"SELECT {campos} FROM tg_batimento WHERE status IN ('vinculado','auto') "
                                             "ORDER BY primeira_vez DESC, documento DESC LIMIT 300")]
        n_base = con.execute("SELECT COUNT(*) FROM tg_batimento WHERE status='base'").fetchone()[0]
        n_ign = con.execute("SELECT COUNT(*) FROM tg_batimento WHERE status='ignorado'").fetchone()[0]
        con.close()
        for x in pend + rec:
            x["sugestoes"] = json.loads(x["sugestoes"]) if x.get("sugestoes") else []
            for s in x["sugestoes"]:
                s["rotulo"] = rot.get(s["processo"], s["processo"])
            x["rotulo"] = rot.get(x.get("processo_sei"), x.get("processo_sei"))
        c3 = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        gov = gov_pendencias(c3)
        c3.commit(); c3.close()
        return jsonify({"sucesso": True, "gov": gov, "ultima": {"quando": ult["quando"], **json.loads(ult["resumo"])} if ult else None,
                        "pendentes": pend, "recentes": rec, "n_base": n_base, "n_ignorados": n_ign,
                        "instrumentos": [{"processo": p, "rotulo": r} for p, r in sorted(rot.items(), key=lambda kv: kv[1])]})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/tg/batimento/vincular")
def api_vincular():
    d = request.get_json(silent=True) or {}
    doc, proc = (d.get("documento") or "").strip(), (d.get("processo") or "").strip()
    modo = (d.get("modo") or "").strip()          # pi | processo | documento (padrão: PI, senão processo, senão documento)
    try:
        from radar_backend.arquivos_db import base_em_atualizacao
        if base_em_atualizacao():
            return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar."}), 409
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        r = con.execute("SELECT pi, processo_citado FROM tg_batimento WHERE documento=?", (doc,)).fetchone()
        if not r or not con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (proc,)).fetchone():
            con.close()
            return jsonify({"sucesso": False, "erro": "Documento ou instrumento não encontrado."}), 404
        pi, pcit = r
        agora = agora_iso()
        if not modo:
            modo = "pi" if pi else "processo" if pcit else "documento"
        if modo == "pi" and not pi:
            modo = "documento"
        if modo == "processo" and not pcit:
            modo = "documento"
        if modo == "documento":
            t, v, dt = con.execute("SELECT tipo, valor, data FROM tg_batimento WHERE documento=?", (doc,)).fetchone()
            etapa = {"NE": "EMPENHO", "NC": "CREDITO", "OB": "PAGAMENTO", "PF": "PF_TG", "GR": "GRU"}.get(t, t)
            con.execute("CREATE TABLE IF NOT EXISTS tg_vinculos_documento (documento TEXT PRIMARY KEY, processo_sei TEXT NOT NULL, etapa TEXT, "
                        "valor REAL, data TEXT, motivo TEXT, criado_em TEXT)")
            con.execute("INSERT OR REPLACE INTO tg_vinculos_documento VALUES (?,?,?,?,?,?,?)",
                        (doc, proc, etapa, v, dt, (d.get("motivo") or "sugestão do batimento")[:300], agora))
            chave = "só este documento"
        elif modo == "pi":
            B = _mod()
            J = B.J
            J.PIS_USUARIO.clear()
            J.PIS_USUARIO.update({x: p for p, x in con.execute("SELECT processo_sei, pi FROM instrumento_pis")})
            por_processo, por_siafi, _ = J.carregar_instrumentos(con)
            dono = J.pi_bate_siafi(pi, por_siafi)
            if dono and dono != proc:
                con.close()
                return jsonify({"sucesso": False, "erro": f"O PI {pi} já pertence ao instrumento {dono}: desvincule lá antes."}), 409
            con.execute("INSERT OR IGNORE INTO instrumento_pis VALUES (?,?,?)", (proc, pi, agora))
            con.execute("DELETE FROM pis_desvinculados WHERE processo_sei=? AND pi=?", (proc, pi))
            chave = f"PI {pi}"
        elif modo == "processo":
            con.execute("CREATE TABLE IF NOT EXISTS processos_relacionados_usuario (processo_principal TEXT NOT NULL, processo_relacionado TEXT NOT NULL, "
                        "motivo TEXT, criado_em TEXT, PRIMARY KEY (processo_principal, processo_relacionado))")
            con.execute("INSERT OR REPLACE INTO processos_relacionados_usuario VALUES (?,?,?,?)", (proc, pcit, f"batimento TG: {doc}", agora))
            if not con.execute("SELECT 1 FROM processos_relacionados WHERE processo_principal=? AND processo_relacionado=?", (proc, pcit)).fetchone():
                con.execute("INSERT INTO processos_relacionados (processo_principal, processo_relacionado, motivo, fonte, coletado_em) VALUES (?,?,?,?,?)",
                            (proc, pcit, f"batimento TG: {doc}", "usuário (batimento TG)", agora))
            chave = f"processo {pcit}"
        con.commit()
        con.close()
        from radar_backend.tg_drive import _rodar_join
        _rodar_join()
        res = rodar(None)                                   # marca como resolvidas as pendências agora ligadas
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        st = con.execute("SELECT status, processo_sei FROM tg_batimento WHERE documento=?", (doc,)).fetchone()
        con.close()
        escrever_log(f"BATIMENTO | {doc}: {chave} vinculado(a) a {proc} pelo usuário -> {st}")
        ok = st and st[0] != "pendente"
        return jsonify({"sucesso": True, "vinculado": bool(ok), "chave": chave, "resolvidos": res.get("resolvidos", 0),
                        "aviso": None if ok else "A chave foi gravada, mas o join não ligou o documento (ver log)."})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/tg/batimento/ignorar")
def api_ignorar():
    d = request.get_json(silent=True) or {}
    doc, motivo = (d.get("documento") or "").strip(), (d.get("motivo") or "não é de instrumento").strip()
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        n = con.execute("UPDATE tg_batimento SET status='ignorado', metodo=?, atualizado_em=? WHERE documento=?",
                        (f"usuário: {motivo}", agora_iso(), doc)).rowcount
        con.commit()
        con.close()
        return jsonify({"sucesso": bool(n)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# =============================================================================== GOV (Transferegov) — usuário, 03/10/2026
# Pendências do GOV no mesmo botão BATIMENTO: (1) instrumentos do Transferegov (UG 110591) fora do RADAR; (2) divergências RADAR x
# Transferegov; (3) mudanças das últimas coletas (API-GOV) em instrumentos do RADAR e convênios/aditivos NOVOS sem instrumento no RADAR.
# Decisões em gov_batimento (preservada no rebuild): ignorado | ciente | vinculado (processo).
DDL_GOV = ("CREATE TABLE IF NOT EXISTS gov_batimento (chave TEXT PRIMARY KEY, decisao TEXT NOT NULL, processo_sei TEXT, nota TEXT, "
           "criado_em TEXT)")
CAMPOS_RUIDO = {"criterio_selecao", "coleta_em_utc", "coleta_endpoint", "coleta_ug", "_raw_json", "_row_hash", "_incluido_em",
                "_atualizado_em", "_execucao_id"}
TABELAS_NOVO_RELEVANTE = ("transferegov_siconv_convenio", "transferegov_siconv_termo_aditivo", "transferegov_ted_plano_acao",
                          "transferegov_ted_termo_execucao")


def _digs(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def gov_pendencias(con: sqlite3.Connection) -> dict:
    con.execute(DDL_GOV)
    dec = {k: d for k, d in con.execute("SELECT chave, decisao FROM gov_batimento")}
    rot = _rotulos(con)
    proc_por_dig = {_digs(p): p for p in rot if len(_digs(p)) >= 15}
    try:
        for pr, rel in con.execute("SELECT processo_principal, processo_relacionado FROM processos_relacionados"):
            proc_por_dig.setdefault(_digs(rel), pr)
    except sqlite3.Error:
        pass
    siafi_proc = {_digs(s): p for p, s in con.execute("SELECT processo_sei, numero_siafi FROM instrumentos") if len(_digs(s)) >= 5}
    fora, diverg, alts = [], [], []
    for r in con.execute("SELECT id, origem_instrumento, processo_sei, localidade, tipo_instrumento, numero_siafi, modulo_transferegov, "
                         "id_transferegov, situacao_transferegov, inicio_vigencia_transferegov, fim_vigencia_transferegov, divergencia "
                         "FROM instrumentos_transferegov"):
        (i, origem, proc, loc, tipo, siafi, mod, idt, sit, ini, fim, div) = r
        if str(origem).startswith("Transferegov"):
            ch = f"FORA:{idt}"
            if ch in dec:
                continue
            antigo = (fim or "")[:4] and (fim or "")[:4] < "2019"
            fora.append({"chave": ch, "id": idt, "proponente": loc, "modulo": mod, "situacao": sit, "inicio": ini, "fim": fim,
                         "explicacao": (f"Convênio/TC nº {idt} da UG 110591 no Transferegov ({mod}), {sit or 'situação não informada'}, "
                                        f"vigência {ini or '?'} a {fim or '?'}; não há instrumento no RADAR com esse nº."
                                        + (" Encerrado antes de 2019: provavelmente fora do escopo do RADAR — confira e ignore." if antigo else
                                           " Confira se é um instrumento do FNAC que falta no RADAR.")),
                         "sugestao": siafi_proc.get(_digs(idt))})
        elif div:
            ch = f"DIV:{i}"
            if ch in dec:
                continue
            diverg.append({"chave": ch, "processo": proc, "rotulo": rot.get(proc, proc), "id": idt or siafi, "situacao": sit,
                           "explicacao": f"{div} (Transferegov: nº {idt or siafi}, {mod}, {sit or '—'}, vigência até {fim or '—'})."})
    # mudanças das 2 últimas coletas (TED e TC)
    execs = [e for (e,) in con.execute("SELECT MAX(id) FROM transferegov_execucoes GROUP BY fonte")] if con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='transferegov_execucoes'").fetchone() else []
    grupos: dict = {}
    for ex, quando, fonte, tab, tipo, inst, ref, campo, ant, novo in con.execute(
            f"SELECT execucao_id, executado_em, fonte, tabela, tipo, instrumento, referencia, campo, valor_anterior, valor_novo "
            f"FROM transferegov_alteracoes WHERE execucao_id IN ({','.join('?' * len(execs)) or 'NULL'})", execs):
        if (campo or "") in CAMPOS_RUIDO:
            continue
        dg = _digs(inst)
        proc = (proc_por_dig.get(dg) if len(dg) >= 15 else None) or (siafi_proc.get(_digs(ref)) if len(_digs(ref)) >= 5 else None)
        if tipo == "NOVO" and not (tab in TABELAS_NOVO_RELEVANTE):
            continue
        if not proc and tipo != "NOVO":
            continue
        chave = f"ALT:{ex}:{proc or ref}"
        g = grupos.setdefault(chave, {"chave": chave, "processo": proc, "rotulo": rot.get(proc, proc) if proc else None, "referencia": ref,
                                      "fonte": fonte, "quando": quando, "itens": []})
        if len(g["itens"]) < 12:
            g["itens"].append(f"{tipo.lower()} em {tab.replace('transferegov_', '')}" + (f": {campo} {str(ant)[:40]} → {str(novo)[:40]}" if campo else ""))
    def _siconv(ref):
        n = _digs(ref)
        try:
            r = con.execute("SELECT c.nr_convenio, c.sit_convenio, c.dia_assin_conv, c.dia_fim_vigenc_conv, c.vl_global_conv, c.nr_processo, "
                            "p.nm_proponente, p.identif_proponente, p.munic_proponente, p.uf_proponente, p.objeto_proposta "
                            "FROM transferegov_siconv_convenio c LEFT JOIN transferegov_siconv_proposta p USING (id_proposta) WHERE trim(c.nr_convenio)=?",
                            (n,)).fetchone()
        except sqlite3.Error:
            return ""
        if not r:
            return ""
        nr, sit, ass, fim, vl, nproc, nm, cnpj, mun, uf, obj = r
        return (f" — {nm or '?'} (CNPJ {cnpj or '?'}, {mun or '?'}/{uf or '?'}), {sit or '?'}, assinado em {ass or '?'}, vigência até {fim or '?'}, "
                f"valor global {vl or '?'}, processo {nproc or '?'}; objeto: {str(obj or '')[:160]}")
    for ch, g in grupos.items():
        if ch in dec:
            continue
        g["explicacao"] = ((f"Coleta {g['fonte']} de {g['quando'][:10]}: mudanças no Transferegov do instrumento {g['rotulo']} ({g['referencia']})"
                            if g["processo"] else
                            f"Coleta {g['fonte']} de {g['quando'][:10]}: {g['referencia']} NOVO no Transferegov, sem instrumento no RADAR"
                            + _siconv(g["referencia"])))
        alts.append(g)
    return {"fora": fora, "divergencias": diverg, "alteracoes": alts, "total": len(fora) + len(diverg) + len(alts)}


@bp.post("/api/gov/batimento/decidir")
def api_gov_decidir():
    d = request.get_json(silent=True) or {}
    chaves = d.get("chaves") or ([d.get("chave")] if d.get("chave") else [])
    decisao, proc = (d.get("decisao") or "").strip(), (d.get("processo") or "").strip() or None
    if decisao not in ("ignorado", "ciente", "vinculado") or not chaves:
        return jsonify({"sucesso": False, "erro": "Informe a chave e a decisão (ignorado, ciente ou vinculado)."}), 400
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL_GOV)
        agora = agora_iso()
        for ch in chaves:
            con.execute("INSERT OR REPLACE INTO gov_batimento VALUES (?,?,?,?,?)", (ch, decisao, proc, (d.get("nota") or "")[:300], agora))
            if decisao == "vinculado" and proc and ch.startswith("FORA:"):      # nº do Transferegov vira o NÚMERO GOV do instrumento
                con.execute("CREATE TABLE IF NOT EXISTS instrumentos_numero_gov (processo_sei TEXT PRIMARY KEY, numero_gov TEXT, fonte TEXT, alterado_em TEXT)")
                if not con.execute("SELECT 1 FROM instrumentos_numero_gov WHERE processo_sei=?", (proc,)).fetchone():
                    con.execute("INSERT INTO instrumentos_numero_gov VALUES (?,?,?,?)", (proc, ch[5:], "batimento GOV", agora))
        con.commit()
        con.close()
        escrever_log(f"BATIMENTO GOV | {len(chaves)} item(ns): {decisao}{' -> ' + proc if proc else ''}")
        return jsonify({"sucesso": True, "n": len(chaves)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
