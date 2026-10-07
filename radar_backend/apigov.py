"""Botões GovTED / GovTC e aba INSTRUMENTOS do RADAR.

  POST /api/apigov/<ted|tc>/atualizar   inicia a coleta (em segundo plano) numa pasta de trabalho (dados/APITED ou dados/APITC),
                                        compara com o Instrumentos.db, grava as mudanças (com a data da execução) e guarda o resumo
  GET  /api/apigov/<ted|tc>/status      andamento e, ao final, o resumo para o popup
  GET  /api/apigov/<ted|tc>/historico   execuções anteriores (data, totais)
  GET  /api/apigov/alteracoes           mudanças registradas (filtros: fonte, execucao, instrumento)
  GET  /api/instrumentos/lista          instrumentos do Instrumentos.db (tela INSTRUMENTOS)
  GET  /api/instrumentos/detalhe        um instrumento + últimas alterações vindas do Transferegov
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import collections
from collections import deque
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import APITC_DIR, APITED_DIR, BASE_DIR, INSTRUMENTOS_DB, SCRIPTS_DIR
from radar_backend.radar_util import agora_iso, escrever_log, resposta_erro
from radar_backend import livro_financeiro as L
from radar_backend.arquivos_db import anexar_sei
from radar_backend.tc_gantt_empenho import _combinar_empenhos, _empenhos_tg_execucao_join

bp = Blueprint("apigov", __name__)

FONTES = {
    "ted": {"chave": "TED", "rotulo": "GovTED (API TED)", "script": "extrair_teds_fnac.py", "pasta": APITED_DIR},
    "tc": {"chave": "TC", "rotulo": "GovTC (Transferências Discricionárias e Legais)", "script": "extrair_transferegov_legais.py", "pasta": APITC_DIR},
}
UG = "110591"
_estado: dict[str, dict] = {k: {"estado": "ocioso", "log": [], "resumo": None, "erro": None} for k in FONTES}
_trava = threading.Lock()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
    con.row_factory = sqlite3.Row
    anexar_sei(con)                  # arquivos_pdf (PDF original) fica no SEI.db desde 01/10/2026
    return con


def _executar(fonte: str) -> None:
    cfg = FONTES[fonte]
    est = _estado[fonte]
    log = deque(maxlen=40)
    try:
        cfg["pasta"].mkdir(parents=True, exist_ok=True)
        est.update(fase="coletando", mensagem=f"Consultando a API e gravando em {cfg['pasta'].relative_to(BASE_DIR)}…")
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / cfg["script"]), "--ug", UG, "--saida", str(cfg["pasta"])],
            cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for linha in proc.stdout:  # type: ignore[union-attr]
            log.append(linha.rstrip())
            est["log"] = list(log)
        codigo = proc.wait()
        if codigo != 0:
            raise RuntimeError(f"A coleta terminou com erro (código {codigo}). Últimas linhas: " + " | ".join(list(log)[-3:]))
        est.update(fase="comparando", mensagem="Comparando com o Instrumentos.db…")
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import apigov_diff
        resumo = apigov_diff.comparar_e_aplicar(cfg["chave"], cfg["pasta"], db=INSTRUMENTOS_DB, ug=UG)
        if fonte == "tc":                                   # cláusula suspensiva do SICONV -> cartões (usuário, 03/10/2026)
            try:
                from radar_backend.instrumentos_edicao import suspensivas_do_transferegov
                _c = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
                r_s = suspensivas_do_transferegov(_c)
                _c.close()
                escrever_log(f"API-GOV | suspensivas do Transferegov: {r_s}")
            except Exception as exc:  # noqa: BLE001
                escrever_log(f"ERRO | suspensivas do Transferegov: {exc}")
        est.update(estado="concluido", fase="concluido", resumo=resumo, erro=None, fim=agora_iso(),
                   mensagem=f"Concluído: {resumo['totais']['novos']} novo(s), {resumo['totais']['alterados']} alterado(s), {resumo['totais']['removidos']} removido(s).")
        escrever_log(f"{cfg['chave']} | atualização concluída: {resumo['totais']}")
    except Exception as exc:  # noqa: BLE001
        est.update(estado="erro", fase="erro", erro=f"{type(exc).__name__}: {exc}", fim=agora_iso(), mensagem="Falha na atualização.")
        escrever_log(f"{cfg['chave']} | erro na atualização: {type(exc).__name__}: {exc}")


def gov_ultima_data() -> str | None:
    """Data (AAAA-MM-DD, horário de Brasília) da última coleta CONCLUÍDA do API-GOV que cobriu TED e TC."""
    try:
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
        datas = []
        for fonte in ("TED", "TC"):
            r = con.execute("SELECT MAX(concluido_em) FROM transferegov_execucoes WHERE fonte=?", (fonte,)).fetchone()
            datas.append(r[0] if r else None)
        con.close()
        if not all(datas):
            return None
        from datetime import timezone, timedelta
        def local(t):
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            return (dt.astimezone(timezone(timedelta(hours=-3))) if dt.tzinfo else dt).date().isoformat()
        return min(local(d) for d in datas)
    except Exception:  # noqa: BLE001
        return None


def gov_diario(forcar: bool = False) -> bool:
    """API-GOV automático (usuário, 03/10/2026): ao abrir o RADAR, coleta TED e TC só se ainda não houve coleta HOJE; o botão API_GOV
    continua coletando sempre. Roda em segundo plano, TED e depois TC."""
    if not forcar and gov_ultima_data() == date.today().isoformat():
        escrever_log("API-GOV | ao abrir: já coletado hoje — não coleta de novo")
        return False
    def _seq():
        for fonte in ("ted", "tc"):
            with _trava:
                if _estado[fonte]["estado"] == "rodando":
                    continue
                _estado[fonte] = {"estado": "rodando", "fase": "iniciando", "mensagem": "Coleta diária automática…", "log": [], "resumo": None,
                                  "erro": None, "inicio": agora_iso()}
            _executar(fonte)
        escrever_log("API-GOV | coleta diária automática concluída (TED e TC)")
    threading.Thread(target=_seq, daemon=True).start()
    return True


@bp.post("/api/apigov/<fonte>/atualizar")
def api_apigov_atualizar(fonte: str):
    if fonte not in FONTES:
        return jsonify({"sucesso": False, "erro": "Fonte desconhecida."}), 404
    with _trava:
        if _estado[fonte]["estado"] == "rodando":
            return jsonify({"sucesso": False, "erro": "Já existe uma atualização em andamento."}), 409
        _estado[fonte] = {"estado": "rodando", "fase": "iniciando", "mensagem": "Iniciando…", "log": [], "resumo": None, "erro": None, "inicio": agora_iso()}
    threading.Thread(target=_executar, args=(fonte,), daemon=True).start()
    return jsonify({"sucesso": True, "message": f"{FONTES[fonte]['rotulo']}: atualização iniciada.", "estado": "rodando"})


@bp.get("/api/apigov/<fonte>/status")
def api_apigov_status(fonte: str):
    if fonte not in FONTES:
        return jsonify({"sucesso": False, "erro": "Fonte desconhecida."}), 404
    e = _estado[fonte]
    return jsonify({"sucesso": True, "estado": e["estado"], "fase": e.get("fase"), "mensagem": e.get("mensagem"), "erro": e.get("erro"),
                    "inicio": e.get("inicio"), "fim": e.get("fim"), "log": e.get("log", [])[-8:], "resumo": e.get("resumo")})


@bp.get("/api/apigov/<fonte>/historico")
def api_apigov_historico(fonte: str):
    if fonte not in FONTES:
        return jsonify({"sucesso": False, "erro": "Fonte desconhecida."}), 404
    try:
        limite = max(1, min(int(request.args.get("limite", 30)), 200))
        with _db() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='transferegov_execucoes'").fetchone():
                return jsonify({"sucesso": True, "execucoes": []})
            linhas = [dict(r) for r in con.execute(
                "SELECT id, fonte, iniciado_em, concluido_em, origem, registros_novos, registros_alterados, registros_removidos "
                "FROM transferegov_execucoes WHERE fonte=? ORDER BY id DESC LIMIT ?", (FONTES[fonte]["chave"], limite))]
        return jsonify({"sucesso": True, "execucoes": linhas})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/apigov/alteracoes")
def api_apigov_alteracoes():
    try:
        filtros, args = [], []
        for campo, coluna in (("fonte", "fonte"), ("execucao", "execucao_id"), ("instrumento", "instrumento")):
            v = (request.args.get(campo) or "").strip()
            if v:
                filtros.append(f"{coluna} = ?")
                args.append(v.upper() if campo == "fonte" else v)
        limite = max(1, min(int(request.args.get("limite", 200)), 2000))
        sql = ("SELECT id, execucao_id, executado_em, fonte, tabela, chave, instrumento, referencia, tipo, campo, valor_anterior, valor_novo "
               "FROM transferegov_alteracoes" + (" WHERE " + " AND ".join(filtros) if filtros else "") + " ORDER BY id DESC LIMIT ?")
        with _db() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='transferegov_alteracoes'").fetchone():
                return jsonify({"sucesso": True, "alteracoes": []})
            return jsonify({"sucesso": True, "alteracoes": [dict(r) for r in con.execute(sql, args + [limite])]})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- INSTRUMENTOS
_CAT_MANUAL: dict = {"mtime": None, "mapa": {}}


def categorias_manuais() -> dict:
    """TIPO DE INSTRUMENTO escolhido no ✏️ do cartão (instrumentos_categoria_manual, 01/10/2026) — relido quando o banco muda."""
    try:
        mt = INSTRUMENTOS_DB.stat().st_mtime
        if _CAT_MANUAL["mtime"] != mt:
            con = sqlite3.connect(INSTRUMENTOS_DB, timeout=60)
            try:
                _CAT_MANUAL["mapa"] = dict(con.execute("SELECT processo_sei, categoria FROM instrumentos_categoria_manual"))
            except sqlite3.Error:
                _CAT_MANUAL["mapa"] = {}
            con.close()
            _CAT_MANUAL["mtime"] = mt
    except OSError:
        pass
    return _CAT_MANUAL["mapa"]


_EXEC_CACHE: dict = {"mtime": None, "mapa": {}}
_RE_CNPJ = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")


def _fmt_cnpj(c: str | None) -> str | None:
    d = re.sub(r"\D", "", str(c or ""))
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d) == 14 else (c or None)


# EXECUTORA lida no INSTRUMENTO INICIAL (ou no aditivo/PT que traz o mesmo CNPJ) — pedido do usuário, 03/10/2026: completa só o que está
# VAZIO (nome e/ou CNPJ); o CNPJ já identificado foi conferido no documento (o nome é o que o documento associa a ele). {processo: (cnpj, nome, doc)}
# Último campo opcional True = o CNPJ anterior estava errado e é substituído (AVSEC: era o do próprio Ministério, contratante).
EXECUTORAS_DOCUMENTO = {
    "00055.001524/2015-77": ("00.394.429/0035-50", "Comando da Aeronáutica — Diretoria de Engenharia da Aeronáutica (DIRENG)", "0323984"),
    "00055.001098/2016-52": (None, "Escola de Administração Fazendária (ESAF)", "VOL-00055.001098-2016-52"),
    "00055.001346/2013-12": (None, "Universidade Federal de Santa Catarina (UFSC)", "TC-04-2013-UFSC"),
    "00055.003553/2013-10": (None, "Conselho Nacional de Desenvolvimento Científico e Tecnológico (CNPq)", "CNPQ-SEI-2004194"),
    "50000.000296/2022-51": (None, "ÁGORA PESQUISA LTDA.", "6632642"),
    "50000.003273/2020-36": (None, "Município de Divinópolis/MG", "2461330"),
    "50000.005038/2019-65": (None, "Município de Ponta Grossa/PR", "2123425"),
    "50000.006179/2017-33": (None, "Secretaria de Transportes do Estado do Piauí (SETRANS/PI)", "604785"),
    "50000.006191/2019-18": (None, "Município de Paracatu/MG", "2157116"),
    "50000.006217/2023-04": (None, "Serviço Federal de Processamento de Dados (SERPRO)", "2800943"),
    "50000.007087/2019-32": (None, "Estado da Bahia — Secretaria de Infraestrutura (SEINFRA/BA)", "2015701"),
    "50000.007122/2019-13": (None, "Superintendência de Obras Públicas do Estado do Ceará (SOP/CE)", "3571286"),
    "50000.008123/2019-85": (None, "Departamento de Edificações e Estradas de Rodagem de Minas Gerais (DER/MG)", "4754003"),
    "50000.008130/2019-87": (None, "Município de Barra do Garças/MT", "2145538"),
    "50000.008429/2019-31": (None, "Município de Araxá/MG", "1833000"),
    "50000.008578/2017-39": (None, "Comando da Aeronáutica — Estado-Maior da Aeronáutica (EMAER)", "6608904"),
    "50000.009506/2018-90": (None, "Ministério da Segurança Pública — Secretaria Nacional de Segurança Pública (SENASP)", "TED-01-2018-SENASP"),
    "50000.010346/2018-21": (None, "Município de Sorriso/MT", "7674464"),
    "50000.011952/2018-64": (None, "Instituto Tecnológico de Aeronáutica (ITA)", "3977745"),
    "50000.013045/2017-79": (None, "Secretaria de Transportes do Estado de Pernambuco (SETRA/PE)", "463442"),
    "50000.015107/2020-82": (None, "Município de Caçador/SC", "3560619"),
    "50000.018360/2020-98": (None, "Município de Cascavel/PR", "4220920"),
    "50000.018637/2018-68": (None, "Agência Nacional de Aviação Civil (ANAC)", "TED-04-2018-ANAC"),
    "50000.019123/2019-19": (None, "Estado da Bahia — Secretaria de Infraestrutura (SEINFRA/BA)", "6468961"),
    "50000.020400/2018-47": (None, "Município de Cascavel/PR", "1080774"),
    "50000.022564/2019-90": (None, "Estado de Santa Catarina — Secretaria de Estado da Infraestrutura e Mobilidade (SIE/SC)", "2150737"),
    "50000.023152/2018-96": (None, "Estado de Santa Catarina — Secretaria de Estado da Infraestrutura (SIE/SC)", "1273850"),
    "50000.023155/2018-20": (None, "Estado do Maranhão — Secretaria de Estado de Indústria, Comércio e Energia (SEINC/MA)", "1345762"),
    "50000.023157/2018-19": (None, "Município de Santa Maria/RS", "2155935"),
    "50000.023223/2020-75": (None, "Departamento Nacional de Infraestrutura de Transportes (DNIT)", "2745556"),
    "50000.023548/2017-52": (None, "Agência Nacional de Aviação Civil (ANAC)", "TED-04-2017-ANAC"),
    "50000.023721/2020-18": (None, "Estado de Rondônia — Departamento Estadual de Estradas de Rodagem, Infraestrutura e Serviços Públicos (DER/RO)", "6623519"),
    "50000.025244/2017-20": (None, "Secretaria dos Transportes do Estado do Rio Grande do Sul (ST/RS)", "719283"),
    "50000.025309/2018-18": ("08.838.143/0001-89", "Secretaria de Logística e Transportes do Rio Grande do Sul (SELT/RS)", "2157784"),
    "50000.025974/2017-21": (None, "Secretaria de Infraestrutura e Logística do Estado de Mato Grosso (SINFRA/MT)", "752979"),
    "50000.025977/2017-64": (None, "Secretaria de Infraestrutura e Logística do Estado de Mato Grosso (SINFRA/MT)", "752714"),
    "50000.027762/2019-40": (None, "PRAXIAN CONSULTORIA LTDA", "3522344"),
    "50000.031090/2020-19": (None, "Estado de Rondônia — DER/RO", "3566650"),
    "50000.032188/2018-61": (None, "Universidade Federal de São Carlos (UFSCar)", "TED-03-2018-UFSCAR"),
    "50000.034273/2018-63": ("00.394.429/0001-00", "Comando da Aeronáutica (COMAER)", "1300960"),
    "50000.035020/2017-26": (None, "Município de Jataí/GO", "752913"),
    "50000.035084/2017-27": (None, "Agência Municipal de Transportes e Trânsito de Araguaína (AMTT/TO)", "752794"),
    "50000.035101/2017-26": (None, "Departamento de Estradas de Rodagem, Infraestrutura e Serviços Públicos (DER/RO)", "719150"),
    "50000.035108/2017-48": (None, "Departamento de Estradas de Rodagem, Infraestrutura e Serviços Públicos (DER/RO)", "719106"),
    "50000.035408/2017-27": (None, "Estado de Pernambuco — Secretaria de Infraestrutura e Recursos Hídricos (SEINFRA/PE)", "4226736"),
    "50000.039795/2018-51": (None, "Estado da Bahia — Secretaria de Infraestrutura (SEINFRA/BA)", "1295308"),
    "50000.039923/2017-86": (None, "Município de Governador Valadares/MG", "719221"),
    "50000.040010/2019-74": (None, "Comissão de Aeroportos da Região Amazônica (COMARA)", "2574940"),
    "50000.040436/2017-66": (None, "Secretaria de Estado de Transportes do Rio de Janeiro (SETRANS/RJ)", "4888343"),
    "50000.040574/2019-15": (None, "Estado de Santa Catarina — Secretaria de Infraestrutura e Mobilidade (SIE/SC)", "2939246"),
    "50000.041740/2019-92": (None, "Município de Caxias do Sul/RS", "2108883"),
    "50000.042503/2019-49": (None, "Estado da Paraíba — Secretaria de Estado de Infraestrutura, dos Recursos Hídricos e do Meio Ambiente (SEIRHMA/PB)", "2155513"),
    "50000.047370/2017-35": ("19.892.624/0001-99", "NUCTECH DO BRASIL LTDA (CT 20/2018; os pórticos são da DETRONIX)", "CT-20-2018-NUCTECH", True),
    "50000.047516/2017-42": (None, "9º Batalhão de Engenharia de Construção (9º BEC — Exército)", "755002"),
    "50000.047771/2017-95": (None, "Secretaria de Estado de Transporte e Desenvolvimento Urbano de Alagoas (SETRAND/AL)", "TC-01-2018-MARAGOGI"),
    "50000.049439/2017-65": (None, "Universidade Federal de Santa Catarina (UFSC)", "TED-01-2018-UFSC"),
    "50000.052483/2019-14": (None, "Município de Rio Verde/GO", "2466938"),
    "50000.054531/2019-17": (None, "Estado de Mato Grosso do Sul — Secretaria de Estado de Infraestrutura (SEINFRA/MS)", "2155880"),
    "50000.054853/2019-58": (None, "Município de Guanambi/BA", "5796190"),
    "50000.060801/2019-11": (None, "Estado do Piauí — Secretaria de Transportes (SETRANS/PI)", "2442880"),
    "50000.060980/2019-96": (None, "Estado do Espírito Santo — Secretaria de Estado de Mobilidade e Infraestrutura (SEMOBI/ES)", "2157432"),
    "50000.066804/2019-68": (None, "Companhia de Desenvolvimento dos Vales do São Francisco e do Parnaíba (CODEVASF)", "2123504"),
    "50000.066930/2019-12": (None, "Secretaria Especial da Receita Federal do Brasil (RFB)", "2356054"),
    "50020.002645/2026-64": (None, "Serviço Federal de Processamento de Dados (SERPRO)", "11828971"),
    "50020.005708/2023-91": (None, "Instituto Tecnológico de Aeronáutica (ITA) — UG Grupamento de Apoio de São José dos Campos (GAP-SJ)", "7847663"),
    "50020.006853/2025-51": (None, "Banco Nacional de Desenvolvimento Econômico e Social (BNDES)", "10706996"),
    "71000.000214/2018-61": (None, "Município de Santa Rosa/RS", "1143312"),
    "SEM-PROCESSO/FRAPORT": (None, "FRAPORT BRASIL S.A. — Aeroporto de Porto Alegre", "8923403"),
    # TEDs do Módulo TED (só nome): CNPJ do mesmo órgão em outro documento da base
    "50000.008875/2022-41": ("83.899.526/0001-82", None, "TED-01-2018-UFSC (mesmo órgão)"),
    "50020.007135/2025-01": ("83.899.526/0001-82", None, "TED-01-2018-UFSC (mesmo órgão)"),
    "50020.004363/2023-59": ("45.358.058/0001-40", None, "TED-03-2018-UFSCAR (mesmo órgão)"),
    "50020.008564/2024-14": ("00.394.429/0144-03", None, "3977745 (ITA, mesmo órgão)"),
}


# Executora que MUDOU ao longo do instrumento (usuário, 03/10/2026: "liste todas, em linhas separadas"): da mais antiga à atual.
# Conferido no documento do instrumento inicial x aditivo/SICONV. {processo: [(cnpj, nome, documento, ato)]}
EXECUTORAS_HISTORICO = {
    "50000.035408/2017-27": [("01.171.481/0001-60", "Secretaria de Transportes do Estado de Pernambuco (SETRA/PE)", "1306995", "Termo de Compromisso"),
                             ("32.535.558/0001-68", "Secretaria de Infraestrutura e Recursos Hídricos de Pernambuco (SEINFRA/PE)", "4226736", "termo aditivo")],
    "50000.040436/2017-66": [("42.498.600/0001-71", "Governo do Estado do Rio de Janeiro", "764825", "Termo de Compromisso"),
                             ("42.498.667/0001-06", "Secretaria de Estado de Transportes do Rio de Janeiro (SETRANS/RJ)", "4888343", "termo aditivo")],
    "50000.047516/2017-42": [("07.521.315/0001-23", "Comando do Exército — Departamento de Engenharia e Construção (DEC)", "754978", "TED"),
                             ("07.529.010/0001-68", "9º Batalhão de Engenharia de Construção (9º BEC — Exército)", "755002", "Plano de Trabalho / unidade executora")],
    "50000.008578/2017-39": [("00.394.429/0001-00", "Comando da Aeronáutica (COMAER)", "587755", "TED"),
                             ("00.394.429/0054-12", "Comando da Aeronáutica — Estado-Maior da Aeronáutica (EMAER)", "6608904", "termo aditivo")],
    "00055.001643/2016-19": [("47.693.643/0001-21", "Departamento Aeroviário do Estado de São Paulo (DAESP)", "TC-03-2017-RIBEIRAO", "Termo de Compromisso"),
                             ("46.379.400/0001-50", "Departamento Aeroviário do Estado de São Paulo (DAESP) — CNPJ do proponente no SICONV", "SICONV", "SICONV (atual)")],
}


def executoras(con: sqlite3.Connection) -> dict:
    """EXECUTORA de cada instrumento (CNPJ e nome) — popup do título do DETALHE (usuário, 01/10/2026). Prioridade:
    1) informado pelo usuário (instrumentos_executora); 2) SICONV: proponente do convênio/TC (convenio -> proposta); 3) Módulo TED: unidade
    responsável pela execução (nome; o Módulo não traz CNPJ); 4) CNPJ mais citado nos documentos SEI do processo, sem os CNPJs de órgãos
    federais que aparecem em 5+ processos (concedente etc.). Conferido em 01/10/2026: documento = SICONV em 21 de 23 casos (nos 2 outros o
    documento cita o município e o SICONV o Estado proponente — por isso o SICONV vem antes)."""
    mt = INSTRUMENTOS_DB.stat().st_mtime
    if _EXEC_CACHE["mtime"] == mt:
        return _EXEC_CACHE["mapa"]
    out: dict = {}
    por_proc: dict = {}
    procs_de: dict = {}
    for p, t in con.execute("SELECT processo_sei, texto_completo FROM documentos WHERE texto_completo LIKE '%CNPJ%' AND processo_sei IS NOT NULL"):
        cs = _RE_CNPJ.findall(t or "")
        cnt = por_proc.setdefault(p, {})
        for c in cs:
            cnt[c] = cnt.get(c, 0) + 1
        for c in set(cs):
            procs_de.setdefault(c, set()).add(p)
    globais = {c for c, ps in procs_de.items() if len(ps) >= 5}
    for p, cnt in por_proc.items():
        cand = sorted(((n, c) for c, n in cnt.items() if c not in globais), reverse=True)
        if cand:
            out[p] = {"cnpj": cand[0][1], "nome": None, "fonte": "documentos SEI do processo (CNPJ mais citado)"}
    prop = {}
    try:
        prop = {str(r[0]).strip(): (r[1], r[2]) for r in con.execute(
            "SELECT c.nr_convenio, p.identif_proponente, p.nm_proponente FROM transferegov_siconv_convenio c JOIN transferegov_siconv_proposta p USING (id_proposta)")}
    except sqlite3.Error:
        pass
    for r in con.execute("SELECT processo_sei, numero_siafi FROM instrumentos").fetchall():
        v = _vinculos(con, r[0], r[1])
        n = str((v.get("tc") or {}).get("nr_convenio") or "").strip()
        if n in prop:
            out[r[0]] = {"cnpj": _fmt_cnpj(prop[n][0]), "nome": prop[n][1], "fonte": f"SICONV (proponente do convênio {n})"}
        elif v.get("ted"):
            u = con.execute("SELECT unidade_responsavel_execucao, sigla_unidade_responsavel_execucao FROM transferegov_ted_plano_acao WHERE id_plano_acao=?",
                            (v["ted"]["id_plano_acao"],)).fetchone()
            if u and u[0]:
                base = out.get(r[0]) or {"cnpj": None}
                out[r[0]] = {"cnpj": base.get("cnpj"), "nome": f"{u[0]}" + (f" ({u[1]})" if u[1] else ""),
                             "fonte": "Módulo TED (unidade executora)" + (" + CNPJ dos documentos SEI" if base.get("cnpj") else "")}
    for p, ent in EXECUTORAS_DOCUMENTO.items():                  # executora lida no instrumento inicial: completa só o vazio
        c, nm, doc = ent[:3]
        base = dict(out.get(p) or {"cnpj": None, "nome": None, "fonte": None})
        trocar = len(ent) > 3 and ent[3]
        mudou = []
        if c and (trocar or not base.get("cnpj")):
            base["cnpj"] = c; mudou.append("CNPJ")
        if nm and not base.get("nome"):
            base["nome"] = nm; mudou.append("nome")
        if mudou:
            base["fonte"] = "; ".join(x for x in (base.get("fonte"), f"{' e '.join(mudou)} do documento SEI {doc}") if x)
            out[p] = base
    try:
        tem_fonte = "fonte" in {r[1] for r in con.execute("PRAGMA table_info(instrumentos_executora)")}
        for p, c, nm, fo in con.execute("SELECT processo_sei, cnpj, nome, " + ("fonte" if tem_fonte else "NULL") + " FROM instrumentos_executora"):
            base = out.get(p) or {}
            out[p] = {"cnpj": _fmt_cnpj(c) if c else base.get("cnpj"), "nome": nm if nm else base.get("nome"), "fonte": fo or "informado pelo usuário"}
    except sqlite3.Error:
        pass
    for p, hist in EXECUTORAS_HISTORICO.items():                # executoras anteriores (uma linha cada na ficha)
        out.setdefault(p, {"cnpj": hist[-1][0], "nome": hist[-1][1], "fonte": f"documento SEI {hist[-1][2]}"})
        out[p]["historico"] = [{"cnpj": c, "nome": n, "documento": d, "ato": a} for c, n, d, a in hist]
    _EXEC_CACHE.update(mtime=mt, mapa=out)
    return out


def _categoria(tipo: str | None, processo: str | None = None) -> str:
    if processo and processo in categorias_manuais():
        return categorias_manuais()[processo]
    t = (tipo or "").strip().upper()
    if t.startswith("TED"):
        return "TED"
    if t.startswith("TC"):
        return "TC"
    if t.startswith(("CT", "CONTRATO")):
        return "CONTRATO"
    if t.startswith(("CV", "CONV")):
        return "CONVÊNIO"
    return "OUTROS"


def _status(situacao: str | None, vigencia: str | None, hoje: str) -> str:
    s = (situacao or "").strip().upper()
    if s.startswith("ENCERRADO") or s == "RESCINDIDO":
        return "ENCERRADO"
    if s == "VIGENTE" and (not vigencia or vigencia >= hoje):      # vigente por decisão do usuário (ex. contrato novo ainda sem documento)
        return "VIGENTE"
    if vigencia and vigencia >= hoje:
        return "VIGENTE"
    if vigencia:
        return "VIGÊNCIA VENCIDA"
    return "SEM VIGÊNCIA"


def _valor_enviado(con: sqlite3.Connection, vinc: dict) -> tuple[float | None, float | None]:
    """Recursos já enviados ao executor, por DUAS fontes independentes (para comparar):
    - TRF do Módulo TED (transferegov_ted_trf): o que foi efetivamente transferido, ligado pelo id_plano_acao;
    - PF alocada no cronograma (transferegov_progresso_alocacao_cronograma_pf): o valor programado/alocado por
      Programação Financeira contra o cronograma de desembolso do TED, ligado pelo numero_ted.
    Para Convênio/TC (sem PF/TRF no Módulo TED) usa o desembolso do SICONV como única fonte (retorna só o 1º valor).
    Retorna (valor_trf_ou_desembolso, valor_pf) — None quando não há vínculo/dado (não é zero)."""
    if vinc.get("ted"):
        trf = con.execute(
            "SELECT COALESCE(SUM(t.vl_valor_trf), 0) FROM transferegov_ted_trf t "   # IN, não JOIN: id_programacao se repete
            "WHERE t.id_programacao IN (SELECT id_programacao FROM transferegov_ted_programacao_financeira WHERE id_plano_acao=?)",
            (vinc["ted"]["id_plano_acao"],)).fetchone()[0]
        # PF não enviada não conta; EXISTS (não JOIN): a tabela de programação repete id_programacao
        # (16.906 linhas p/ 16.516 ids) e o JOIN dobrava a PF (TED 939156: 57,9 -> 115,8 mi)
        pf = con.execute("SELECT COALESCE(SUM(a.valor), 0) FROM transferegov_progresso_alocacao_cronograma_pf a "
                         "WHERE a.numero_ted=? AND EXISTS (SELECT 1 FROM transferegov_ted_programacao_financeira p "
                         "WHERE p.id_programacao = CAST(a.id_programacao AS INTEGER) AND p.tx_situacao_programacao='ENVIADA')",
                          (vinc["ted"]["numero_ted"],)).fetchone()[0]
        return float(trf or 0), float(pf or 0)
    if vinc.get("processo_ted_sem_modulo"):
        # TED fora do Módulo TED: PF líquida (entradas - estornos) das telas CONTRANSF do SIAFI guardadas no banco
        try:
            pf = con.execute("SELECT SUM(valor) FROM siafi_pf_liberadas WHERE processo_sei=?", (vinc["processo_ted_sem_modulo"],)).fetchone()[0]
        except sqlite3.Error:
            pf = None
        return None, (float(pf) if pf is not None else None)
    if vinc.get("tc"):
        r = con.execute("SELECT COALESCE(SUM(vl_desembolsado), 0) FROM transferegov_siconv_desembolso WHERE trim(nr_convenio)=?",
                         (vinc["tc"]["nr_convenio"],)).fetchone()[0]
        return float(r or 0), None
    return None, None


@bp.get("/api/instrumentos/lista")
def api_instrumentos_lista():
    try:
        hoje = date.today().isoformat()
        with _db() as con:
            linhas = con.execute(
                "SELECT processo_sei, tipo_instrumento, numero_instrumento, numero_siafi, localidades, regioes, situacao, vigencia_mais_futura, prazo_execucao_fim, "
                "n_aditivos_total, valor_atual, valor_total, contrapartida, data_assinatura_instrumento, substr(objeto, 1, 240) AS objeto "
                "FROM instrumentos ORDER BY processo_sei").fetchall()
            from radar_backend.aeroportos import (aeroportos_do_instrumento, aplicar_edicoes, aplicar_vinculos, atribuir_chaves, completar_valor, etapa, marcar_nacional,
                                                  mapas_de_apoio, todas_edicoes, todos_vinculos)
            from radar_backend.instrumentos_edicao import garantir_tabelas, pt_ocultos
            garantir_tabelas(con)
            pt_oculto = pt_ocultos(con)                       # tick PLANO DE TRABALHO / METAS/ETAPAS desmarcado na ficha (03/10/2026)
            from radar_backend.instrumentos_edicao import suspensivas as _susp
            suspensiva = _susp(con)                           # CONDIÇÃO SUSPENSIVA (03/10/2026)
            apoio = mapas_de_apoio()
            manual_etapa = dict(con.execute("SELECT processo_sei, etapa FROM instrumentos_etapa_manual").fetchall())
            ed_aeros = todas_edicoes(con)
            vinc_aeros = todos_vinculos(con)                  # aeroportos incluídos/retirados pelo usuário (N:N, 01/10/2026)
            con.execute("CREATE TABLE IF NOT EXISTS instrumentos_executora (processo_sei TEXT PRIMARY KEY, cnpj TEXT, nome TEXT, alterado_em TEXT)")
            mapa_exec = executoras(con)                   # edições do DETALHE do aeroporto (popup do título, 01/10/2026)
            prazo_situacao = dict(con.execute("SELECT processo_sei, data_estimada FROM instrumentos_situacao_prazo"))
            numero_gov = dict(con.execute("SELECT processo_sei, numero_gov FROM instrumentos_numero_gov"))
            con.execute("CREATE TABLE IF NOT EXISTS instrumentos_nome_cartao (processo_sei TEXT PRIMARY KEY, nome TEXT, alterado_em TEXT)")
            nome_cartao = dict(con.execute("SELECT processo_sei, nome FROM instrumentos_nome_cartao"))
            con.execute("CREATE TABLE IF NOT EXISTS instrumentos_processo_informado (processo_sei TEXT PRIMARY KEY, processo TEXT, alterado_em TEXT)")   # PROCESSO informado na ficha p/ instrumento sem processo (02/10/2026)
            processo_informado = dict(con.execute("SELECT processo_sei, processo FROM instrumentos_processo_informado"))
            gestores_radar: dict = {}
            for p_, nome_ in con.execute("SELECT processo_sei, nome FROM instrumentos_gestores_radar ORDER BY nome"):
                gestores_radar.setdefault(p_, []).append(nome_)
            # último ano com movimento financeiro (empenho, OB, PF/TRF, NC) — para instrumentos sem data de assinatura
            ultimo_mov = {}
            for p_, doc_, dt_ in con.execute("SELECT processo_sei, documento, data FROM tg_execucao_join WHERE etapa IN "
                                             "('EMPENHO','PAGAMENTO','PF','PF_TG','TRF','CREDITO')"):
                m_ = re.match(r"(20\d\d)", str(dt_ or "")) or re.search(r"(20\d\d)(?:NE|OB|PF|NC)", str(doc_ or ""))
                if m_:
                    ultimo_mov[p_] = max(ultimo_mov.get(p_, 0), int(m_.group(1)))
            excluidos = {r[0] for r in con.execute("SELECT processo_sei FROM instrumentos_excluidos")}
            itens = []
            for r in linhas:
                d = dict(r)
                if d["processo_sei"] in excluidos:          # lixeira do cartão (usuário, 30/09/2026): fica no banco, sai do RADAR
                    continue
                d["categoria"] = _categoria(d["tipo_instrumento"], d["processo_sei"])
                d["status"] = _status(d["situacao"], d["vigencia_mais_futura"], hoje)
                # CONCLUÍDO / EM EXECUÇÃO / PREPARATÓRIO (usuário, 30/09/2026) + aeroportos com "AEROPORTO / UF - ICAO" (COMARA > DINV > cadastro)
                d["etapa"] = manual_etapa.get(d["processo_sei"]) or etapa(d["situacao"], d["data_assinatura_instrumento"], d["vigencia_mais_futura"], hoje,
                                                                       ultimo_mov.get(d["processo_sei"]))
                d["etapa_manual"] = d["processo_sei"] in manual_etapa
                d["gestores_radar"] = gestores_radar.get(d["processo_sei"], [])
                d["pt_oculto"] = pt_oculto.get(d["processo_sei"], [])
                d["suspensiva"] = suspensiva.get(d["processo_sei"])
                d["situacao_data_estimada"] = prazo_situacao.get(d["processo_sei"])
                d["numero_gov"] = numero_gov.get(d["processo_sei"])      # NÚMERO GOV (Transferegov), 02/10/2026
                d["executora"] = mapa_exec.get(d["processo_sei"])                    # CNPJ / nome da executora (popup do título, 01/10/2026)  # DATA ESTIMADA da situação registrada (01/10/2026)     # GESTOR RADAR (filtro GESTOR_RADAR, 01/10/2026)
                d["vencida"] = d["status"] == "VIGÊNCIA VENCIDA"
                d["uf"] = d["regioes"]
                d["valor"] = d["valor_atual"] or d["valor_total"]
                d["uniao"] = (d["valor"] - (d["contrapartida"] or 0)) if d["valor"] else None
                d["aeroportos"] = aeroportos_do_instrumento(d, d["etapa"], apoio)
                atribuir_chaves(d["aeroportos"])                  # chave da FONTE (antes da edição); linha repetida = "ICAO#2"
                d["aeroportos"] = aplicar_vinculos(d["processo_sei"], d["aeroportos"], vinc_aeros, apoio[0], d["etapa"])
                aplicar_edicoes(con, d["processo_sei"], d["aeroportos"], ed_aeros)
                marcar_nacional(d["aeroportos"])
                completar_valor(d["aeroportos"], d["valor"])
                n_a = len(d["aeroportos"])
                d["linha_aeroporto"] = d["aeroportos"][0]["linha"] if n_a == 1 else f"{n_a} AEROPORTOS" if n_a else ""
                if n_a and all(x.get("nacional") for x in d["aeroportos"]):
                    # SAC só no MAPA (usuário, 01/10/2026): no KANBAN o título é o nome do instrumento (ex.: UFSCAR), tirado da Localidade
                    nome_ = re.split(r"\s+-\s+|\s*\(", (d.get("localidades") or "").split("|")[0])[0].strip()
                    d["linha_aeroporto"] = (nome_ or d.get("tipo_instrumento") or "").upper()
                if d.get("categoria") == "OUTROS" and d.get("tipo_instrumento"):
                    # KANBAN trata de INSTRUMENTOS (usuário, 02/10/2026): nos OUTROS (Inframerica, Obras Infraero, aportes, Fraport, BNDES…)
                    # o título é o nome do instrumento, não o aeroporto (São Gonçalo) nem "N AEROPORTOS"
                    d["linha_aeroporto"] = d["tipo_instrumento"].upper()
                d["linha_aeroporto_auto"] = d["linha_aeroporto"]
                d["nome_cartao"] = nome_cartao.get(d["processo_sei"])
                d["processo_informado"] = processo_informado.get(d["processo_sei"])
                if d["nome_cartao"]:
                    d["linha_aeroporto"] = d["nome_cartao"]       # NOME NO CARTÃO definido pelo usuário (✏️ da ficha, 02/10/2026)
                d["rotulo"] = " — ".join(x for x in (d["tipo_instrumento"], d["localidades"]) if x)
                d["valor_enviado_trf"], d["percentual_trf"], d["valor_enviado_pf"], d["percentual_pf"] = None, None, None, None
                d["valor_empenhado"], d["percentual_empenho"] = None, None
                if d["status"] == "VIGENTE":                              # só calcula execução para quem ainda está em andamento
                    vinc = _vinculos(con, d["processo_sei"], d.get("numero_siafi"))
                    if d["categoria"] == "TED" and not vinc.get("ted"):
                        vinc = dict(vinc, processo_ted_sem_modulo=d["processo_sei"])
                    trf, pf = _valor_enviado(con, vinc)
                    total = d.get("valor_atual") or d.get("valor_total")
                    d["valor_enviado_trf"], d["valor_enviado_pf"] = trf, pf
                    if total:
                        if trf is not None:
                            d["percentual_trf"] = round(trf / total * 100, 1)
                        if pf is not None:
                            d["percentual_pf"] = round(pf / total * 100, 1)
                    if d["categoria"] in ("TC", "CONVÊNIO"):
                        # TC/Convênio: execução medida por EMPENHO (Tesouro Gerencial + SICONV, sem duplicar NE — mesma regra do Gantt).
                        # CONTRATO fica sem percentual (decisão do usuário, 24/09/2026): renovável ano a ano, o empenho acumulado passa
                        # de 100% e não tem sentido como execução — o cartão mostra só o valor do contrato.
                        siafi = (vinc.get("tc") or {}).get("nr_convenio") or d.get("numero_siafi") or ""
                        d["valor_empenhado"] = L.total(L.livro_empenho(con, d["processo_sei"], str(siafi).strip()))
                        if total:
                            d["percentual_empenho"] = round(d["valor_empenhado"] / total * 100, 1)
                itens.append(d)
        categorias = sorted({i["categoria"] for i in itens})
        return jsonify({"sucesso": True, "hoje": hoje, "categorias": categorias, "instrumentos": itens})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _digs(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _vinculos(con: sqlite3.Connection, processo: str, siafi: str | None) -> dict:
    """Liga o instrumento do Cadastro ao TED (transferegov_ted_*) e ao convênio/TC (transferegov_siconv_*), por processo SEI ou nº SIAFI."""
    ted = tc = None
    dp, ds = _digs(processo), _digs(siafi)
    try:
        for r in con.execute("SELECT a.id_plano_acao, a.sq_instrumento, a.aa_instrumento, tm.tx_num_processo_sei AS proc FROM transferegov_ted_plano_acao a "
                             "LEFT JOIN transferegov_ted_termo_execucao tm USING (id_plano_acao)"):
            # processo sem dígitos (SEM-PROCESSO/..., ESTRUTURACAO-...) não casa com TED/convênio de processo sem dígitos (04/10/2026)
            if (dp and r["proc"] and _digs(r["proc"]) == dp) or (ds and _digs(r["sq_instrumento"]) == ds):
                ted = {"numero_ted": str(r["sq_instrumento"]), "id_plano_acao": r["id_plano_acao"], "rotulo": f"TED {r['sq_instrumento']}/{r['aa_instrumento']}"}
                break
    except sqlite3.Error:
        pass
    try:
        for r in con.execute("SELECT nr_convenio, nr_processo, ano FROM transferegov_siconv_convenio"):
            if (dp and r["nr_processo"] and _digs(r["nr_processo"]) == dp) or (ds and _digs(r["nr_convenio"]) == ds):
                tc = {"nr_convenio": str(r["nr_convenio"]).strip(), "rotulo": f"Convênio/TC {str(r['nr_convenio']).strip()}"}
                break
    except sqlite3.Error:
        pass
    return {"ted": ted, "tc": tc}


def _tabela(con: sqlite3.Connection, titulo: str, sql: str, args: tuple, colunas: list[tuple[str, str]], total: str | None = None) -> dict:
    """Seção genérica {titulo, colunas[{campo, rotulo}], linhas[dict], total}."""
    linhas = [dict(r) for r in con.execute(sql, args)]
    sec = {"titulo": titulo, "colunas": [{"campo": c, "rotulo": r} for c, r in colunas], "linhas": linhas}
    if total:
        def num(v):
            try:
                return float(str(v).replace(",", "."))
            except (TypeError, ValueError):
                return 0.0
        sec["total"] = {"campo": total, "valor": sum(num(l.get(total)) for l in linhas)}
    return sec


@bp.get("/api/instrumentos/tc/detalhes")
def api_instrumentos_tc_detalhes():
    nr = (request.args.get("convenio") or "").strip()
    tipo = (request.args.get("tipo") or "").strip()
    if not nr or tipo not in ("pf", "cronograma", "metas_etapas"):
        return jsonify({"sucesso": False, "erro": "Informe convenio e tipo (pf, cronograma, metas_etapas)."}), 400
    try:
        T = "transferegov_siconv_"
        with _db() as con:
            conv = con.execute(f"SELECT * FROM {T}convenio WHERE trim(nr_convenio)=?", (nr,)).fetchone()
            if conv is None:
                return jsonify({"sucesso": False, "erro": "Convênio não encontrado na base."}), 404
            resumo = {"situacao": conv["sit_convenio"], "inicio_vigencia": conv["dia_inic_vigenc_conv"], "fim_vigencia": conv["dia_fim_vigenc_conv"],
                      "valor_global": conv["vl_global_conv"], "valor_repasse": conv["vl_repasse_conv"], "valor_contrapartida": conv["vl_contrapartida_conv"]}
            if tipo == "pf":
                # Livro de movimentos por etapa (radar_backend/livro_financeiro.py): cada documento como
                # Entrada/Saída, fontes complementares sem repetição, não utilizados listados com o motivo
                # e fora do total — mesma base do card, do gráfico e do Gantt (24/09/2026).
                proc_row = con.execute("SELECT processo_sei FROM instrumentos WHERE trim(numero_siafi)=?", (nr,)).fetchone()
                processo_sei = proc_row["processo_sei"] if proc_row else None
                colunas = [{"campo": "documento", "rotulo": "Documento"}, {"campo": "data", "rotulo": "Data"},
                           {"campo": "movimento", "rotulo": "Movimento"}, {"campo": "situacao", "rotulo": "Situação"},
                           {"campo": "fonte", "rotulo": "Fonte"}, {"campo": "valor", "rotulo": "Valor"}]

                def livro(titulo, linhas):
                    return {"titulo": titulo, "colunas": colunas, "linhas": linhas, "livro": True,
                            "total": {"campo": "valor", "valor": L.total(linhas)},
                            "n_nao_utilizados": sum(1 for l in linhas if not l["considerado"])}
                secoes = [
                    livro("Empenhos", L.livro_empenho(con, processo_sei, nr) if processo_sei else []),
                    livro("Desembolsos", L.livro_desembolso(con, processo_sei, nr) if processo_sei else []),
                    livro("Pagamentos", L.livro_pagamento(con, processo_sei, nr) if processo_sei else []),
                    _tabela(con, "Ingresso de contrapartida", f"SELECT * FROM {T}ingresso_contrapartida WHERE trim(nr_convenio)=? ORDER BY dt_ingresso_contrapartida", (nr,),
                            [("dt_ingresso_contrapartida", "Data"), ("vl_ingresso_contrapartida", "Valor")], "vl_ingresso_contrapartida"),
                ]
                titulo = "Execução financeira (empenho, desembolso e pagamento)"
            elif tipo == "cronograma":
                secoes = [_tabela(con, "Cronograma de desembolso (previsto)", f"SELECT * FROM {T}cronograma_desembolso WHERE trim(nr_convenio)=? "
                                  "ORDER BY CAST(ano_crono_desembolso AS INTEGER), CAST(mes_crono_desembolso AS INTEGER), tipo_resp_crono_desembolso", (nr,),
                                  [("nr_parcela_crono_desembolso", "Parcela"), ("mes_crono_desembolso", "Mês"), ("ano_crono_desembolso", "Ano"), ("tipo_resp_crono_desembolso", "Responsável"),
                                   ("valor_parcela_crono_desembolso", "Valor")], "valor_parcela_crono_desembolso"),
                          _tabela(con, "Desembolsos realizados", f"SELECT * FROM {T}desembolso WHERE trim(nr_convenio)=? ORDER BY data_desembolso", (nr,),
                                  [("data_desembolso", "Data"), ("vl_desembolsado", "Valor")], "vl_desembolsado")]
                titulo = "Cronograma de desembolso"
            else:
                secoes = [_tabela(con, "Metas", f"SELECT * FROM {T}meta_crono_fisico WHERE trim(nr_convenio)=? ORDER BY CAST(nr_meta AS INTEGER)", (nr,),
                                  [("nr_meta", "Nº"), ("desc_meta", "Descrição"), ("data_inicio_meta", "Início"), ("data_fim_meta", "Fim"), ("qtd_meta", "Qtd."), ("und_fornecimento_meta", "Unid."), ("vl_meta", "Valor")], "vl_meta"),
                          _tabela(con, "Etapas", f"SELECT e.*, m.nr_meta FROM {T}etapa_crono_fisico e JOIN {T}meta_crono_fisico m ON m.id_meta=e.id_meta WHERE trim(m.nr_convenio)=? "
                                  "ORDER BY CAST(m.nr_meta AS INTEGER), CAST(e.nr_etapa AS INTEGER)", (nr,),
                                  [("nr_meta", "Meta"), ("nr_etapa", "Etapa"), ("desc_etapa", "Descrição"), ("data_inicio_etapa", "Início"), ("data_fim_etapa", "Fim"), ("qtd_etapa", "Qtd."), ("vl_etapa", "Valor")], "vl_etapa")]
                titulo = "Metas e etapas (cronograma físico)"
        return jsonify({"sucesso": True, "convenio": nr, "tipo": tipo, "titulo": titulo, "resumo": resumo, "secoes": secoes})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _iso(v) -> str | None:
    """'2024-03-15...' | '15/03/2024' -> '2024-03-15'"""
    t = str(v or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", t)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _data_gov(con: sqlite3.Connection, vinc: dict) -> dict | None:
    """Data da versão do GOV = data do último ato: TED = maior entre assinatura/efetivação do termo e pareceres do plano;
    TC/convênio = assinatura do último termo aditivo (ou do convênio). Devolve {origem, data, ato} da fonte mais recente."""
    cands = []
    try:
        if vinc.get("ted"):
            pid = vinc["ted"]["id_plano_acao"]
            datas = []
            for r in con.execute("SELECT dt_assinatura_termo, dt_efetivacao_termo FROM transferegov_ted_termo_execucao WHERE id_plano_acao=?", (pid,)):
                datas += [(_iso(r[0]), "assinatura do termo"), (_iso(r[1]), "efetivação do termo")]
            for r in con.execute("SELECT dt_data_parecer FROM transferegov_ted_plano_acao_parecer WHERE id_plano_acao=?", (pid,)):
                datas.append((_iso(r[0]), "parecer do plano de ação"))
            datas = [d for d in datas if d[0]]
            if datas:
                d, ato = max(datas)
                cands.append({"origem": "TED", "data": d, "ato": ato})
    except sqlite3.Error:
        pass
    try:
        if vinc.get("tc"):
            nr = vinc["tc"]["nr_convenio"]
            datas = [(_iso(r[0]), f"termo aditivo {r[1]}") for r in con.execute(
                "SELECT dt_assinatura_ta, numero_ta FROM transferegov_siconv_termo_aditivo WHERE trim(nr_convenio)=?", (nr,))]
            datas += [(_iso(r[0]), "assinatura do convênio") for r in con.execute("SELECT dia_assin_conv FROM transferegov_siconv_convenio WHERE trim(nr_convenio)=?", (nr,))]
            datas = [d for d in datas if d[0]]
            if datas:
                d, ato = max(datas)
                cands.append({"origem": "TC", "data": d, "ato": ato})
    except sqlite3.Error:
        pass
    return max(cands, key=lambda c: c["data"]) if cands else None


def _versoes_sei(con: sqlite3.Connection, processo: str, tipo: str) -> list[dict]:
    col = "desembolso_ok" if tipo == "cronograma" else "fisico_ok"
    try:
        return [dict(r) for r in con.execute(
            f"SELECT documento_sei, tipo_documento, data_versao, data_origem FROM pt_cronograma_docs WHERE processo_sei=? AND {col}=1 "
            "ORDER BY data_versao DESC, CAST(documento_sei AS INTEGER) DESC", (processo,))]
    except sqlite3.Error:
        return []


# Metas e etapas que vêm do PT do SEI mesmo quando o GOV tem data igual (decisão do usuário). {processo: motivo}
METAS_SEI_POR_DECISAO = {
    "50020.008564/2024-14": ("ITA H&A (TED 972556/24): METAS = tabela única das etapas do PT 9208116 (decisão do usuário, 24/09/2026); "
                             "o Módulo TED tem só 2 metas sem etapas, com a mesma data (20/12/2024)"),
}
# Instrumentos sem Plano de Trabalho por enquanto (decisão do usuário). {processo: motivo}
PT_AINDA_INEXISTENTE = {
    "50020.003437/2025-00": "Ponta Grossa/PR: instrumento novo, ativo, ainda sem Plano de Trabalho (decisão do usuário, 24/09/2026)",
}


def pt_ausencia_e_pendencia(con: sqlite3.Connection, processo: str, tipo: str, etapa: str | None = None) -> bool:
    """Regra do usuário (03/10/2026): a falta de Plano de Trabalho (tipo cronograma | metas_etapas) NÃO é pendência quando o instrumento está
    em ESTRUTURAÇÃO ou quando o botão correspondente foi desmarcado no tick da ficha (instrumentos_pt_ocultos). Contrato não tem metas."""
    from radar_backend.instrumentos_edicao import pt_ocultos
    if (etapa or "").upper() == "ESTRUTURAÇÃO" or tipo in pt_ocultos(con, processo).get(processo, []):
        return False
    r = con.execute("SELECT tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    return not (r and _categoria(r[0], processo) == "CONTRATO" and tipo == "metas_etapas")

def _escolher_versao(con: sqlite3.Connection, processo: str, vinc: dict, tipo: str) -> dict:
    """Regra: o cronograma / as metas e etapas vêm SEMPRE da versão mais atualizada, seja do GOV ou do SEI.
    Contrato não tem metas (decisão do usuário, 24/09/2026): 'não se aplica', não 'sem dados'."""
    r = con.execute("SELECT tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    if tipo == "metas_etapas" and r and _categoria(r[0], processo) == "CONTRATO" and not _versoes_sei(con, processo, tipo):   # objeto lido (ex. TR) -> mostra
        return {"escolhida": None, "motivo": "Não se aplica: contrato não tem metas e etapas.", "rotulo_vazio": "não se aplica",
                "sei": None, "outras_versoes_sei": 0, "gov": None, "gov_origem": None}
    from radar_backend.instrumentos_edicao import pt_ocultos
    if tipo in pt_ocultos(con, processo).get(processo, []):         # tick desmarcado na ficha ✏️ (usuário, 03/10/2026)
        return {"escolhida": None, "motivo": "Sem Plano de Trabalho: botão desmarcado na ficha do instrumento (decisão do usuário).",
                "rotulo_vazio": "sem Plano de Trabalho", "oculto": True, "sei": None, "outras_versoes_sei": 0, "gov": None, "gov_origem": None}
    if processo in PT_AINDA_INEXISTENTE and not _versoes_sei(con, processo, tipo):
        return {"escolhida": None, "motivo": PT_AINDA_INEXISTENTE[processo], "rotulo_vazio": "PT ainda não elaborado",
                "sei": None, "outras_versoes_sei": 0, "gov": None, "gov_origem": None}
    sei = _versoes_sei(con, processo, tipo)
    # versão editada pelo usuário no popup ✏️ (documento USR:<processo>, radar_backend/plano_trabalho.py) prevalece sempre (01/10/2026)
    usr = next((s for s in sei if str(s["documento_sei"]).startswith("USR:")), None)
    if usr:
        return {"escolhida": "SEI", "motivo": f"Plano de Trabalho editado pelo usuário (gravado em {usr['data_versao'] or '—'}); prevalece sobre GOV e SEI.",
                "sei": usr, "outras_versoes_sei": len(sei) - 1, "gov": None, "gov_origem": ("TED" if vinc.get("ted") else "TC" if vinc.get("tc") else None),
                "editado_usuario": True}
    gov = _data_gov(con, vinc) if (vinc.get("ted") or vinc.get("tc")) else None
    tem_gov = bool(vinc.get("ted") or vinc.get("tc"))
    sei0 = sei[0] if sei else None
    if sei0 and tipo == "metas_etapas" and processo in METAS_SEI_POR_DECISAO:
        escolhida, motivo = "SEI", f"{METAS_SEI_POR_DECISAO[processo]} (doc. {sei0['documento_sei']})."
    elif sei0 and tem_gov and gov and sei0["data_versao"] and sei0["data_versao"] > gov["data"]:
        escolhida, motivo = "SEI", f"Plano de Trabalho do SEI (doc. {sei0['documento_sei']}, {sei0['data_versao']}) é mais recente que o último ato no GOV ({gov['ato']}, {gov['data']})."
    elif tem_gov:
        escolhida = "GOV"
        if sei0 and gov and sei0["data_versao"]:
            motivo = f"Último ato no GOV ({gov['ato']}, {gov['data']}) é igual ou mais recente que o Plano de Trabalho do SEI (doc. {sei0['documento_sei']}, {sei0['data_versao']})."
        elif sei0:
            motivo = "GOV sem data de ato identificável; usada a base oficial do GOV."
        else:
            motivo = "Sem versão validada do SEI para este instrumento."
    elif sei0:
        escolhida, motivo = "SEI", f"Instrumento sem dados no GOV; Plano de Trabalho do SEI (doc. {sei0['documento_sei']}, {sei0['data_versao'] or 'sem data'})."
    else:
        escolhida, motivo = None, "Sem cronograma disponível (nem no GOV, nem em Plano de Trabalho do SEI lido e validado)."
    return {"escolhida": escolhida, "motivo": motivo, "sei": sei0, "outras_versoes_sei": len(sei) - 1 if sei else 0,
            "gov": gov, "gov_origem": ("TED" if vinc.get("ted") else "TC" if vinc.get("tc") else None)}


@bp.get("/api/instrumentos/versao")
def api_instrumentos_versao():
    processo = (request.args.get("processo") or "").strip()
    tipo = (request.args.get("tipo") or "").strip()
    if not processo or tipo not in ("cronograma", "metas_etapas"):
        return jsonify({"sucesso": False, "erro": "Informe processo e tipo (cronograma ou metas_etapas)."}), 400
    try:
        with _db() as con:
            r = con.execute("SELECT numero_siafi, tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
            if r is None:
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            vinc = _vinculos(con, processo, r["numero_siafi"])
            esc = _escolher_versao(con, processo, vinc, tipo)
            out = {"sucesso": True, "processo": processo, "tipo": tipo, "vinculos": vinc, **esc}
            if esc["escolhida"] == "SEI":
                doc = esc["sei"]["documento_sei"]
                if tipo == "metas_etapas":
                    out["secoes"] = [_tabela(con, "Metas e etapas (cronograma de execução do Plano de Trabalho)",
                                             "SELECT * FROM pt_cronograma_fisico WHERE documento_sei=? ORDER BY ordem", (doc,),
                                             [("meta_etapa", "Meta/Etapa"), ("descricao", "Descrição"), ("unidade", "Unid."), ("quantidade", "Qtd."),
                                              ("valor_unitario", "Valor unit."), ("valor_total", "Valor"), ("inicio_texto", "Início"), ("fim_texto", "Término")])]
                    tot = con.execute("SELECT fisico_total_declarado FROM pt_cronograma_docs WHERE documento_sei=?", (doc,)).fetchone()
                    out["total_declarado"] = tot[0] if tot else None
                else:
                    out["secoes"] = [_tabela(con, "Cronograma de desembolso (Plano de Trabalho)",
                                             "SELECT * FROM pt_cronograma_desembolso WHERE documento_sei=? AND adotado=1 ORDER BY tipo, ano, mes, ordem", (doc,),
                                             [("tipo", "Tipo"), ("ano", "Ano"), ("rotulo", "Mês/Parcela"), ("indicador_fisico", "Indicador físico"), ("valor", "Valor")], "valor")]
                    tot = con.execute("SELECT desembolso_total_declarado FROM pt_cronograma_docs WHERE documento_sei=?", (doc,)).fetchone()
                    out["total_declarado"] = tot[0] if tot else None
            return jsonify(out)
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/detalhe")
def api_instrumentos_detalhe():
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            r = con.execute("SELECT * FROM instrumentos WHERE processo_sei = ?", (processo,)).fetchone()
            if r is None:
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            d = dict(r)
            cadeia = [dict(x) for x in con.execute(
                "SELECT ordem, evento, documento_sei, data_evento, termino_anterior, termino_novo FROM instrumento_vigencias WHERE processo_sei=? ORDER BY ordem", (processo,))]
            alt = []
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='transferegov_alteracoes'").fetchone():
                alt = [dict(x) for x in con.execute(
                    "SELECT executado_em, fonte, tabela, tipo, campo, valor_anterior, valor_novo FROM transferegov_alteracoes WHERE instrumento=? ORDER BY id DESC LIMIT 60", (processo,))]
            vinc = _vinculos(con, processo, d.get("numero_siafi"))
            versoes = {t: _escolher_versao(con, processo, vinc, t) for t in ("cronograma", "metas_etapas")}
            # processos relacionados (principal <-> relacionados), pedido de 24/09/2026
            relacionados = []
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='processos_relacionados'").fetchone():
                for x in con.execute("SELECT processo_principal, processo_relacionado, motivo, fonte FROM processos_relacionados "
                                     "WHERE processo_principal=? OR processo_relacionado=? ORDER BY id", (processo, processo)):
                    outro = x["processo_relacionado"] if x["processo_principal"] == processo else x["processo_principal"]
                    relacionados.append({"processo": outro, "papel": "relacionado" if x["processo_principal"] == processo else "principal",
                                         "motivo": x["motivo"], "fonte": x["fonte"]})
            # Relatório de Cumprimento do Objeto (RCO parcial/final) — destaque no painel de detalhe (pedido de 24/09/2026)
            rco = []
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='rco_relatorios'").fetchone():
                for x in con.execute("SELECT * FROM rco_relatorios WHERE processo_sei=? ORDER BY COALESCE(data,'') DESC", (processo,)):
                    item = dict(x)
                    item["metas"] = [dict(m) for m in con.execute("SELECT meta, descricao, valor_meta, valor_gasto, situacao FROM rco_metas "
                                                                   "WHERE documento_sei=? ORDER BY CAST(meta AS INTEGER)", (x["documento_sei"],))]
                    rco.append(item)
            # prestação de contas / encerramento (coluna AQ do Cadastro, tabela prestacao_contas) — também no destaque
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='prestacao_contas'").fetchone():
                for x in con.execute("SELECT documento_sei, tipo_documento, referencia, data_prestacao, resultado, conclusao FROM prestacao_contas "
                                     "WHERE processo_sei=? ORDER BY COALESCE(data_prestacao,'') DESC", (processo,)):
                    rco.append({"tipo": "PRESTACAO", "documento_sei": x["documento_sei"], "data": x["data_prestacao"], "resultado_pc": x["resultado"],
                                "resultado": x["conclusao"], "tipo_documento": x["tipo_documento"], "referencia": x["referencia"], "metas": []})
            portaria = _portaria_gestao(con, processo)
        d["categoria"] = _categoria(d.get("tipo_instrumento"), d.get("processo_sei"))
        d["status"] = _status(d.get("situacao"), d.get("vigencia_mais_futura"), date.today().isoformat())
        return jsonify({"sucesso": True, "instrumento": d, "cadeia_vigencias": cadeia, "alteracoes_transferegov": alt, "vinculos": vinc, "versoes": versoes,
                        "rco": rco, "processos_relacionados": relacionados, "portaria_gestao": portaria})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


_SITUACOES_TC_PENDENTES = ("Aguardando Prestação de Contas", "Prestação de Contas em Complementação", "Prestação de Contas enviada para Análise")


def _acoes_pendentes(con: sqlite3.Connection, processo: str, status: str, vinc: dict, vigencia_mais_futura: str | None) -> list[dict]:
    """Ações a tomar para o instrumento — só sinais concretos já coletados na base (SEI e Transferegov),
    nada inferido além do que os próprios dados afirmam. Usado no modo KANBAN em vez de Cronograma/Metas e Etapas."""
    acoes = []

    # 1) prestação de contas com pendência registrada no SEI (notas técnicas/pareceres)
    for r in con.execute("SELECT documento_sei, resultado, conclusao, data_prestacao FROM prestacao_contas "
                          "WHERE processo_sei=? AND resultado IN ('COM PENDÊNCIAS','NÃO ACEITA')", (processo,)):
        acoes.append({"tipo": "prestacao_contas", "prioridade": "alta",
                       "titulo": f"Prestação de contas — {r['resultado'].lower()}",
                       "descricao": (r["conclusao"] or "")[:400], "fonte": f"SEI, doc. {r['documento_sei']}"})

    # 2) TC/Convênio: situação no Transferegov indica prestação de contas pendente de alguma ação
    if vinc.get("tc"):
        nr = vinc["tc"]["nr_convenio"]
        r = con.execute("SELECT sit_convenio, dia_fim_vigenc_conv FROM transferegov_siconv_convenio WHERE trim(nr_convenio)=?", (nr,)).fetchone()
        if r and r["sit_convenio"] in _SITUACOES_TC_PENDENTES:
            acoes.append({"tipo": "prestacao_contas_gov", "prioridade": "media",
                           "titulo": f"Transferegov: {r['sit_convenio']}",
                           "descricao": f"Situação atual do Convênio/TC {nr} no módulo Discricionárias e Legais.", "fonte": "Transferegov"})

    # 3) TED: Programação Financeira (PF) bloqueada ou pendente de envio/em elaboração
    if vinc.get("ted"):
        numero_ted, id_plano_acao = vinc["ted"]["numero_ted"], vinc["ted"]["id_plano_acao"]
        bloq = con.execute("SELECT motivo, atualizado_em FROM transferegov_progresso_pf_bloqueada_alocacao_automatica WHERE numero_ted=?", (numero_ted,)).fetchall()
        bloq += con.execute("SELECT NULL AS motivo, atualizado_em FROM transferegov_progresso_cronograma_pf_bloqueada WHERE numero_ted=?", (numero_ted,)).fetchall()
        for r in bloq:
            acoes.append({"tipo": "pf_bloqueada", "prioridade": "alta", "titulo": "Programação Financeira bloqueada",
                           "descricao": f"TED {numero_ted}: alocação automática bloqueada"
                           + (f" ({r['motivo']})" if r["motivo"] else "") + ". Requer alocação manual no Módulo TED.", "fonte": "Módulo TED"})
        pend = con.execute("SELECT id_programacao, tx_situacao_programacao, tx_numero_programacao FROM transferegov_ted_programacao_financeira "
                            "WHERE id_plano_acao=? AND tx_situacao_programacao IN ('PENDENTE_ENVIO','EM_ELABORACAO')", (id_plano_acao,)).fetchall()
        if pend:
            rot = {"PENDENTE_ENVIO": "pendente de envio", "EM_ELABORACAO": "em elaboração"}
            por_sit = {}
            for r in pend:
                por_sit.setdefault(r["tx_situacao_programacao"], []).append(r["tx_numero_programacao"] or str(r["id_programacao"]))
            desc = "; ".join(f"{len(v)} programação(ões) {rot[k]}" for k, v in por_sit.items())
            acoes.append({"tipo": "pf_pendente", "prioridade": "media", "titulo": "Programação Financeira do mês pendente",
                           "descricao": f"TED {numero_ted}: {desc}.", "fonte": "Módulo TED"})

    # 4) vigência vencida sem instrumento encerrado — sinal de que falta providência (aditivo, prorrogação ou encerramento)
    if status == "VIGÊNCIA VENCIDA":
        acoes.append({"tipo": "vigencia_vencida", "prioridade": "alta", "titulo": "Vigência vencida",
                       "descricao": "A vigência mais recente já passou e o instrumento não está registrado como encerrado/rescindido: "
                       "verificar se falta um termo aditivo/prorrogação ou se o encerramento ainda não foi registrado.", "fonte": "Instrumentos.db"})

    # 5) instrumento ainda vigente mas com vigência vencendo em até 6 meses: alertar prorrogação com antecedência
    if status == "VIGENTE" and vigencia_mais_futura:
        hoje = date.today()
        limite = date(hoje.year + (1 if hoje.month > 6 else 0), (hoje.month + 6 - 1) % 12 + 1, min(hoje.day, 28))
        if vigencia_mais_futura <= limite.isoformat():
            dias = (date.fromisoformat(vigencia_mais_futura) - hoje).days
            acoes.append({"tipo": "prorrogar", "prioridade": "alta" if dias <= 90 else "media", "titulo": "Prorrogar",
                           "descricao": f"Vence em {dias} dia(s).", "fonte": "Instrumentos.db"})

    ordem = {"alta": 0, "media": 1, "baixa": 2}
    acoes.sort(key=lambda a: ordem.get(a["prioridade"], 9))
    # 6) execução x cronograma até hoje (logo abaixo do 'Prorrogar'): previsto até hoje − descentralizado = débito/crédito
    ex = _saldo_cronograma(con, processo, vinc) if status == "VIGENTE" else None
    if ex:
        i = next((k + 1 for k, a in enumerate(acoes) if a["tipo"] == "prorrogar"), len(acoes))
        acoes.insert(i, ex)
    return acoes


def _saldo_cronograma(con: sqlite3.Connection, processo: str, vinc: dict) -> dict | None:
    """Pedido do usuário (24/09/2026): com a data de HOJE, soma no cronograma (o mesmo do popup, com os movimentos salvos) o valor
    programado até o mês atual e compara com o total já descentralizado (TED: PF; TC/Convênio: empenho). Previsto > realizado = DÉBITO;
    realizado > previsto = CRÉDITO. Traz também a descentralização programada imediatamente anterior e a posterior a hoje."""
    from radar_backend.tc_gantt_empenho import _detalhes_cronograma_empenho, _nr_convenio_do_processo
    r = con.execute("SELECT tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    cat = _categoria(r["tipo_instrumento"] if r else None, processo)
    if cat not in ("TED", "TC", "CONVÊNIO"):
        return None                                         # contrato não tem cronograma
    base = "pf" if cat == "TED" else "empenho"
    try:
        det = _detalhes_cronograma_empenho(con, processo, _nr_convenio_do_processo(con, processo) or "", base)
    except Exception:  # noqa: BLE001
        return None
    per = [l for l in det.get("linhas") or [] if l["_nivel_cronograma"] == "periodo" and l.get("id_periodo") != "ANTES"]
    if not per:
        return None
    rot = "PF" if base == "pf" else "empenho"
    br = lambda v: "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    hoje = date.today()
    mes_hoje = hoje.isoformat()[:7]
    datados = [l for l in per if l.get("_mes")]
    realizado = float(det.get("total_empenhado") or 0)
    if not datados:
        return {"tipo": "cronograma_saldo", "prioridade": "media", "titulo": "Planejado x Executado",
                "descricao": f"Cronograma por parcelas condicionadas a evento (sem mês/ano): não há valor previsto por data.\n"
                             f"Descentralizado ({rot}): {br(realizado)} de {br(float(det.get('total_programado') or 0))} programados.",
                "fonte": _fonte_curta(det.get("origem_estrutura"))}
    previsto = sum(float(l["valor_programado"] or 0) for l in datados if l["_mes"] <= mes_hoje)
    saldo = previsto - realizado
    tipo_saldo = "Débito" if saldo > 0.005 else ("Crédito" if saldo < -0.005 else "Em dia")
    meses = ("", "JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")
    ref = lambda l: f"{meses[int(l['_mes'][5:7])]}/{l['_mes'][:4]} ({br(float(l['valor_programado'] or 0))})"
    ant = [l for l in datados if l["_mes"] <= mes_hoje]
    pos = [l for l in datados if l["_mes"] > mes_hoje]
    linhas = [f"Previsto até {hoje.strftime('%d/%m/%Y')}: {br(previsto)} · Descentralizado ({rot}): {br(realizado)}",
              f"{tipo_saldo}: saldo {'-' if saldo > 0.005 else ''}{br(abs(saldo))}",   # saldo = executado − planejado (negativo = débito)
              f"Descentralização anterior: {ref(ant[-1]) if ant else '—'} · próxima: {ref(pos[0]) if pos else '— (fim do cronograma)'}"]
    return {"tipo": "cronograma_saldo", "prioridade": "alta" if tipo_saldo == "Débito" else "media",
            "titulo": "Planejado x Executado", "descricao": "\n".join(linhas), "saldo": round(-saldo, 2),
            "fonte": _fonte_curta(det.get("origem_estrutura"))}


def _fonte_curta(origem: str | None) -> str:
    """Fonte resumida no card (pedido do usuário, 24/09/2026): 'SICONV', 'PT SEI 9866077', 'Módulo TED'."""
    o = origem or ""
    m = re.search(r"doc\. (\w+)", o)
    if o.startswith("Plano de Trabalho do SEI") and m:
        return f"PT SEI {m.group(1)}"
    if o.startswith("SICONV"):
        return "SICONV"
    if "Módulo TED" in o:
        return "Módulo TED"
    return o or "Cronograma"


def _portaria_gestao(con: sqlite3.Connection, processo: str) -> dict | None:
    """Portaria de gestão mais recente (gestor/fiscal) — destaque 'GESTOR: Portaria' no DETALHE (pedido do usuário, 25/09/2026)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='portarias_gestao'").fetchone():
        return None
    pg = con.execute("SELECT * FROM portarias_gestao WHERE processo_sei=? ORDER BY COALESCE(data,'') DESC LIMIT 1", (processo,)).fetchone()
    if not pg:
        return None
    portaria = dict(pg)
    portaria["membros"] = [dict(m) for m in con.execute("SELECT artigo, nome, siape, cargo, texto FROM portarias_membros "
                                                        "WHERE documento_sei=? ORDER BY artigo", (pg["documento_sei"],))]
    portaria["anteriores"] = [dict(x) for x in con.execute("SELECT documento_sei, numero, data FROM portarias_gestao WHERE processo_sei=? "
                                                           "AND documento_sei<>? ORDER BY COALESCE(data,'') DESC", (processo, pg["documento_sei"]))]
    return portaria


