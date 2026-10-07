"""PREFERÊNCIAS DE TELA do RADAR (usuário, 03/10/2026: "grave a última configuração para outras aberturas do RADAR").

  radar_preferencias (chave, valor JSON, alterado_em) — preservada no rebuild (scripts/criar_base_completa.py)
  GET  /api/preferencias          {chave: valor, ...}
  POST /api/preferencias          {chave, valor}  (valor null apaga)

Chaves em uso: "mapa_layout" = {"normal": fração da largura do mapa, "cheia": fração na TELA CHEIA}.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import INSTRUMENTOS_DB
from radar_backend.radar_util import resposta_erro

bp = Blueprint("preferencias", __name__)
DDL = "CREATE TABLE IF NOT EXISTS radar_preferencias (chave TEXT PRIMARY KEY, valor TEXT, alterado_em TEXT)"


@bp.get("/api/preferencias")
def api_preferencias():
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL)
        out = {}
        for k, v in con.execute("SELECT chave, valor FROM radar_preferencias"):
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        con.close()
        return jsonify({"sucesso": True, "preferencias": out})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/preferencias")
def api_preferencias_gravar():
    d = request.get_json(silent=True) or {}
    chave = str(d.get("chave") or "").strip()
    if not chave or len(chave) > 80:
        return jsonify({"sucesso": False, "erro": "Informe a chave da preferência."}), 400
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        con.execute(DDL)
        if d.get("valor") is None:
            con.execute("DELETE FROM radar_preferencias WHERE chave=?", (chave,))
        else:
            con.execute("INSERT OR REPLACE INTO radar_preferencias VALUES (?,?,?)",
                        (chave, json.dumps(d["valor"], ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
        con.commit()
        con.close()
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
