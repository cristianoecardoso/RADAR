"""CHECKLIST do instrumento (usuário, 03/10/2026) — botão CHECKLIST na ficha ✏️ (ao lado do título, depois do GESTOR RADAR).

  checklist_modelo        modelo ÚNICO para todos os cartões: itens e subitens (id estável, pai, ordem, rótulo)
  instrumentos_checklist  marcações de cada instrumento (processo, item)

  GET  /api/checklist?processo=       modelo + itens marcados do instrumento
  POST /api/checklist/marcar          {processo, item, marcado}
  POST /api/checklist/modelo          {itens: [{id|null, pai, rotulo}]} — muda o modelo de TODOS os cartões; marcação mantida só nos itens
                                      que não foram alterados (mesmo id e mesmo rótulo) nem excluídos
Ambas as tabelas são preservadas no rebuild.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import INSTRUMENTOS_DB
from radar_backend.radar_util import escrever_log, resposta_erro

bp = Blueprint("checklist", __name__)

PADRAO = [
    ("Nota Técnica - Justificativa da Contratação", []),
    ("PAC", []),
    ("CONVENENTE", [("CNPJ", []), ("Responsável", []), ("Declaração de Capacidade Técnica", []), ("Declaração de Contrapartida", [])]),
    ("Proposta", []),
    ("Certidão de Titularidade", []),
    ("Licença Ambiental", []),
    ("Declaração de Disponibilidade Orçamentária", []),
    ("Execução", [("Fiscalização", [("Concedente", []), ("Convenente", [])]), ("ART", []), ("Licitação", []), ("Relatórios", []), ("Medições", [])]),
]


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
    con.execute("CREATE TABLE IF NOT EXISTS checklist_modelo (id TEXT PRIMARY KEY, pai TEXT, ordem INTEGER, rotulo TEXT NOT NULL, alterado_em TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_checklist (processo_sei TEXT NOT NULL, item TEXT NOT NULL, marcado_em TEXT, "
                "PRIMARY KEY (processo_sei, item))")
    if not con.execute("SELECT 1 FROM checklist_modelo LIMIT 1").fetchone():
        agora, linhas = datetime.now().isoformat(timespec="seconds"), []
        def semear(itens, pai):
            for k, (rot, filhos) in enumerate(itens):
                i = uuid.uuid4().hex[:10]
                linhas.append((i, pai, k, rot, agora))
                semear(filhos, i)
        semear(PADRAO, None)
        con.executemany("INSERT INTO checklist_modelo VALUES (?,?,?,?,?)", linhas)
        con.commit()
    return con


def _modelo(con) -> list[dict]:
    return [{"id": i, "pai": p, "ordem": o, "rotulo": r} for i, p, o, r in
            con.execute("SELECT id, pai, ordem, rotulo FROM checklist_modelo ORDER BY ordem")]


@bp.get("/api/checklist")
def api_checklist():
    proc = (request.args.get("processo") or "").strip()
    try:
        con = _con()
        modelo = _modelo(con)
        marc = [r[0] for r in con.execute("SELECT item FROM instrumentos_checklist WHERE processo_sei=?", (proc,))] if proc else []
        con.close()
        return jsonify({"sucesso": True, "modelo": modelo, "marcados": marc})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/checklist/resumo")
def api_checklist_resumo():
    """{processo: [marcados, total de itens-folha]} para o rótulo do botão."""
    try:
        con = _con()
        modelo = _modelo(con)
        pais = {m["pai"] for m in modelo if m["pai"]}
        folhas = {m["id"] for m in modelo if m["id"] not in pais}
        out = {}
        for p, it in con.execute("SELECT processo_sei, item FROM instrumentos_checklist"):
            if it in folhas:
                out[p] = out.get(p, 0) + 1
        con.close()
        return jsonify({"sucesso": True, "total": len(folhas), "marcados": out})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/checklist/marcar")
def api_marcar():
    d = request.get_json(silent=True) or {}
    proc, item, marcado = (d.get("processo") or "").strip(), (d.get("item") or "").strip(), bool(d.get("marcado"))
    try:
        con = _con()
        if not con.execute("SELECT 1 FROM checklist_modelo WHERE id=?", (item,)).fetchone():
            con.close()
            return jsonify({"sucesso": False, "erro": "Item do checklist não encontrado (o modelo mudou?)."}), 404
        if marcado:
            con.execute("INSERT OR REPLACE INTO instrumentos_checklist VALUES (?,?,?)", (proc, item, datetime.now().isoformat(timespec="seconds")))
        else:
            con.execute("DELETE FROM instrumentos_checklist WHERE processo_sei=? AND item=?", (proc, item))
        con.commit()
        con.close()
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/checklist/modelo")
def api_modelo():
    """Novo modelo (lista completa, na ordem). Item com id e o MESMO rótulo de antes = mantido (marcações ficam); rótulo alterado ou id
    novo = item novo (sem marcações); id ausente da lista = excluído (marcações apagadas em todos os cartões)."""
    d = request.get_json(silent=True) or {}
    itens = d.get("itens") or []
    if not itens or any(not str(x.get("rotulo") or "").strip() for x in itens):
        return jsonify({"sucesso": False, "erro": "O checklist precisa de itens, todos com nome."}), 400
    try:
        con = _con()
        antes = {m["id"]: m["rotulo"] for m in _modelo(con)}
        agora = datetime.now().isoformat(timespec="seconds")
        novos, mapa, mantidos = [], {}, set()
        ordem_pai: dict = {}
        for x in itens:
            iid, rot = x.get("id"), str(x["rotulo"]).strip()
            if iid in antes and antes[iid] == rot:
                nid = iid
                mantidos.add(iid)
            else:
                nid = uuid.uuid4().hex[:10]
            mapa[x.get("tmp") or iid or nid] = nid
            pai = mapa.get(x.get("pai")) if x.get("pai") else None
            ordem_pai[pai] = ordem_pai.get(pai, -1) + 1
            novos.append((nid, pai, ordem_pai[pai], rot, agora))
        apagar = set(antes) - mantidos
        con.execute("DELETE FROM checklist_modelo")
        con.executemany("INSERT INTO checklist_modelo VALUES (?,?,?,?,?)", novos)
        n_marc = 0
        for i in apagar:
            n_marc += con.execute("DELETE FROM instrumentos_checklist WHERE item=?", (i,)).rowcount
        con.commit()
        con.close()
        escrever_log(f"CHECKLIST | modelo alterado: {len(novos)} item(ns), {len(apagar)} alterado(s)/excluído(s), {n_marc} marcação(ões) desfeita(s)")
        return jsonify({"sucesso": True, "itens": len(novos), "alterados_ou_excluidos": len(apagar), "marcacoes_desfeitas": n_marc})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