@bp.get("/api/instrumentos/acoes")
def api_instrumentos_acoes():
    """Ações a tomar para um instrumento (usado no modo KANBAN em vez de Cronograma/Metas e Etapas)."""
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            r = con.execute("SELECT numero_siafi, situacao, vigencia_mais_futura FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
            if r is None:
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            status = _status(r["situacao"], r["vigencia_mais_futura"], date.today().isoformat())
            vinc = _vinculos(con, processo, r["numero_siafi"])
            acoes = _acoes_pendentes(con, processo, status, vinc, r["vigencia_mais_futura"])
            portaria = _portaria_gestao(con, processo)
        return jsonify({"sucesso": True, "processo": processo, "status": status, "acoes": acoes, "portaria_gestao": portaria})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _num_br(s) -> float:
    """'13.340.385,91' ou '13340385,91' -> 13340385.91; None/vazio -> 0.0"""
    if s is None or s == "":
        return 0.0
    try:
        return float(str(s).replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


_RE_PF_CURTO = re.compile(r"(\d{4}PF\d+)$")


def _pf_ted(con: sqlite3.Connection, processo: str, vinc: dict) -> dict | None:
    """PF (Programação Financeira) de um TED: junta a lista ao vivo do Transferegov (Módulo TED) com a
    lista antiga do Cadastro (tabela pf_processo, campo 'pf'), para achar PFs que só aparecem numa fonte.
    Acrescenta TRF (transferência efetivamente realizada, tabela tg_execucao_join/etapa TRF, ver
    scripts/criar_tg_execucao_join.py) para comparar PF (pedido) x TRF (repasse) — achado de 22-23/09/2026:
    PF não carrega valor, só TRF; por isso a comparação é por QUANTIDADE de eventos, não valor x valor."""
    if not vinc.get("ted"):
        return None
    id_plano_acao = vinc["ted"]["id_plano_acao"]
    gov = {}
    for r in con.execute("SELECT tx_numero_programacao, tx_situacao_programacao, dh_recebimento_programacao, id_programacao "
                          "FROM transferegov_ted_programacao_financeira WHERE id_plano_acao=? AND tx_numero_programacao IS NOT NULL",
                          (id_plano_acao,)):
        n = r["tx_numero_programacao"]
        if n not in gov or (r["dh_recebimento_programacao"] or "") > (gov[n]["data"] or ""):
            gov[n] = {"situacao": r["tx_situacao_programacao"], "data": r["dh_recebimento_programacao"], "id": r["id_programacao"]}
    # TRF por PF (id_programacao): cada transferência é uma ENTRADA (a API não traz devolução de TRF)
    trf_por_id, trf_linhas = {}, []
    num_por_id = {g["id"]: n for n, g in gov.items()}
    for r in con.execute("SELECT t.id_programacao, t.vl_valor_trf, t.cd_situacao_contabil_trf, t.cd_categoria_gasto_trf FROM transferegov_ted_trf t "
                         "WHERE t.id_programacao IN (SELECT id_programacao FROM transferegov_ted_programacao_financeira WHERE id_plano_acao=?)",
                         (id_plano_acao,)):
        v = float(r["vl_valor_trf"] or 0)
        trf_por_id[r["id_programacao"]] = trf_por_id.get(r["id_programacao"], 0.0) + v
        trf_linhas.append({"pf": num_por_id.get(r["id_programacao"]), "situacao_contabil": r["cd_situacao_contabil_trf"],
                           "categoria": r["cd_categoria_gasto_trf"], "movimento": "Saída" if v < 0 else "Entrada", "valor": round(v, 2)})
    cad = set()
    for (pf,) in con.execute("SELECT DISTINCT pf FROM pf_processo WHERE processo_sei=? AND pf IS NOT NULL", (processo,)):
        m = _RE_PF_CURTO.search(pf or "")
        if m:
            cad.add(m.group(1))
    todos = sorted(set(gov) | cad)
    # PF: Transferegov + Cadastro complementares, sem repetir número. Só PF ENVIADA (ou só do Cadastro,
    # já efetivada) é considerada; EM_ELABORACAO/PENDENTE_ENVIO nunca saiu — listada, fora da conta.
    linhas = []
    for n in todos:
        sit = gov.get(n, {}).get("situacao")
        considerado = sit in (None, "ENVIADA")
        linhas.append({"pf": n, "situacao": sit, "data": gov.get(n, {}).get("data"),
                       "origem": "ambos" if (n in gov and n in cad) else ("Transferegov" if n in gov else "Cadastro (não achado no Transferegov)"),
                       "movimento": "Entrada", "considerado": considerado,
                       "motivo": None if considerado else f"Não utilizada — PF {sit.lower().replace('_', ' ')}, nunca enviada",
                       "trf_valor": round(trf_por_id.get(gov.get(n, {}).get("id"), 0.0), 2)})
    trf_n, trf_valor = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(valor),0) FROM tg_execucao_join WHERE processo_sei=? AND etapa='TRF'", (processo,)
    ).fetchone() if con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_execucao_join'").fetchone() else (0, 0)
    # Pago pela EXECUTORA (pedido do usuário, 25/09/2026): OBs da UG executora casadas pelo PI da NE (etapa PAGAMENTO do
    # tg_execucao_join, ex. COMARA 120628 no TED 939156, CAE/CISCEA 120195 no TED 977360) — entradas menos estornos ('*_ANULACAO');
    # '*_NAO_PAGAMENTO' (aplicação financeira) fica fora. radar_ob.csv só tem OBs emitidas em 2026 (pendência OB-UNIFICADO).
    pago_n, pago_valor, pago_anos = 0, 0.0, []
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_execucao_join'").fetchone():
        pago_n, pago_valor = con.execute(
            "SELECT COUNT(DISTINCT substr(documento,1,23)), COALESCE(SUM(CASE WHEN metodo_match LIKE '%_ANULACAO' THEN -valor ELSE valor END),0) "
            "FROM tg_execucao_join WHERE processo_sei=? AND etapa='PAGAMENTO' AND metodo_match NOT LIKE '%_NAO_PAGAMENTO'", (processo,)).fetchone()
        pago_anos = [a for (a,) in con.execute(
            "SELECT DISTINCT substr(documento,12,4) FROM tg_execucao_join WHERE processo_sei=? AND etapa='PAGAMENTO' ORDER BY 1", (processo,))]
    div = divergencias_trf_pf(con, processo)
    trf_api, pf_alocada = _valor_enviado(con, vinc)
    if trf_api is not None and pf_alocada is not None and abs(trf_api - pf_alocada) >= 0.01:
        div.append({"tipo": "Total PF alocada x TRF", "pf": None, "valor": round(pf_alocada - trf_api, 2),
                    "descricao": f"PF alocada no cronograma (Módulo TED) {_brl(pf_alocada)} x TRF {_brl(trf_api)}"})
    return {"linhas": linhas, "trf_linhas": trf_linhas, "divergencias": div, "total": len(todos), "considerados": sum(1 for l in linhas if l["considerado"]), "so_cadastro": len(cad - set(gov)), "so_transferegov": len(set(gov) - cad), "em_ambos": len(cad & set(gov)),
            "trf_total": trf_n, "trf_valor": round(trf_valor or 0, 2),
            "pf_valor": round(sum(l["trf_valor"] for l in linhas if l["considerado"]), 2),   # PF enviada, valor = repasse efetivado da PF (como no livro)
            "pago_executora_n": pago_n, "pago_executora_valor": round(pago_valor or 0, 2), "pago_executora_anos": pago_anos}


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def divergencias_trf_pf(con: sqlite3.Connection, processo: str) -> list[dict]:
    """Inconsistências TRF x PF de um TED (usuário, 25/09/2026: o botão TRF x PF só aparece se houver alguma). Compara, PF a PF,
    o livro de PF de TODAS as fontes (Módulo TED, SIAFI CONTRANSF, RCO, documentos; só considerados) com a TRF (trf_ted: Módulo TED;
    fora dele, a própria PF do SIAFI — então sem divergência por construção): PF sem TRF, TRF sem PF, valor diferente e total diferente."""
    from radar_backend.tc_gantt_empenho import livro_pf, trf_ted
    pf, trf = {}, {}
    for l in livro_pf(con, processo):
        if l.get("considerado") and abs(l.get("valor") or 0) >= 0.005:
            k = l.get("_chave") or l["documento"]
            pf.setdefault(k, [0.0, l.get("fonte") or ""])[0] += l["valor"]
    for l in trf_ted(con, processo):
        k = l.get("_chave") or l["documento"]
        trf[k] = trf.get(k, 0.0) + (l.get("valor") or 0)
    div = []
    for k in sorted(set(pf) | set(trf)):
        vp, fonte = pf.get(k, (None, ""))
        vt = trf.get(k)
        if vt is None:
            div.append({"tipo": "PF sem TRF", "pf": k, "valor": round(vp, 2), "descricao": f"PF sem transferência correspondente — fonte: {fonte}"})
        elif vp is None:
            div.append({"tipo": "TRF sem PF", "pf": k, "valor": round(vt, 2), "descricao": "transferência sem PF correspondente no livro de PF"})
        elif abs(vp - vt) >= 0.01:
            div.append({"tipo": "Valor diferente", "pf": k, "valor": round(vp - vt, 2), "descricao": f"PF {_brl(vp)} x TRF {_brl(vt)} — fonte da PF: {fonte}"})
    # mesmo documento com valor diferente entre fontes (regra das fontes complementares, 25/09/2026: vale o de maior prioridade —
    # Módulo TED > telas/planilha/RCO/documento > Tesouro Gerencial —, a diferença aparece aqui)
    from radar_backend.tc_gantt_empenho import pfs_tg
    for e in pfs_tg(con, processo):
        vp = pf.get(e["pf"], (None, ""))[0]
        if vp is not None and abs(vp - e["valor"]) >= 0.01:
            div.append({"tipo": "Valor diferente (TG)", "pf": e["pf"], "valor": round(vp - e["valor"], 2),
                        "descricao": f"livro de PF {_brl(vp)} x Tesouro Gerencial {_brl(e['valor'])} ({e['arquivo']})"})
    tp, tt = sum(v[0] for v in pf.values()), sum(trf.values())
    if div and abs(tp - tt) >= 0.01:
        div.append({"tipo": "Total", "pf": None, "valor": round(tp - tt, 2), "descricao": f"total PF {_brl(tp)} x total TRF {_brl(tt)}"})
    return div


