"""Financiamento BNDES/FNAC às companhias aéreas (usuário, 03/10/2026): botões AZUL · GOL · LATAM · ABAETÉ (símbolo de cada
companhia) no popup do link "BNDES"; cada botão abre Nome do Consórcio/Empresa, CNPJ, Valor Autorizado, Valor Financiado, Linha de
Financiamento, Valor Liberado pelo BNDES e Data de Liberação — editáveis (✏️), cada campo com a sua FONTE.

  GET  /api/bndes/empresas                 lista (sem o SVG)
  GET  /api/bndes/logo/<empresa>           símbolo (SVG guardado no banco)
  POST /api/bndes/empresas/editar          {empresa, campos: {campo: valor}, fontes: {campo: texto}}
  GET  /api/bndes/calculo-base             desembolsos do FNAC ao BNDES (OBs do tg_execucao_join) p/ o cálculo de retorno
  GET  /api/bndes/selic?inicio=&fim=        Taxa Média Selic diária (BCB SGS 11, cache em bndes_selic): fator acumulado pro rata die
  POST /api/bndes/empresas/calculo         {empresa, calc: {campo: valor}} — campos do CÁLCULO DO VALOR A RETORNAR ao FNAC editados
  POST /api/bndes/cronograma/baixa         {empresa, parcela, pago, valor} — ✓ manual do CRONOGRAMA DE DEVOLUÇÃO (bndes_baixas)

CÁLCULO DO VALOR A RETORNAR AO FNAC (usuário, 03/10/2026) — Contrato FNAC x BNDES (SEI 10706996) e 1º TA (SEI 11464570), Cláusula Sétima:
  I  enquanto em tesouraria no BNDES (do repasse do FNAC até a liberação à aérea): Taxa Média Selic, pro rata die;
  II após a liberação à aérea: encargos financeiros dos mutuários (Res. CMN 5.260/2025 ou 5.297/2026 — capital de giro, 4% a.a.).

Tabela bndes_empresas no Instrumentos.db, preservada no rebuild (scripts/criar_base_completa.py). Carga inicial (03/10/2026): notícias
públicas e cadastro CNPJ — ver FONTES_INICIAIS; o que não foi encontrado fica vazio (preencher pelo ✏️ com o contrato/BNDES).
Símbolos: Wikimedia Commons (Azul, GOL, LATAM) e site oficial voeabaete.com.br (Abaeté), cópia em dados/BNDES/logos.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen

from flask import Blueprint, Response, jsonify, request

from radar_backend.radar_config import DADOS_DIR, INSTRUMENTOS_DB
from radar_backend.radar_util import escrever_log, resposta_erro

bp = Blueprint("bndes", __name__)

LOGOS_DIR = DADOS_DIR / "BNDES" / "logos"
CAMPOS = ["nome", "cnpj", "valor_autorizado", "valor_financiado", "linha", "valor_liberado", "data_liberacao"]
NUMERICOS = {"valor_autorizado", "valor_financiado", "valor_liberado"}

_N_AUT = "https://www.panrotas.com.br/aviacao/empresas/2026/07/bndes-libera-ate-r-8-bilhoes-em-credito-para-abaete-azul-gol-e-latam_230649.html"
_N_LIM = "https://passageirodeprimeira.com/bndes-aprova-8-bilhoes-capital-giro-companhias-aereas/"
_N_RED = "https://pontospravoar.com/governo-redistribui-r-492-milhoes-do-fnac-entre-latam-azul-e-gol/"
_N_AZUL = "https://www.panrotas.com.br/aviacao/empresas/2026/09/azul-recebe-primeiro-desembolso-de-r-13-bilhao-do-fnac-via-bndes_232416.html"
_LINHA = "Capital de giro — FNAC/BNDES (taxa fixa de 4% a.a., prazo até 60 meses, carência até 12 meses; disponível até dez/2026)"
_F_LINHA = f"BNDES aprova linha de até R$ 8 bi (30/07/2026), condições do CGFNAC — {_N_AUT} · {_N_LIM}"
_F_AUT3 = (f"R$ 2,5 bi (limite por companhia aprovado pelo BNDES em 30/07/2026 — {_N_LIM}) + R$ 164 mi (redistribuição de R$ 492 mi "
           f"do FNAC, resolução de 21/07/2026 — {_N_RED})")

# (ordem, empresa, rótulo, logo, {campo: (valor, fonte)})
FONTES_INICIAIS = [
    (1, "AZUL", "Azul", "azul.svg", {
        "nome": ("Azul Linhas Aéreas Brasileiras S.A.", "cadastro CNPJ — https://cnpja.com/office/09296295000160 (conferir o tomador no contrato BNDES)"),
        "cnpj": ("09.296.295/0001-60", "cadastro CNPJ — https://cnpja.com/office/09296295000160"),
        "valor_autorizado": (2664000000.0, _F_AUT3),
        "valor_financiado": (2660000000.0, f"contrato definitivo com o BNDES, \"até R$ 2,66 bilhões\" (29/09/2026) — {_N_AZUL}"),
        "linha": (_LINHA, _F_LINHA),
        "valor_liberado": (1300000000.0, f"1º desembolso de R$ 1,3 bi; restante (~R$ 1,3 bi) previsto no 4º tri/2026 — {_N_AZUL}"),
        "data_liberacao": ("2026-09-29", f"1º desembolso em 29/09/2026 — {_N_AZUL}"),
    }),
    (2, "GOL", "GOL", "gol.svg", {
        "nome": ("GOL Linhas Aéreas S.A.", "cadastro CNPJ — https://cnpjcheck.com.br/empresa/gol-linhas-aereas-s-a-07575651000159 (conferir o tomador no contrato BNDES)"),
        "cnpj": ("07.575.651/0001-59", "cadastro CNPJ — https://cnpjcheck.com.br/empresa/gol-linhas-aereas-s-a-07575651000159"),
        "valor_autorizado": (2664000000.0, _F_AUT3),
        "valor_financiado": (None, "não divulgado até 03/10/2026 (sem contrato/desembolso noticiado)"),
        "linha": (_LINHA, _F_LINHA),
        "valor_liberado": (None, "não divulgado até 03/10/2026"),
        "data_liberacao": (None, "não divulgado até 03/10/2026"),
    }),
    (3, "LATAM", "LATAM", "latam.svg", {
        "nome": ("TAM Linhas Aéreas S.A. (LATAM Airlines Brasil)", "cadastro CNPJ — https://www.informecadastral.com.br/cnpj/tam-linhas-aereas-sa-02012862000160 (conferir o tomador no contrato BNDES)"),
        "cnpj": ("02.012.862/0001-60", "cadastro CNPJ — https://www.informecadastral.com.br/cnpj/tam-linhas-aereas-sa-02012862000160"),
        "valor_autorizado": (2664000000.0, _F_AUT3),
        "valor_financiado": (None, "não divulgado até 03/10/2026 (sem contrato/desembolso noticiado)"),
        "linha": (_LINHA, _F_LINHA),
        "valor_liberado": (None, "não divulgado até 03/10/2026"),
        "data_liberacao": (None, "não divulgado até 03/10/2026"),
    }),
    (4, "ABAETE", "Abaeté", "abaete.svg", {
        "nome": ("ATA Aerotáxi Abaeté Ltda. (Abaeté Aviação)", "cadastro CNPJ — https://casadosdados.com.br/solucao/cnpj/ata-aerotaxi-abaete-ltda-14674451000119 (conferir o tomador no contrato BNDES)"),
        "cnpj": ("14.674.451/0001-19", "cadastro CNPJ — https://casadosdados.com.br/solucao/cnpj/ata-aerotaxi-abaete-ltda-14674451000119"),
        "valor_autorizado": (80000000.0, f"limite de até R$ 80 mi para a Abaeté (BNDES, 30/07/2026) — {_N_LIM}"),
        "valor_financiado": (None, "não divulgado até 03/10/2026"),
        "linha": (_LINHA, _F_LINHA),
        "valor_liberado": (None, "não divulgado até 03/10/2026"),
        "data_liberacao": (None, "não divulgado até 03/10/2026"),
    }),
]


def garantir_tabela(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS bndes_empresas (empresa TEXT PRIMARY KEY, ordem INTEGER, rotulo TEXT, logo_svg TEXT, "
                + ", ".join(f"{c} {'REAL' if c in NUMERICOS else 'TEXT'}" for c in CAMPOS)
                + ", fontes_json TEXT, alterado_em TEXT)")
    for ordem, emp, rot, logo, campos in FONTES_INICIAIS:          # carga inicial só se a empresa ainda não existe (edições ficam)
        if con.execute("SELECT 1 FROM bndes_empresas WHERE empresa=?", (emp,)).fetchone():
            continue
        arq = LOGOS_DIR / logo
        svg = arq.read_text(encoding="utf-8") if arq.exists() else None
        con.execute(f"INSERT INTO bndes_empresas (empresa, ordem, rotulo, logo_svg, {', '.join(CAMPOS)}, fontes_json, alterado_em) "
                    f"VALUES ({','.join('?' * (len(CAMPOS) + 6))})",
                    [emp, ordem, rot, svg, *[campos[c][0] for c in CAMPOS],
                     json.dumps({c: campos[c][1] for c in CAMPOS}, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")])


PROCESSO_BNDES = "50020.006853/2025-51"
SGS_SELIC = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.11/dados?formato=json&dataInicial={ini}&dataFinal={fim}"


def _garantir_calc(con: sqlite3.Connection) -> None:
    if "calc_json" not in {r[1] for r in con.execute("PRAGMA table_info(bndes_empresas)")}:
        con.execute("ALTER TABLE bndes_empresas ADD COLUMN calc_json TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS bndes_selic (data TEXT PRIMARY KEY, taxa_dia REAL, coletado_em TEXT)")
    # ✓ do cronograma de devolução (usuário, 03/10/2026): parcela marcada = recebida pelo FNAC (sai do saldo a receber)
    con.execute("CREATE TABLE IF NOT EXISTS bndes_baixas (empresa TEXT, parcela TEXT, valor REAL, marcado_em TEXT, PRIMARY KEY (empresa, parcela))")


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
    con.row_factory = sqlite3.Row
    garantir_tabela(con)
    _garantir_calc(con)
    con.commit()
    return con


@bp.get("/api/bndes/empresas")
def api_empresas():
    try:
        con = _con()
        linhas = [dict(r) for r in con.execute("SELECT * FROM bndes_empresas ORDER BY ordem")]
        baixas = {}
        for emp_, parc, val, em in con.execute("SELECT empresa, parcela, valor, marcado_em FROM bndes_baixas"):
            baixas.setdefault(emp_, {})[parc] = {"valor": val, "marcado_em": em}
        con.close()
        for r in linhas:
            r["baixas"] = baixas.get(r["empresa"], {})
        for r in linhas:
            r["tem_logo"] = bool(r.pop("logo_svg"))
            r["fontes"] = json.loads(r.pop("fontes_json") or "{}")
            r["calc"] = json.loads(r.pop("calc_json") or "{}")
        return jsonify({"sucesso": True, "empresas": linhas})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/bndes/logo/<empresa>")
def api_logo(empresa: str):
    try:
        con = _con()
        r = con.execute("SELECT logo_svg FROM bndes_empresas WHERE empresa=?", (empresa.upper(),)).fetchone()
        con.close()
        if not r or not r[0]:
            return jsonify({"sucesso": False, "erro": "Sem símbolo."}), 404
        return Response(r[0], mimetype="image/svg+xml", headers={"Cache-Control": "max-age=86400"})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/bndes/empresas/editar")
def api_editar():
    d = request.get_json(silent=True) or {}
    emp = (d.get("empresa") or "").strip().upper()
    campos, fontes = dict(d.get("campos") or {}), dict(d.get("fontes") or {})
    try:
        con = _con()
        r = con.execute("SELECT fontes_json FROM bndes_empresas WHERE empresa=?", (emp,)).fetchone()
        if r is None:
            con.close()
            return jsonify({"sucesso": False, "erro": "Empresa não encontrada."}), 404
        fts = json.loads(r[0] or "{}")
        sets, vals = [], []
        for c, v in campos.items():
            if c not in CAMPOS:
                continue
            v = None if v in (None, "") else v
            if c in NUMERICOS and v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    con.close()
                    return jsonify({"sucesso": False, "erro": f"Valor inválido em {c}."}), 400
            if c == "cnpj" and v is not None and len(re.sub(r"\D", "", str(v))) != 14:
                con.close()
                return jsonify({"sucesso": False, "erro": "CNPJ deve ter 14 dígitos."}), 400
            if c == "data_liberacao" and v is not None and not re.match(r"\d{4}-\d{2}-\d{2}$", str(v)):
                con.close()
                return jsonify({"sucesso": False, "erro": "Data de liberação inválida."}), 400
            sets.append(f"{c}=?")
            vals.append(v)
            fts[c] = (fontes.get(c) or "").strip() or f"informado pelo usuário em {datetime.now().strftime('%d/%m/%Y')}"
        if sets:
            con.execute(f"UPDATE bndes_empresas SET {', '.join(sets)}, fontes_json=?, alterado_em=? WHERE empresa=?",
                        [*vals, json.dumps(fts, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), emp])
            con.commit()
            escrever_log(f"BNDES {emp}: editado {', '.join(s.split('=')[0] for s in sets)}")
        con.close()
        return jsonify({"sucesso": True, "alterados": len(sets)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/bndes/calculo-base")
def api_calculo_base():
    """Repasses do FNAC ao BNDES (PAGAMENTO no tg_execucao_join do processo do contrato), somados por OB."""
    try:
        con = _con()
        obs = {}
        for doc, valor, dt in con.execute("SELECT documento, valor, data FROM tg_execucao_join WHERE processo_sei=? AND etapa='PAGAMENTO' "
                                          "ORDER BY data", (PROCESSO_BNDES,)):
            ob = (re.search(r"\d{4}OB\d{6}", doc or "") or [doc])[0]
            o = obs.setdefault(ob, {"ob": ob, "data": dt, "valor": 0.0})
            o["valor"] += float(valor or 0)
        con.close()
        return jsonify({"sucesso": True, "desembolsos_fnac": list(obs.values())})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


@bp.get("/api/bndes/selic")
def api_selic():
    """Fator Selic acumulado pro rata die de `inicio` (inclusive) a `fim` (exclusive): produto de (1 + taxa diária) da série 11 do BCB.
    Busca no BCB os dias que faltam no cache; sem rede/sem série completa → {disponivel: false} (a tela usa a taxa anual informada)."""
    try:
        ini, fim = _iso(request.args.get("inicio", "")), _iso(request.args.get("fim", ""))
    except ValueError:
        return jsonify({"sucesso": False, "erro": "Datas inválidas."}), 400
    if fim <= ini:
        return jsonify({"sucesso": True, "disponivel": True, "fator": 1.0, "dias_uteis": 0, "fonte": "período vazio"})
    try:
        con = _con()
        def cache():
            return {d: t for d, t in con.execute("SELECT data, taxa_dia FROM bndes_selic WHERE data>=? AND data<?", (ini.isoformat(), fim.isoformat()))}
        tx = cache()
        ultimo = max(tx) if tx else None
        if not ultimo or _iso(ultimo) < min(fim, date.today()) - timedelta(days=4):     # falta pedaço: busca no BCB
            try:
                url = SGS_SELIC.format(ini=ini.strftime("%d/%m/%Y"), fim=(fim - timedelta(days=1)).strftime("%d/%m/%Y"))
                dados = json.loads(urlopen(Request(url, headers={"User-Agent": "RADAR-FNAC"}), timeout=20).read().decode("utf-8"))
                agora = datetime.now().isoformat(timespec="seconds")
                for r in dados:
                    d = datetime.strptime(r["data"], "%d/%m/%Y").date().isoformat()
                    con.execute("INSERT OR REPLACE INTO bndes_selic VALUES (?,?,?)", (d, float(str(r["valor"]).replace(",", ".")) / 100, agora))
                con.commit()
                tx = cache()
            except Exception as exc:  # noqa: BLE001
                escrever_log(f"BNDES selic: BCB indisponível ({exc})")
        con.close()
        if not tx or _iso(max(tx)) < min(fim, date.today()) - timedelta(days=4):
            return jsonify({"sucesso": True, "disponivel": False, "erro": "Série Selic do BCB indisponível para o período."})
        fator = 1.0
        for t in tx.values():
            fator *= 1 + t
        n = len(tx)
        return jsonify({"sucesso": True, "disponivel": True, "fator": fator, "dias_uteis": n,
                        "taxa_aa": (fator ** (252 / n) - 1) * 100 if n else None,
                        "fonte": f"BCB/SGS série 11 (Taxa Selic diária), {n} dias úteis de {ini.strftime('%d/%m/%Y')} a {(fim - timedelta(days=1)).strftime('%d/%m/%Y')}"})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/bndes/empresas/calculo")
def api_calculo_salvar():
    """Guarda só os campos do cálculo que o usuário alterou (os demais seguem o pré-preenchimento)."""
    d = request.get_json(silent=True) or {}
    emp = (d.get("empresa") or "").strip().upper()
    calc = {k: v for k, v in dict(d.get("calc") or {}).items() if v not in (None, "")}
    try:
        con = _con()
        n = con.execute("UPDATE bndes_empresas SET calc_json=?, alterado_em=? WHERE empresa=?",
                        (json.dumps(calc, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), emp)).rowcount
        con.commit()
        con.close()
        if not n:
            return jsonify({"sucesso": False, "erro": "Empresa não encontrada."}), 404
        escrever_log(f"BNDES {emp}: cálculo de retorno gravado ({', '.join(calc) or 'padrão'})")
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/bndes/cronograma/baixa")
def api_baixa():
    d = request.get_json(silent=True) or {}
    emp, parc = (d.get("empresa") or "").strip().upper(), (d.get("parcela") or "").strip()
    if not emp or not parc:
        return jsonify({"sucesso": False, "erro": "Informe empresa e parcela."}), 400
    try:
        con = _con()
        if d.get("pago"):
            con.execute("INSERT OR REPLACE INTO bndes_baixas VALUES (?,?,?,?)",
                        (emp, parc, d.get("valor"), datetime.now().isoformat(timespec="seconds")))
        else:
            con.execute("DELETE FROM bndes_baixas WHERE empresa=? AND parcela=?", (emp, parc))
        con.commit()
        con.close()
        escrever_log(f"BNDES {emp}: parcela {parc} {'marcada como recebida' if d.get('pago') else 'desmarcada'}")
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