def _pf_ted_sem_modulo(con: sqlite3.Connection, processo: str) -> dict | None:
    """TED fora do Módulo TED (ex. Balsas 698918): mesmo formato de _pf_ted para o gráfico do DETALHE. Desembolso-TRF = PF do SIAFI
    (livro de PF: telas CONTRANSF, RCO, documentos; devoluções abatidas) — lá a PF é a própria transferência (usuário, 25/09/2026)."""
    from radar_backend.tc_gantt_empenho import trf_ted
    trf = trf_ted(con, processo)
    if not trf:
        return None
    pago_n, pago_valor, pago_anos = 0, 0.0, []
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_execucao_join'").fetchone():
        pago_n, pago_valor = con.execute(
            "SELECT COUNT(DISTINCT substr(documento,1,23)), COALESCE(SUM(CASE WHEN metodo_match LIKE '%_ANULACAO' THEN -valor ELSE valor END),0) "
            "FROM tg_execucao_join WHERE processo_sei=? AND etapa='PAGAMENTO' AND metodo_match NOT LIKE '%_NAO_PAGAMENTO'", (processo,)).fetchone()
        pago_anos = [a for (a,) in con.execute(
            "SELECT DISTINCT substr(documento,12,4) FROM tg_execucao_join WHERE processo_sei=? AND etapa='PAGAMENTO' ORDER BY 1", (processo,))]
    total = round(sum(l["valor"] for l in trf), 2)
    return {"linhas": [], "trf_linhas": [], "divergencias": divergencias_trf_pf(con, processo), "total": len(trf), "considerados": len(trf), "so_cadastro": 0,
            "so_transferegov": 0, "em_ambos": 0, "trf_total": len(trf), "trf_valor": total, "pf_valor": total, "sem_modulo": True,
            "pago_executora_n": pago_n, "pago_executora_valor": round(pago_valor or 0, 2), "pago_executora_anos": pago_anos}


def _execucao_tg(con: sqlite3.Connection, processo: str) -> dict | None:
    """Crédito (NC), Empenho (NE) e Pagamento (OB) de tg_execucao_join (dados/tg/ + API TransfereGov,
    ver scripts/criar_tg_execucao_join.py), para o gráfico de barras dos instrumentos sem PF (TC,
    Convênio, Contrato). Substitui o antigo Empenho/Desembolso/Pagamento do SICONV (22-23/09/2026):
    não existe fonte confiável de Liquidação em dados/tg/ (ver docstring do script), então a 3ª etapa
    virou Crédito (a etapa mais robusta e anterior ao Empenho), não mais uma etapa 'liquidação'."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_execucao_join'").fetchone():
        return None
    linhas = dict(con.execute(                        # entradas menos saídas ('*_ANULACAO'), regra do usuário de 24/09/2026
        "SELECT etapa, SUM(CASE WHEN metodo_match LIKE '%_ANULACAO' THEN -valor ELSE valor END) FROM tg_execucao_join "
        "WHERE processo_sei=? AND etapa IN ('CREDITO','EMPENHO','PAGAMENTO') GROUP BY etapa",
        (processo,)).fetchall())
    if not linhas:
        return None
    empenho = sum(e["valor"] for e in _empenhos_tg_execucao_join(con, processo))  # líquido (desconta anulações)
    # regra padronizada (usuário, 25/09/2026): as OBs daqui (FNAC ou UG executora ao contratado/BNDES) são DESEMBOLSO ao executor;
    # PAGAMENTO (executor -> particular) não existe nessas bases para contrato; no BNDES virá com os empréstimos às aéreas
    r = con.execute("SELECT tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    contrato = _categoria(r["tipo_instrumento"] if r else None, processo) == "CONTRATO"
    return {"credito": round(linhas.get("CREDITO") or 0, 2), "empenho": round(empenho, 2),
            "desembolso_executor": round(linhas.get("PAGAMENTO") or 0, 2), "pagamento": 0.0, "contrato": contrato}


RE_OB_CURTO = re.compile(r"(\d{4}OB\d+)")
UG_FNAC = "110591"


def _num_siconv(x) -> float:
    """Valor do SICONV vem como texto no formato BR ('3869130,82')."""
    try:
        return float(str(x or "0").replace(".", "").replace(",", ".")) if "," in str(x or "") else float(x or 0)
    except ValueError:
        return 0.0


def _execucao_tc(con: sqlite3.Connection, processo: str, siafi: str) -> dict:
    """Empenho, Desembolso e Pagamento de TC/Convênio = total dos livros de movimentos
    (radar_backend/livro_financeiro.py): entradas menos saídas, só documentos considerados,
    fontes complementares sem repetição (regras do usuário, 24/09/2026)."""
    emp = L.livro_empenho(con, processo, siafi)
    des = L.livro_desembolso(con, processo, siafi)
    pag = L.livro_pagamento(con, processo, siafi)
    return {"empenho": L.total(emp), "desembolso": L.total(des), "pagamento": L.total(pag),
            "fontes": {"n_empenho": len(emp), "n_desembolso": len(des), "n_pagamento": len(pag)}}


def livros_do_processo(con: sqlite3.Connection, processo: str) -> dict | None:
    """Livro de movimentos de QUALQUER instrumento (TC, Convênio, TED, Contrato), por processo: Crédito (NC), Empenho, PF,
    Desembolso e Pagamento, documento a documento — entradas, anulações/cancelamentos/devoluções (saídas) e documentos não
    utilizados (listados com o motivo, fora do saldo). Pedido do usuário, 24/09/2026. Usado pela tela (/api/instrumentos/livro)
    e gravado na tabela livro_financeiro (scripts/registrar_decisoes.py)."""
    from radar_backend.tc_gantt_empenho import livro_pf, trf_ted, livro_nc
    r = con.execute("SELECT numero_siafi, tipo_instrumento, localidades, situacao, valor_atual, valor_total FROM instrumentos "
                    "WHERE processo_sei=?", (processo,)).fetchone()
    if r is None:
        return None
    vinc = _vinculos(con, processo, r["numero_siafi"])
    siafi = str((vinc.get("tc") or {}).get("nr_convenio") or r["numero_siafi"] or "").strip() or None
    cat = _categoria(r["tipo_instrumento"], processo)
    colunas = [{"campo": "documento", "rotulo": "Documento"}, {"campo": "data", "rotulo": "Data"},
               {"campo": "movimento", "rotulo": "Movimento"}, {"campo": "situacao", "rotulo": "Situação"},
               {"campo": "fonte", "rotulo": "Fonte"}, {"campo": "valor", "rotulo": "Valor"}]

    def livro(titulo, linhas, nota):
        return {"titulo": titulo, "nota": nota, "colunas": colunas, "linhas": linhas, "livro": True,
                "total": {"campo": "valor", "valor": L.total(linhas)},
                "n_entradas": sum(1 for l in linhas if l["movimento"] == "Entrada" and l["considerado"]),
                "n_saidas": sum(1 for l in linhas if l["movimento"] == "Saída" and l["considerado"]),
                "n_nao_utilizados": sum(1 for l in linhas if not l["considerado"])}
    secoes = [
        livro("Crédito (NC)", livro_nc(con, processo),
              "Notas de crédito do FNAC (descentralização; no TED faz o papel do empenho): descentralização (entradas) e "
              "anulação/estorno/devolução (saídas) — Tesouro Gerencial + SIAFI/RCO + documentos + Módulo TED."),
        livro("Empenho", L.livro_empenho(con, processo, siafi, outras_ug=cat == "CONTRATO"),
              "Notas de empenho: emissão, reforço (entradas) e anulação/cancelamento (saídas), evento a evento — "
              "Tesouro Gerencial + SICONV + documentos, sem repetir documento."
              + (" Contrato: inclui o saldo das NEs da UG executora (crédito descentralizado)." if cat == "CONTRATO" else "")),
        # TED (dentro ou fora do Transferegov): o botão PF É a TRF (transferência efetivada; fora do Módulo TED, a PF do SIAFI) e não há
        # botão Desembolso — padronização do usuário, 25/09/2026. Inconsistência TRF x PF: divergencias_trf_pf (botão só se houver).
        livro("PF", trf_ted(con, processo) if cat == "TED" else livro_pf(con, processo),
              "TED: PF = TRF — transferências efetivadas ao executor (Módulo TED; fora dele, PF do SIAFI CONTRANSF/RCO/documentos, devoluções = "
              "saídas). Na maioria das vezes coincide com a PF; se não coincidir, aparece o botão TRF x PF." if cat == "TED" else
              "Programações Financeiras: enviadas (entradas) e devoluções/estornos (saídas) — Módulo TED + SIAFI CONTRANSF + RCO + documentos."),
        # regra padronizada (usuário, 25/09/2026): DESEMBOLSO = recurso ao EXECUTOR; PAGAMENTO = o executor paga o PARTICULAR
        livro("Desembolso", [] if cat == "TED" else L.livro_desembolso(con, processo, siafi, todas_ug=cat == "CONTRATO"),
              "Recurso ao executor. " + ("TED: ver PF (= TRF)." if cat == "TED" else
              "Contrato: OBs ao contratado — do FNAC (UG 110591) e da UG executora do crédito descentralizado — Tesouro Gerencial; estornos = saídas."
              if cat == "CONTRATO" else
              "Ordens bancárias do FNAC ao executor (convenente/BNDES) (entradas) e devoluções/estornos (saídas) — Tesouro Gerencial + SICONV + documentos.")),
        livro("Pagamento", [] if cat == "CONTRATO" else L.livro_pagamento(con, processo, siafi),
              "O executor paga o particular. " + ("Contrato: o pagamento do contratado a terceiros não é acompanhado." if cat == "CONTRATO" else
              "Pagamentos do executor aos fornecedores (OBTV/OB da UG executora): SICONV + Tesouro Gerencial, sem repetir; estornos = saídas. "
              "BNDES: empréstimos às aéreas (ainda não há).")),
    ]
    return {"processo": processo, "numero_siafi": r["numero_siafi"], "categoria": cat,
            "rotulo": " — ".join(x for x in (r["tipo_instrumento"], r["localidades"]) if x),
            "situacao": r["situacao"], "valor": r["valor_atual"] or r["valor_total"], "secoes": secoes}


# ---------------------------------------------------------------- PDFs do instrumento (lista de seleção ao lado do Físico-Financeiro)
# Pedido do usuário (26/09/2026): no modo LISTAGEM, uma lista com os PDFs guardados no Instrumentos.db (arquivos_pdf) do processo, com
# nomes sintéticos — "TC Inicial", "Aditivo 1", "Plano de Trabalho Atual"... — e, ao escolher, o PDF abre. O papel de cada documento vem
# do Cadastro (cadastro_documentos: coluna B/C/D/E...) e de documentos_vinculados; sem papel, do tipo lido do próprio documento.
_PAPEL_COLUNA = {"B": "instrumento_inicial", "C": "pti", "D": "aditivo", "E": "pt", "Y": "ddo_inicial", "Z": "portaria_gestor_sac",
                 "AG": "contratos", "AO": "monitoramento_do_objeto_rco_parcial", "AQ": "avaliacao_do_objeto_rco_final_prestacao_de_contas"}
# rótulos definidos pelo usuário (26/09/2026): "RCO" = Relatório de Cumprimento do Objeto; "Gestor/Fiscal" = portaria de gestores e fiscais
_ROTULO_PAPEL = {"ddo_inicial": "DDO Inicial", "portaria_gestor_sac": "Gestor/Fiscal", "contratos": "Contrato do Executor",
                 "monitoramento_do_objeto_rco_parcial": "RCO Parcial", "avaliacao_do_objeto_rco_final_prestacao_de_contas": "RCO"}
_ORDEM_PAPEL = ["instrumento_inicial", "aditivo", "pti", "pt", "avaliacao_do_objeto_rco_final_prestacao_de_contas",
                "monitoramento_do_objeto_rco_parcial", "portaria_gestor_sac", "contratos", "ddo_inicial"]


def _sigla_instrumento(tipo: str | None) -> str:
    t = (tipo or "").strip().upper()
    if t.startswith("TED") or "EXECUÇÃO DESCENTRALIZADA" in t:
        return "TED"
    if t.startswith("TC") or "COMPROMISSO" in t:
        return "TC"
    if t.startswith("CONV"):
        return "Convênio"
    if t.startswith(("CT", "CONTRATO")) or "FINANCIAMENTO" in t:
        return "Contrato"
    return (tipo or "Instrumento").split(" ")[0]


def _br_data(d: str | None) -> str:
    d = str(d or "")[:10]
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if re.match(r"\d{4}-\d{2}-\d{2}", d) else ""


def documentos_pdf(con: sqlite3.Connection, processo: str) -> list[dict]:
    """PDFs do processo guardados no banco, com rótulo sintético e na ordem: instrumento, aditivos, planos de trabalho, relatórios, demais."""
    inst = con.execute("SELECT tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    sigla = _sigla_instrumento(inst["tipo_instrumento"] if inst else None)
    papeis: dict[str, set] = {}
    for letra, doc in con.execute("SELECT coluna_letra, documento_sei FROM cadastro_documentos WHERE processo_sei=? AND no_banco=1", (processo,)):
        if letra in _PAPEL_COLUNA:
            papeis.setdefault(doc, set()).add(_PAPEL_COLUNA[letra])
    motivos = {}
    for papel, doc, motivo in con.execute("SELECT papel, documento_sei, motivo FROM documentos_vinculados WHERE processo_sei=?", (processo,)):
        papeis.setdefault(doc, set()).add(papel)
        motivos[doc] = (motivos.get(doc, "") + " " + (motivo or "")).strip()
    docs = {}
    for r in con.execute("""SELECT d.documento_sei, d.tipo_documento, d.numero_documento, COALESCE(d.data_assinatura, d.data_documento) AS data
                            FROM documentos d JOIN arquivos_pdf a USING (documento_sei)
                            WHERE lower(a.nome) LIKE '%.pdf' AND (d.processo_sei=? OR d.documento_sei IN
                              (SELECT documento_sei FROM cadastro_documentos WHERE processo_sei=?) OR d.documento_sei IN
                              (SELECT documento_sei FROM documentos_vinculados WHERE processo_sei=?))""", (processo, processo, processo)):
        docs[r["documento_sei"]] = dict(r)
    # documentos adicionados pela tela SEI (01/10/2026): aparecem já, antes do próximo rebuild ler o texto deles
    for r in con.execute("SELECT documento_sei, COALESCE(data_documento, substr(adicionado_em, 1, 10)) AS data, tipo_detectado FROM sei.documentos_usuario "
                         "WHERE processo_sei=?", (processo,)):
        tipo_d = "Plano de Trabalho" if r["tipo_detectado"] == "Cronograma Físico-Financeiro" else r["tipo_detectado"]
        docs.setdefault(r["documento_sei"], {"documento_sei": r["documento_sei"], "tipo_documento": tipo_d, "numero_documento": None, "data": r["data"]})
    nomes = dict(con.execute("SELECT documento_sei, nome FROM sei.documentos_nomes"))   # nome dado pelo usuário substitui o rótulo
    usuario = {r[0] for r in con.execute("SELECT documento_sei FROM sei.documentos_usuario WHERE processo_sei=?", (processo,))}
    try:                                                   # processo SEI do documento (principal ou relacionado), 02/10/2026
        proc_doc = dict(con.execute("SELECT documento_sei, processo_documento FROM sei.documentos_usuario WHERE processo_sei=? "
                                    "AND processo_documento IS NOT NULL", (processo,)))
    except sqlite3.Error:
        proc_doc = {}
    itens = []
    # 1. instrumento
    iniciais = sorted((d for d in docs.values() if "instrumento_inicial" in papeis.get(d["documento_sei"], ())), key=lambda d: d["data"] or "9999")
    for k, d in enumerate(iniciais):
        itens.append((0, d, f"{sigla} Inicial" + (f" ({k + 1})" if k else "")))
    # 2. aditivos: nº do documento ("Termo Aditivo 3º") ou ordem de data; mesmo nº repetido = outra versão/minuta
    adit = sorted((d for d in docs.values() if "aditivo" in papeis.get(d["documento_sei"], ()) or
                   (d["tipo_documento"] == "Termo Aditivo" and d["documento_sei"] not in papeis)), key=lambda d: d["data"] or "9999")
    numerados, seq = [], 0
    for d in adit:
        m = re.search(r"(\d+)", d["numero_documento"] or "")
        n = int(m.group(1)) if m else None
        if n is None:                                   # sem nº no documento: minuta cuja versão assinada vem depois = mesmo nº
            prox = next((x for x in adit if x is not d and (x["data"] or "") >= (d["data"] or "") and re.search(r"\d+", x["numero_documento"] or "")), None)
            n = int(re.search(r"\d+", prox["numero_documento"]).group(0)) if prox and "minuta" in motivos.get(d["documento_sei"], "").lower() else seq + 1
        seq = max(seq, n)
        numerados.append((n, d))
    por_n: dict[int, list] = {}
    for n, d in numerados:
        por_n.setdefault(n, []).append(d)
    for n, grupo in por_n.items():
        for d in grupo:
            minuta = len(grupo) > 1 and "minuta" in motivos.get(d["documento_sei"], "").lower()
            outra = len(grupo) > 1 and not minuta and d is not next((x for x in grupo if "minuta" not in motivos.get(x["documento_sei"], "").lower()), None)
            itens.append((1 + n / 1000, d, f"Aditivo {n}" + (" (minuta)" if minuta else " (outra versão)" if outra else "")))
    # 3. planos de trabalho: o mais recente com papel PT = "Atual"; o PTI = "Inicial"; os demais pela data
    pts = sorted((d for d in docs.values() if papeis.get(d["documento_sei"], set()) & {"pti", "pt"} or
                  (d["tipo_documento"] == "Plano de Trabalho" and d["documento_sei"] not in papeis)), key=lambda d: d["data"] or "")
    atual = next((d for d in reversed(pts) if "pt" in papeis.get(d["documento_sei"], ())), pts[-1] if pts else None)
    inicial = next((d for d in pts if "pti" in papeis.get(d["documento_sei"], ())), pts[0] if len(pts) > 1 else None)
    for d in pts:
        if atual and inicial and d is atual and d is inicial:
            rot = "Plano de Trabalho (Inicial e Atual)"
        elif d is atual:
            rot = "Plano de Trabalho Atual"
        elif d is inicial:
            rot = "Plano de Trabalho Inicial"
        else:
            rot = "Plano de Trabalho " + (_br_data(d["data"]) or d["documento_sei"])
        itens.append((2.0 if d is inicial else 2.9 if d is atual else 2.5, d, rot))   # Inicial, intermediários, Atual
    # 4. demais: pelo papel no Cadastro ou pelo tipo lido do documento
    vistos = {id(x[1]) for x in itens}
    for d in sorted(docs.values(), key=lambda d: d["data"] or "9999"):
        if id(d) in vistos:
            continue
        ps = [p for p in _ORDEM_PAPEL if p in papeis.get(d["documento_sei"], ())]
        if ps and ps[0] in _ROTULO_PAPEL:
            ordem, rot = 3 + _ORDEM_PAPEL.index(ps[0]), _ROTULO_PAPEL[ps[0]]
        elif d["tipo_documento"] in ("Termo de Compromisso", "Termo de Execução Descentralizada", "Convênio", "Contrato"):
            ordem, rot = 3, f"{sigla} (outra via)"
        elif d["tipo_documento"] and d["tipo_documento"] != "Outro":
            ordem, rot = 20, d["tipo_documento"]
        else:
            ordem, rot = 30, "Documento"
        itens.append((ordem, d, rot))
    # rótulo repetido ganha a data (ou o nº do documento)
    cont: dict[str, int] = {}
    for _, _, rot in itens:
        cont[rot] = cont.get(rot, 0) + 1
    saida = []
    for ordem, d, rot in sorted(itens, key=lambda x: (x[0], x[1]["data"] or "9999")):
        if cont[rot] > 1:
            rot = f"{rot} — {_br_data(d['data']) or d['documento_sei']}"
        saida.append({"documento_sei": d["documento_sei"], "rotulo": nomes.get(d["documento_sei"]) or rot, "rotulo_automatico": rot,
                      "data": d["data"], "tipo": d["tipo_documento"], "adicionado": d["documento_sei"] in usuario,
                      "processo_documento": proc_doc.get(d["documento_sei"])})
    return saida


@bp.get("/api/instrumentos/documentos-pdf")
def api_instrumentos_documentos_pdf():
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            return jsonify({"sucesso": True, "processo": processo, "documentos": documentos_pdf(con, processo)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/pdf")
def api_instrumentos_pdf():
    """Devolve o PDF original guardado no banco (arquivos_pdf), para abrir no navegador."""
    import zlib
    from flask import Response
    doc = (request.args.get("documento") or "").strip()
    try:
        with _db() as con:
            r = con.execute("SELECT nome, compressao, conteudo FROM arquivos_pdf WHERE documento_sei=?", (doc,)).fetchone()
        if not r or not str(r["nome"] or "").lower().endswith(".pdf"):
            return jsonify({"sucesso": False, "erro": "PDF não encontrado no banco."}), 404
        dados = zlib.decompress(r["conteudo"]) if r["compressao"] == "zlib" else r["conteudo"]
        return Response(dados, mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", doc)}.pdf"'})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- documento: dados de origem + DESVINCULAR (usuário, 26/09/2026)
# No livro (botões NC / NE / PF / Desembolso / Pagamento), clicar no nº do documento abre todas as informações dele nas tabelas de
# origem; o botão DESVINCULAR tira o documento do processo e ele volta para a base comum (tg_execucao_join deixa de ligá-lo; a
# decisão fica em `desvinculos` e é respeitada pelo criar_tg_execucao_join.py nas próximas cargas e pelo livro para as demais fontes).
RE_DOC_ID_COMPLETO = re.compile(r"\d{6}\d{5}\d{4}(?:NE|NC|OB|PF|RO|NS|GR)\d{6}")
RE_DOC_ID_CURTO = re.compile(r"(?<!\d)\d{4}(?:NE|NC|OB|PF|RO|NS|GR)\d{6}")
DDL_DESVINCULOS = ("CREATE TABLE IF NOT EXISTS desvinculos (processo_sei TEXT NOT NULL, chave TEXT NOT NULL, etapa TEXT, documento TEXT, "
                   "motivo TEXT, criado_em TEXT, PRIMARY KEY (processo_sei, chave))")


def doc_id(documento: str | None, chave: str | None = None) -> str:
    """Identificador do documento: o 1º nº SIAFI completo (UG+gestão+nº) do texto; senão o curto; senão a chave do livro."""
    for texto in (documento, chave):
        m = RE_DOC_ID_COMPLETO.search(texto or "") or RE_DOC_ID_CURTO.search(texto or "")
        if m:
            return m.group(0)
    return (chave or documento or "").strip()


def _casa_id(identificador: str, texto: str | None) -> bool:
    if not identificador or not texto:
        return False
    if RE_DOC_ID_COMPLETO.fullmatch(identificador):
        return identificador in texto
    return bool(re.search(r"(?<![\dA-Z])" + re.escape(identificador) + r"(?!\d)", texto))


DDL_VINCULOS_MANUAIS = ("CREATE TABLE IF NOT EXISTS vinculos_manuais (chave TEXT NOT NULL, processo_origem TEXT NOT NULL, processo_destino TEXT NOT NULL, "
                        "etapa TEXT, documento TEXT, criado_em TEXT, PRIMARY KEY (chave, processo_origem))")


def _garantir_tabelas_vinculo(con: sqlite3.Connection) -> None:
    con.execute(DDL_DESVINCULOS)
    con.execute(DDL_VINCULOS_MANUAIS)
    if "linhas_json" not in {c[1] for c in con.execute("PRAGMA table_info(desvinculos)")}:
        con.execute("ALTER TABLE desvinculos ADD COLUMN linhas_json TEXT")    # linhas tiradas do tg_execucao_join (para revincular)


def desvinculados(con: sqlite3.Connection, processo: str) -> set[str]:
    _garantir_tabelas_vinculo(con)
    return {c for (c,) in con.execute("SELECT chave FROM desvinculos WHERE processo_sei=?", (processo,))}


def _linhas_arquivo_tg(nome: str, identificador: str, limite: int = 30) -> tuple[list, list]:
    """Linhas do arquivo do Tesouro Gerencial (dados/tg; recriado do banco se preciso) que contêm o documento, como {coluna: valor}."""
    import csv as _csv
    import io as _io
    from radar_backend import tg_arquivos
    from radar_backend.radar_config import TG_DIR
    tg_arquivos.restaurar()
    caminho = TG_DIR / nome
    if not caminho.exists() or caminho.suffix.lower() != ".csv":
        return [], []
    raw = caminho.read_bytes()
    txt = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig") if raw[:3] == b"\xef\xbb\xbf"
           else raw.decode("latin-1")).replace("\x00", "")
    linhas = list(_csv.reader(_io.StringIO(txt), delimiter=";"))
    if not linhas:
        return [], []
    cab = [c.strip() or f"(coluna {k + 1})" for k, c in enumerate(linhas[0])]
    curto = RE_DOC_ID_CURTO.search(identificador)
    ug = identificador[:6] if RE_DOC_ID_COMPLETO.fullmatch(identificador) else None
    achadas = []
    for row in linhas[1:]:
        s = ";".join(row)
        if identificador in s or (curto and ug and curto.group(0) in s and ug in s):
            achadas.append({cab[k] if k < len(cab) else f"(coluna {k + 1})": v for k, v in enumerate(row)})
            if len(achadas) >= limite:
                break
    return cab, achadas


def documento_origem(con: sqlite3.Connection, processo: str, documento: str, chave: str | None) -> dict:
    ident = doc_id(documento, chave)
    curto = (RE_DOC_ID_CURTO.search(ident) or RE_DOC_ID_CURTO.search(chave or "") or [None])
    curto = curto[0] if curto else None
    secoes = []

    def sec(titulo, linhas, nota=None):
        if linhas:
            cols = list(dict.fromkeys(k for l in linhas for k in l.keys()))
            secoes.append({"titulo": titulo, "colunas": cols, "linhas": linhas, "nota": nota})

    join = [dict(r) for r in con.execute("SELECT * FROM tg_execucao_join WHERE processo_sei=?", (processo,)) if _casa_id(ident, r["documento"])]
    sec("Vínculo no RADAR — tg_execucao_join (este processo)", join, "como o documento foi ligado ao processo: etapa, arquivo de origem e método")
    arquivos = list(dict.fromkeys(str(r.get("arquivo_origem") or "").split(" [")[0] for r in join))
    for nome in arquivos:
        if nome.lower().endswith(".csv"):
            _, achadas = _linhas_arquivo_tg(nome, ident)
            sec(f"Tesouro Gerencial — {nome} (linhas brutas do arquivo)", achadas)
    if "OB" in ident and con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_ob_detalhe'").fetchone():
        sec("Detalhe da OB — tg_ob_detalhe", [dict(r) for r in con.execute("SELECT * FROM tg_ob_detalhe WHERE ob LIKE ?", (f"%{curto or ident}",))
                                               if _casa_id(ident, r["ob"]) or not RE_DOC_ID_COMPLETO.fullmatch(ident)])
    if curto and "PF" in curto:
        for tab, col in (("tg_pf_documento", "pf"), ("tg_pf_observacao", "pf"), ("siafi_pf_liberadas", "pf")):
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (tab,)).fetchone():
                sec(tab, [dict(r) for r in con.execute(f"SELECT * FROM {tab} WHERE {col} LIKE ?", (f"%{curto}",))][:30])
    if curto and "NC" in curto and con.execute("SELECT 1 FROM sqlite_master WHERE name='siafi_nc_transferencia'").fetchone():
        cols = [c[1] for c in con.execute("PRAGMA table_info(siafi_nc_transferencia)")]
        if "nc" in cols:
            sec("siafi_nc_transferencia", [dict(r) for r in con.execute("SELECT * FROM siafi_nc_transferencia WHERE nc LIKE ?", (f"%{curto}",))][:30])
    # Transferegov (SICONV / Módulo TED): linhas que citam o documento em qualquer coluna de texto
    alvo = curto or ident
    if alvo:
        for (tab,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'transferegov_siconv_%' OR name LIKE 'transferegov_ted_%')"):
            cols = [c[1] for c in con.execute(f'PRAGMA table_info("{tab}")') if c[1] not in ("_raw_json",)]
            cond = " OR ".join(f'instr(CAST("{c}" AS TEXT), ?) > 0' for c in cols)
            try:
                achadas = [dict(r) for r in con.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{tab}" WHERE {cond} LIMIT 20', [alvo] * len(cols))]
            except sqlite3.Error:
                achadas = []
            sec(f"Transferegov — {tab}", achadas)
    try:
        sec("Adicionado manualmente — tc_empenhos_manuais", [dict(r) for r in con.execute(
            "SELECT * FROM tc_empenhos_manuais WHERE processo_sei=? AND (documento LIKE ? OR chave LIKE ?)", (processo, f"%{alvo}%", f"%{alvo}%"))])
    except sqlite3.Error:
        pass
    ja = ident in desvinculados(con, processo)
    return {"processo": processo, "documento": documento, "identificador": ident, "desvinculado": ja, "secoes": secoes}


@bp.get("/api/instrumentos/documento-origem")
def api_instrumentos_documento_origem():
    processo = (request.args.get("processo") or "").strip()
    documento = (request.args.get("documento") or "").strip()
    if not processo or not documento:
        return jsonify({"sucesso": False, "erro": "Informe processo e documento."}), 400
    try:
        with _db() as con:
            return jsonify({"sucesso": True, **documento_origem(con, processo, documento, request.args.get("chave"))})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/desvincular")
def api_instrumentos_desvincular():
    """Desvincula o documento do processo: grava em `desvinculos` e apaga as linhas dele no tg_execucao_join deste processo
    (o documento continua nos arquivos do TG = base comum; o criar_tg_execucao_join.py não volta a ligá-lo a este processo)."""
    d = request.get_json(silent=True) or {}
    processo, documento = (d.get("processo") or "").strip(), (d.get("documento") or "").strip()
    if not processo or not documento:
        return jsonify({"sucesso": False, "erro": "Informe processo e documento."}), 400
    try:
        with _db() as con:
            ident = doc_id(documento, d.get("chave"))
            _garantir_tabelas_vinculo(con)
            linhas = [dict(r) for r in con.execute("SELECT * FROM tg_execucao_join WHERE processo_sei=?", (processo,)) if _casa_id(ident, r["documento"])]
            ids = [r["id"] for r in linhas]
            con.execute("INSERT OR REPLACE INTO desvinculos (processo_sei, chave, etapa, documento, motivo, criado_em, linhas_json) VALUES (?,?,?,?,?,?,?)",
                        (processo, ident, d.get("etapa"), documento, d.get("motivo") or "desvinculado pelo usuário no painel", agora_iso(),
                         json.dumps(linhas, ensure_ascii=False)))
            con.execute("DELETE FROM vinculos_manuais WHERE chave=? AND processo_destino=?", (ident, processo))   # vínculo manual aqui desfeito
            con.executemany("DELETE FROM tg_execucao_join WHERE id=?", [(i,) for i in ids])
            con.commit()
        escrever_log(f"desvinculado {ident} do processo {processo} ({len(ids)} linha(s) do tg_execucao_join)")
        return jsonify({"sucesso": True, "identificador": ident, "linhas_removidas": len(ids)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- DETALHE dos cartões em OUTROS: tudo o que há na base (26/09/2026)
RE_PROC_SEI = re.compile(r"(?<!\d)(\d{5})\.?(\d{6})/?(\d{4})-?(\d{2})(?!\d)")


def _procs_no_texto(texto: str) -> list[str]:
    """Números com cara de processo SEI (NNNNN.NNNNNN/AAAA-DD, com ou sem pontuação), normalizados."""
    saida = []
    for a, b, c, d in RE_PROC_SEI.findall(texto or ""):
        if 1990 <= int(c) <= 2035:
            saida.append(f"{a}.{b}/{c}-{d}")
    return saida


def base_completa(con: sqlite3.Connection, processo: str) -> dict:
    """Tudo o que a base tem sobre o instrumento: cadastro, documentos, cada linha do tg_execucao_join com a linha bruta do arquivo de
    origem, e os números de processo SEI citados em qualquer campo (contados)."""
    import csv as _csv
    import io as _io
    from radar_backend import tg_arquivos
    from radar_backend.radar_config import TG_DIR
    inst = con.execute("SELECT * FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
    if inst is None:
        return {}
    inst = dict(inst)
    cad = [dict(r) for r in con.execute("SELECT * FROM cadastro WHERE processo_sei=?", (processo,))] if \
        "processo_sei" in {c[1] for c in con.execute("PRAGMA table_info(cadastro)")} else []
    docs = [dict(r) for r in con.execute("SELECT documento_sei, tipo_documento, data_assinatura, numero_instrumento, substr(objeto,1,300) AS objeto "
                                         "FROM documentos WHERE processo_sei=?", (processo,))]
    linhas = [dict(r) for r in con.execute("SELECT etapa, documento, valor, data, arquivo_origem, metodo_match FROM tg_execucao_join "
                                           "WHERE processo_sei=? ORDER BY etapa, data, documento", (processo,))]
    # linha bruta: um índice por arquivo de origem (doc SIAFI completo -> linha {coluna: valor})
    tg_arquivos.restaurar()
    indices: dict[str, dict] = {}
    for arq in {str(l["arquivo_origem"] or "").split(" [")[0] for l in linhas}:
        caminho = TG_DIR / arq
        if not arq.lower().endswith(".csv") or not caminho.exists():
            continue
        raw = caminho.read_bytes()
        txt = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig") if raw[:3] == b"\xef\xbb\xbf"
               else raw.decode("latin-1")).replace("\x00", "")
        rows = list(_csv.reader(_io.StringIO(txt), delimiter=";"))
        if not rows:
            continue
        cab = [c.strip() or f"col{k + 1}" for k, c in enumerate(rows[0])]
        idx = {}
        for row in rows[1:]:
            d = {cab[k] if k < len(cab) else f"col{k + 1}": v for k, v in enumerate(row) if str(v).strip() not in ("", "-8", "-9")}
            for m in RE_DOC_ID_COMPLETO.finditer(";".join(row)):
                idx.setdefault(m.group(0), d)
        indices[arq] = idx
    procs = collections.Counter()
    for l in linhas:
        arq = str(l["arquivo_origem"] or "").split(" [")[0]
        m = RE_DOC_ID_COMPLETO.findall(l["documento"] or "")
        bruta = next((indices.get(arq, {}).get(x) for x in m if indices.get(arq, {}).get(x)), None)
        l["origem"] = bruta or {}
        for p in _procs_no_texto(" ".join(map(str, (l["origem"] or {}).values())) + " " + str(l["documento"])):
            procs[p] += 1
    for bloco in [inst] + cad + docs:
        for p in _procs_no_texto(" ".join(str(v) for v in bloco.values() if v is not None)):
            procs[p] += 1
    tem_processo = bool(RE_PROC_SEI.fullmatch(processo.replace(".", "").replace("/", "").replace("-", "")) or _procs_no_texto(processo))
    cadastrados = {p for (p,) in con.execute("SELECT processo_sei FROM instrumentos")}
    return {"instrumento": inst, "cadastro": cad, "documentos": docs, "linhas": linhas, "tem_processo": tem_processo,
            "processos_citados": [{"processo": p, "vezes": n, "e_deste": p == processo, "outro_instrumento": p in cadastrados and p != processo}
                                  for p, n in procs.most_common()]}


@bp.get("/api/instrumentos/base-completa")
def api_instrumentos_base_completa():
    processo = (request.args.get("processo") or "").strip()
    try:
        with _db() as con:
            d = base_completa(con, processo)
        if not d:
            return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
        return jsonify({"sucesso": True, **d})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- botão PI do DETALHE (26/09/2026)
def pis_do_instrumento(con: sqlite3.Connection, processo: str) -> dict:
    from radar_backend import pi_util
    from radar_backend import tg_arquivos
    from radar_backend.radar_config import TG_DIR
    pi_util.criar_tabelas(con)
    tg_arquivos.restaurar()
    mp = pi_util.mapas(con, TG_DIR)
    por: dict[str, dict] = {}
    for etapa, doc, valor, metodo in con.execute("SELECT etapa, documento, valor, metodo_match FROM tg_execucao_join WHERE processo_sei=?", (processo,)):
        pi = pi_util.pi_da_linha(etapa, doc, mp) or ""          # "" = documento sem PI identificado na base
        e = por.setdefault(pi, {"pi": pi, "nome": mp["nomes"].get(pi), "n": 0, "etapas": {}})
        e["n"] += 1
        sinal = -1 if str(metodo or "").endswith("_ANULACAO") else 1
        x = e["etapas"].setdefault(etapa, {"n": 0, "valor": 0.0})
        x["n"] += 1
        x["valor"] = round(x["valor"] + sinal * float(valor or 0), 2)
    manuais = {pi for (pi,) in con.execute("SELECT pi FROM instrumento_pis WHERE processo_sei=?", (processo,))}
    for pi in manuais:
        por.setdefault(pi, {"pi": pi, "nome": mp["nomes"].get(pi), "n": 0, "etapas": {}})
    for e in por.values():
        e["adicionado_pelo_usuario"] = e["pi"] in manuais
    desv = [pi for (pi,) in con.execute("SELECT pi FROM pis_desvinculados WHERE processo_sei=? ORDER BY criado_em", (processo,))]
    sem_pi = por.pop("", None)
    return {"pis": sorted(por.values(), key=lambda e: -e["n"]), "sem_pi": sem_pi, "desvinculados": desv}


def _notas_divergencia_pi(processo: str) -> list[dict]:
    """Aviso no DETALHE: NE com PI de um instrumento e nº de processo de outro (divergencias_pi, refeita a cada join). Não desvincula nada."""
    from radar_backend import pi_util
    with _db() as con:
        divs = pi_util.divergencias_do_processo(con, processo)
        rot = {p: r for p, r in con.execute("SELECT processo_sei, COALESCE(tipo_instrumento, processo_sei) FROM instrumentos")}
    if not divs:
        return []
    nome = lambda p: f"{p} ({rot[p]})" if p in rot else p
    linhas = [f"NE {d['ne']}: PI {d['pi']} é de {nome(d['processo_do_pi'])}, mas o nº de processo da NE ({d['processo_campo']}) é de "
              f"{nome(d['processo_da_ne'])}; hoje vinculada a {d['vinculada_a'] or 'nenhum instrumento'}." for d in divs]
    return [{"titulo": f"DIVERGÊNCIA PI x PROCESSO ({len(divs)} documento(s)) — não desvinculado automaticamente",
             "texto": "\n".join(linhas), "fonte": "Tesouro Gerencial (PI e nº de processo da própria NE) — conferir e decidir no livro NE (DESVINCULAR/VINCULAR)",
             "documento_sei": None, "data": None}]


def _rodar_join() -> str:
    r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "criar_tg_execucao_join.py")], capture_output=True, text=True, timeout=900, cwd=str(BASE_DIR))
    if r.returncode != 0:
        raise RuntimeError("criar_tg_execucao_join.py falhou: " + (r.stderr or r.stdout)[-800:])
    return r.stdout


@bp.get("/api/instrumentos/pis")
def api_instrumentos_pis():
    processo = (request.args.get("processo") or "").strip()
    try:
        with _db() as con:
            return jsonify({"sucesso": True, "processo": processo, **pis_do_instrumento(con, processo)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/pis/desvincular")
def api_instrumentos_pis_desvincular():
    """DESVINCULAR PI: grava em pis_desvinculados e apaga já as linhas do instrumento com esse PI (o join as mantém fora)."""
    from radar_backend import pi_util
    from radar_backend.radar_config import TG_DIR
    d = request.get_json(silent=True) or {}
    processo, pi = (d.get("processo") or "").strip(), (d.get("pi") or "").strip()
    if not processo or not pi:
        return jsonify({"sucesso": False, "erro": "Informe processo e PI."}), 400
    try:
        with _db() as con:
            pi_util.criar_tabelas(con)
            con.execute("INSERT OR REPLACE INTO pis_desvinculados VALUES (?,?,?)", (processo, pi, agora_iso()))
            con.execute("DELETE FROM instrumento_pis WHERE processo_sei=? AND pi=?", (processo, pi))
            mp = pi_util.mapas(con, TG_DIR)
            divergentes = {d["ne"] for d in pi_util.divergencias_do_processo(con, processo) if d["pi"] == pi}
            ids, mantidas = [], set()
            for r in con.execute("SELECT id, etapa, documento FROM tg_execucao_join WHERE processo_sei=?", (processo,)):
                if pi_util.pi_da_linha(r["etapa"], r["documento"], mp) != pi:
                    continue
                if pi_util.ne_do_documento(r["documento"]) in divergentes:   # PI x processo divergentes: não sai automaticamente
                    mantidas.add(pi_util.ne_do_documento(r["documento"]))
                else:
                    ids.append(r["id"])
            con.executemany("DELETE FROM tg_execucao_join WHERE id=?", [(i,) for i in ids])
            con.commit()
        escrever_log(f"PI {pi} desvinculado do processo {processo} ({len(ids)} linha(s); {len(mantidas)} NE(s) mantidas por divergência)")
        return jsonify({"sucesso": True, "linhas_removidas": len(ids), "mantidas_divergencia": sorted(mantidas)})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/pis/adicionar")
def api_instrumentos_pis_adicionar():
    """ADICIONAR PI: grava em instrumento_pis e roda o criar_tg_execucao_join.py, que liga ao instrumento todo documento ainda sem
    vínculo com esse PI (NE, NC, OB pela NE, saldo, execução agregada, planejamento). Documentos com o PI já ligados a OUTRO
    instrumento não são tomados — a resposta os lista."""
    from radar_backend import pi_util
    from radar_backend.radar_config import TG_DIR
    d = request.get_json(silent=True) or {}
    processo, pi = (d.get("processo") or "").strip(), (d.get("pi") or "").strip().upper()
    if not processo or not pi:
        return jsonify({"sucesso": False, "erro": "Informe processo e PI."}), 400
    try:
        with _db() as con:
            pi_util.criar_tabelas(con)
            dono = con.execute("SELECT processo_sei FROM instrumento_pis WHERE pi=? AND processo_sei<>?", (pi, processo)).fetchone()
            if dono:
                return jsonify({"sucesso": False, "erro": f"O PI {pi} já foi adicionado ao processo {dono[0]}."}), 409
            antes = con.execute("SELECT COUNT(*) FROM tg_execucao_join WHERE processo_sei=?", (processo,)).fetchone()[0]
            con.execute("INSERT OR REPLACE INTO instrumento_pis VALUES (?,?,?)", (processo, pi, agora_iso()))
            con.execute("DELETE FROM pis_desvinculados WHERE processo_sei=? AND pi=?", (processo, pi))
            con.commit()
        _rodar_join()
        with _db() as con:
            depois = con.execute("SELECT COUNT(*) FROM tg_execucao_join WHERE processo_sei=?", (processo,)).fetchone()[0]
            mp = pi_util.mapas(con, TG_DIR)
            outros = {}
            for p, etapa, doc in con.execute("SELECT processo_sei, etapa, documento FROM tg_execucao_join WHERE processo_sei<>?", (processo,)):
                if pi_util.pi_da_linha(etapa, doc, mp) == pi:
                    outros[p] = outros.get(p, 0) + 1
        escrever_log(f"PI {pi} adicionado ao processo {processo}: {depois - antes} linha(s) novas")
        return jsonify({"sucesso": True, "linhas_novas": depois - antes, "em_outros_instrumentos": outros})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/notas")
def api_instrumentos_notas():
    """Notas do instrumento (instrumento_notas, gravadas por scripts/registrar_decisoes.NOTAS_INSTRUMENTO) — quadro DETALHE."""
    processo = (request.args.get("processo") or "").strip()
    try:
        with _db() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='instrumento_notas'").fetchone():
                return jsonify({"sucesso": True, "notas": _notas_divergencia_pi(processo)})
            notas = [dict(r) for r in con.execute("SELECT titulo, texto, fonte, documento_sei, data FROM instrumento_notas WHERE processo_sei=? "
                                                  "ORDER BY data DESC, id", (processo,))]
        return jsonify({"sucesso": True, "notas": _notas_divergencia_pi(processo) + notas})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/desvinculados")
def api_instrumentos_desvinculados():
    """Documentos desvinculados (de qualquer processo) disponíveis para o botão VINCULAR do livro. Os do próprio processo vêm primeiro
    (revincular = desfazer); os de outro processo podem ser vinculados a este (passam a ser deste processo, inclusive nas próximas cargas)."""
    processo = (request.args.get("processo") or "").strip()
    try:
        with _db() as con:
            _garantir_tabelas_vinculo(con)
            ja = {(r["chave"], r["processo_origem"]): r["processo_destino"] for r in con.execute("SELECT * FROM vinculos_manuais")}
            lista = []
            for r in con.execute("SELECT d.*, i.tipo_instrumento, i.localidades FROM desvinculos d LEFT JOIN instrumentos i USING (processo_sei) "
                                 "ORDER BY (d.processo_sei = ?) DESC, d.criado_em DESC", (processo,)):
                if (r["chave"], r["processo_sei"]) in ja:
                    continue                                    # já vinculado manualmente a outro processo
                linhas = json.loads(r["linhas_json"] or "[]")
                lista.append({"chave": r["chave"], "processo_origem": r["processo_sei"], "deste_processo": r["processo_sei"] == processo,
                              "origem_rotulo": " — ".join(x for x in (r["tipo_instrumento"], r["localidades"]) if x) or r["processo_sei"],
                              "etapa": r["etapa"], "documento": r["documento"], "criado_em": r["criado_em"],
                              "valor": round(sum(float(l.get("valor") or 0) for l in linhas), 2), "n_linhas": len(linhas)})
        return jsonify({"sucesso": True, "processo": processo, "documentos": lista})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/vincular")
def api_instrumentos_vincular():
    """Vincula ao processo um documento desvinculado: do próprio processo -> desfaz o desvínculo; de outro -> vínculo manual
    (vinculos_manuais; o criar_tg_execucao_join.py passa a pôr o documento neste processo). As linhas guardadas no desvínculo voltam já
    ao tg_execucao_join, com o processo de destino."""
    d = request.get_json(silent=True) or {}
    processo, chave, origem = (d.get("processo") or "").strip(), (d.get("chave") or "").strip(), (d.get("processo_origem") or "").strip()
    if not processo or not chave or not origem:
        return jsonify({"sucesso": False, "erro": "Informe processo, chave e processo de origem."}), 400
    try:
        with _db() as con:
            _garantir_tabelas_vinculo(con)
            r = con.execute("SELECT * FROM desvinculos WHERE processo_sei=? AND chave=?", (origem, chave)).fetchone()
            if r is None:
                return jsonify({"sucesso": False, "erro": "Documento não está entre os desvinculados."}), 404
            linhas = json.loads(r["linhas_json"] or "[]")
            if origem == processo:
                con.execute("DELETE FROM desvinculos WHERE processo_sei=? AND chave=?", (origem, chave))
                metodo_novo = None
            else:
                con.execute("INSERT OR REPLACE INTO vinculos_manuais VALUES (?,?,?,?,?,?)", (chave, origem, processo, r["etapa"], r["documento"], agora_iso()))
                metodo_novo = "MANUAL_USUARIO"
            for l in linhas:
                con.execute("INSERT INTO tg_execucao_join (processo_sei, etapa, documento, valor, data, arquivo_origem, metodo_match, coletado_em) "
                            "VALUES (?,?,?,?,?,?,?,?)", (processo, l.get("etapa"), l.get("documento"), l.get("valor"), l.get("data"),
                                                         l.get("arquivo_origem"), metodo_novo or l.get("metodo_match"), agora_iso()))
            con.commit()
        escrever_log(f"vinculado {chave} ao processo {processo} (origem {origem}, {len(linhas)} linha(s))")
        return jsonify({"sucesso": True, "linhas": len(linhas), "revinculado": origem == processo})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/rap")
def api_instrumentos_rap():
    """RESTOS A PAGAR do instrumento (tg_rap, do db_bi_tesgerencial.csv — pedido do usuário, 28/09/2026).
    Movimentos mensais por exercício: janeiro = inscrição; depois pagos/cancelados abatem o "a pagar".
    RAP a pagar = soma do item a_pagar no exercício de referência (o mais recente da base)."""
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            tem = con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_rap'").fetchone()
            if not tem:
                return jsonify({"sucesso": True, "processo": processo, "exercicio": None, "resumo": None, "por_ano": [], "linhas": [], "meses": [],
                                "safras": []})
            ref = con.execute("SELECT MAX(exercicio) FROM tg_rap").fetchone()[0]
            itens = ("inscrito", "pago", "cancelado", "a_pagar")

            def agrega(sql, args, chaves):
                out = {}
                for r in con.execute(sql, args):
                    k = tuple(r[:len(chaves)])
                    d = out.setdefault(k, {**dict(zip(chaves, k)), **{i: 0.0 for i in itens}})
                    d[r[-2]] = round(d[r[-2]] + (r[-1] or 0), 2)
                return list(out.values())

            por_ano = agrega("SELECT exercicio, item, SUM(valor) FROM tg_rap WHERE processo_sei=? GROUP BY exercicio, item ORDER BY exercicio",
                             (processo,), ["exercicio"])
            # UO/PTRES/PO separam linhas que antes vinham somadas (usuário, 28/09/2026)
            linhas = agrega("""SELECT pi, localizador, localizador_nome, nd, nd_nome, uo, ptres, po, MAX(po_nome), metodo, item, SUM(valor) FROM tg_rap
                               WHERE processo_sei=? AND exercicio=? GROUP BY pi, localizador, nd, uo, ptres, po, metodo, item
                               ORDER BY localizador, pi, nd, uo, ptres""",
                            (processo, ref), ["pi", "localizador", "localizador_nome", "nd", "nd_nome", "uo", "ptres", "po", "po_nome", "metodo"])
            # SAFRA (ano da NE) do RAP a pagar da posição mais recente — tg_rap_safra (scripts/criar_tg_execucao_join.rap_safras)
            safras = []
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_rap_safra'").fetchone():
                safras = [dict(r) for r in con.execute(
                    """SELECT safra, safra_ate, uo, ptres, po, po_nome, pi, nd, ROUND(SUM(valor), 2) AS valor, MAX(metodo) AS metodo FROM tg_rap_safra
                       WHERE processo_sei=? GROUP BY safra, safra_ate, uo, ptres, po, pi, nd ORDER BY safra, uo, ptres""", (processo,))]
            meses = agrega("SELECT mes, item, SUM(valor) FROM tg_rap WHERE processo_sei=? AND exercicio=? GROUP BY mes, item ORDER BY mes",
                           (processo, ref), ["mes"])
        resumo = next((a for a in por_ano if a["exercicio"] == ref), {"exercicio": ref, **{i: 0.0 for i in itens}})
        linhas = [l for l in linhas if any(abs(l[i]) >= 0.01 for i in itens)]
        meses = [m for m in meses if any(abs(m[i]) >= 0.01 for i in itens)]
        from radar_backend.gerencial import RAP_CANCELAR_DE, RAP_CANCELAR_ATE, rap_parcelas
        # RAP a pagar = a MESMA regra do GERENCIAL (usuário, 01/10/2026): NEs até 2023 pela Avaliação RPNP (NE a NE), de 2024 em diante
        # pelo Tesouro Gerencial. O "a pagar" só do Tesouro Gerencial fica em a_pagar_tg (ex.: Feira de Santana, NE 2019NE000055 sem PI
        # no localizador NACIONAL: TG = 0, RPNP = 45.800).
        with _db() as con3:
            parc = rap_parcelas(con3).get(processo, [])
            tem_rpnp = bool(con3.execute("SELECT 1 FROM sqlite_master WHERE name='rpnp_avaliacao'").fetchone())
        if tem_rpnp:
            resumo = {**resumo, "a_pagar_tg": resumo.get("a_pagar"), "a_pagar": round(sum(x["valor"] for x in parc), 2)}
        # AVALIAÇÃO RPNP por NE (planilhas AVALIAÇÃO RPNP — rpnp_avaliacao), 30/09/2026
        with _db() as con2:
            rpnp = [dict(r) for r in con2.execute("SELECT ne, ano_inscricao, localizador, ptres, po, nd, fonte, saldo, localidade, status, classificacao, estagio, "
                                                   "avaliacao, arquivo FROM rpnp_avaliacao WHERE processo_sei=? ORDER BY ne", (processo,))] \
                if con2.execute("SELECT 1 FROM sqlite_master WHERE name='rpnp_avaliacao'").fetchone() else []
        return jsonify({"sucesso": True, "processo": processo, "exercicio": ref, "resumo": resumo, "por_ano": por_ano,
                        "linhas": linhas, "meses": meses, "safras": safras, "rpnp": rpnp, "parcelas": parc if tem_rpnp else [],
                        "cancelar_safras": [ref - RAP_CANCELAR_DE, ref - RAP_CANCELAR_ATE] if ref else None,
                        "fonte": "Tesouro Gerencial — db_bi_tesgerencial.csv (itens RAP inscritos / pagos / cancelados / a pagar, proc. e não proc.)"})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def livros_filtrados(con: sqlite3.Connection, processo: str) -> dict | None:
    """livros_do_processo sem os documentos DESVINCULADOS pelo usuário (saem de todas as fontes do livro). Usado pelo botão do
    livro no DETALHE e pelo GERENCIAL — os dois mostram os mesmos números."""
    d = livros_do_processo(con, processo)
    fora = desvinculados(con, processo)
    if d is None or not fora:
        return d
    for s in d["secoes"]:
        if not s.get("livro"):
            continue
        s["linhas"] = [l for l in s["linhas"] if doc_id(l.get("documento"), l.get("_chave")) not in fora and l.get("_chave") not in fora]
        cons = [l for l in s["linhas"] if l.get("considerado")]
        s["total"] = {"campo": "valor", "valor": L.total(s["linhas"])}
        s["n_entradas"] = sum(1 for l in cons if l.get("movimento") == "Entrada")
        s["n_saidas"] = sum(1 for l in cons if l.get("movimento") == "Saída")
        s["n_nao_utilizados"] = sum(1 for l in s["linhas"] if not l.get("considerado"))
    return d


@bp.get("/api/instrumentos/livro")
def api_instrumentos_livro():
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            d = livros_filtrados(con, processo)
        if d is None:
            return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
        return jsonify({"sucesso": True, **d})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


def _execucao_por_pi(con: sqlite3.Connection, processo: str) -> list[dict] | None:
    """Empenho líquido, pago e a pagar por PI (= localidade, ex. cada aeroporto do Aporte Infraero), a partir do tg_execucao_join
    (EMPENHO/PAGAMENTO) e do PI de cada NE (tg_ne_pi_historico). Só quando o processo tem 2+ PIs (pedido do usuário, 26/09/2026)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_ne_pi_historico'").fetchone():
        return None
    pi_ne: dict[tuple, str] = {}
    for ne, pi in con.execute("SELECT ne, pi FROM tg_ne_pi_historico"):
        m = re.match(r"(\d{6})\d{5}(\d{4}NE\d{6})$", ne or "")
        if m:
            pi_ne.setdefault((m.group(1), m.group(2)), pi)
    rotulos = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='subdivisao_pi'").fetchone():
        rotulos = {pi: loc for pi, loc in con.execute("SELECT pi, localidade FROM subdivisao_pi WHERE processo_sei=?", (processo,))}
    por: dict[str, dict] = {}
    for etapa, doc, valor, metodo in con.execute("SELECT etapa, documento, valor, metodo_match FROM tg_execucao_join WHERE processo_sei=? "
                                                 "AND etapa IN ('EMPENHO','PAGAMENTO') AND valor IS NOT NULL", (processo,)):
        m = re.search(r"(\d{4}NE\d{6})", doc or "")
        if not m:
            continue
        pi = pi_ne.get((str(doc)[:6], m.group(1)))
        if not pi:
            continue
        e = por.setdefault(pi, {"pi": pi, "localidade": rotulos.get(pi) or pi, "empenho": 0.0, "pago": 0.0, "nes": set()})
        sinal = -1 if str(metodo or "").endswith("_ANULACAO") else 1
        e["empenho" if etapa == "EMPENHO" else "pago"] += sinal * float(valor)
        e["nes"].add(m.group(1))
    if len(por) < 2:
        return None
    saida = []
    for e in sorted(por.values(), key=lambda x: -x["empenho"]):
        saida.append({**e, "empenho": round(e["empenho"], 2), "pago": round(e["pago"], 2), "a_pagar": round(e["empenho"] - e["pago"], 2),
                      "nes": sorted(e["nes"])})
    return saida


@bp.get("/api/instrumentos/financeiro")
def api_instrumentos_financeiro():
    """PF+TRF (para TED) ou Crédito/Empenho/Pagamento de tg_execucao_join (para os demais tipos) —
    complementa o painel de ações do modo KANBAN."""
    processo = (request.args.get("processo") or "").strip()
    if not processo:
        return jsonify({"sucesso": False, "erro": "Informe o processo."}), 400
    try:
        with _db() as con:
            r = con.execute("SELECT numero_siafi, tipo_instrumento FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone()
            if r is None:
                return jsonify({"sucesso": False, "erro": "Instrumento não encontrado."}), 404
            vinc = _vinculos(con, processo, r["numero_siafi"])
            pf = _pf_ted(con, processo, vinc) if vinc.get("ted") else None
            if pf is None and _categoria(r["tipo_instrumento"], processo) == "TED":
                pf = _pf_ted_sem_modulo(con, processo)     # TED fora do Módulo TED: PF do SIAFI = TRF (usuário, 25/09/2026)
            execucao_tg = None if vinc.get("ted") else _execucao_tg(con, processo)
            if not vinc.get("ted") and _categoria(r["tipo_instrumento"], processo) in ("TC", "CONVÊNIO"):
                siafi = str((vinc.get("tc") or {}).get("nr_convenio") or r["numero_siafi"] or "").strip()
                execucao_tg = _execucao_tc(con, processo, siafi)
            por_pi = _execucao_por_pi(con, processo)
        return jsonify({"sucesso": True, "processo": processo, "tipo": "TED" if (vinc.get("ted") or pf) else ("TC" if vinc.get("tc") else None),
                        "pf": pf, "execucao_tg": execucao_tg, "nr_convenio": (vinc.get("tc") or {}).get("nr_convenio"), "por_pi": por_pi})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
