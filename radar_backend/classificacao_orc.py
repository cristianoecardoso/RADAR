"""CLASSIFICAÇÃO ORÇAMENTÁRIA do instrumento — Programa de Trabalho, Ação e PO (ficha ✏️ do KANBAN; usuário, 02/10/2026).

Localização, por PTRES:
  1. NEs do instrumento (tg_execucao_join: EMPENHO / SALDO_NE / NE_CCOR) -> PTRES da própria NE (CSVs de NE do Tesouro Gerencial)
  2. NCs do instrumento (tg_execucao_join: CREDITO)                       -> PTRES da NC (radar_nc.csv, nc_unificado*.csv)
  3. RAP do instrumento (tg_rap) e Avaliação RPNP (rpnp_avaliacao)        -> PTRES da linha
  4. PIs do instrumento (os do botão PI), só se 1-3 não acharem nada     -> PTRES do PI em db_bi_tesgerencial.csv
  PTRES -> função.subfunção.programa.ação.localizador (Programa de Trabalho), Ação e PO: db_bi_tesgerencial.csv (e sac_bi.csv).
  NC de transferência lida dos documentos SEI (siafi_nc_transferencia.programa_trabalho) entra direto, sem PTRES.
Nada é gravado: é leitura das fontes a cada abertura da ficha (mapas em cache enquanto os CSVs não mudam).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from radar_backend.pi_util import _ler_csv
from radar_backend.radar_config import TG_DIR

RE_NE = re.compile(r"\d{11}\d{4}NE\d{6}")
RE_NC = re.compile(r"\d{11}\d{4}NC\d{6}")
COLS_NE_PTRES = (("NE", "NE - PTRES"), ("Documento Origem", "NE - PTRES"), ("documento", "ne_ptres"), ("ne", "ne_ptres"))
COLS_NC_PTRES = (("NC", "NC Célula - PTRES"), ("nc", "ptres"))
_CACHE: dict = {"chave": None, "mapas": None}


def _ptres(x) -> str:
    x = str(x or "").strip()
    return "" if x in ("", "-8", "-9", "0") else x.lstrip("0") or x


def _po(x) -> str:
    x = str(x or "").strip()
    return x.zfill(4) if x.isdigit() else x


def _pt(funcao, subfuncao, programa, acao, localizador) -> str:
    loc = str(localizador or "")[-4:]
    partes = [str(funcao or ""), str(subfuncao or ""), str(programa or ""), str(acao or ""), loc]
    return ".".join(partes) if all(partes) else ""


def _mapas() -> dict:
    arquivos = sorted(TG_DIR.glob("*.csv"))
    chave = tuple((p.name, p.stat().st_mtime) for p in arquivos)
    if _CACHE["chave"] == chave:
        return _CACHE["mapas"]
    ne_ptres, nc_ptres, pi_ptres, info = {}, {}, {}, {}
    for caminho in arquivos:
        h, dados = _ler_csv(caminho)
        if caminho.name == "db_bi_tesgerencial.csv":
            ix = {c: h.index(c) for c in ("ptres", "funcao", "subfuncao", "programa", "acao", "acao_nome", "localizador",
                                          "localizador_nome", "po", "po_nome", "pi", "uo") if c in h}
            for r in dados:
                if len(r) <= max(ix.values()):
                    continue
                g = {c: r[i].strip() for c, i in ix.items()}
                p = _ptres(g.get("ptres"))
                if not p:
                    continue
                info.setdefault(p, g)
                if g.get("pi") not in (None, "", "-8", "-9"):
                    pi_ptres.setdefault(g["pi"], set()).add(p)
            continue
        if caminho.name == "sac_bi.csv" and "PTRES" in h:          # cabeçalho com colunas deslocadas: posições fixas do relatório
            i_p = h.index("PTRES")
            for r in dados:
                if len(r) > max(i_p, 12) and _ptres(r[i_p]) and _ptres(r[i_p]) not in info:
                    info[_ptres(r[i_p])] = {"acao": r[2].strip(), "acao_nome": r[3].strip(), "localizador": r[4].strip(), "localizador_nome": r[5].strip(),
                                            "uo": r[6].strip(), "funcao": r[7].strip(), "subfuncao": r[8].strip(), "programa": r[9].strip(),
                                            "po": r[11].strip(), "po_nome": r[12].strip()}
            continue
        for c_doc, c_pt, destino, rx in [(a, b, ne_ptres, RE_NE) for a, b in COLS_NE_PTRES] + [(a, b, nc_ptres, RE_NC) for a, b in COLS_NC_PTRES]:
            if c_doc in h and c_pt in h:
                a, b = h.index(c_doc), h.index(c_pt)
                for r in dados:
                    if max(a, b) < len(r):
                        m, p = rx.fullmatch(r[a].strip()), _ptres(r[b])
                        if m and p:
                            destino.setdefault(m.group(0), p)
                break
    _CACHE.update(chave=chave, mapas={"ne": ne_ptres, "nc": nc_ptres, "pi": pi_ptres, "info": info})
    return _CACHE["mapas"]


def classificacao_do_instrumento(con: sqlite3.Connection, processo: str, pis: list | None = None) -> dict:
    """Lista de {programa_trabalho, acao, acao_nome, po, po_nome, ptres, localizador_nome, fontes} do instrumento, mais usado primeiro."""
    mp = _mapas()
    por: dict[str, dict] = {}

    def add(ptres, fonte, n=1):
        e = por.setdefault(ptres, {"ptres": ptres, "fontes": {}})
        e["fontes"][fonte] = e["fontes"].get(fonte, 0) + n

    tem = lambda t: con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()
    nes, ncs = set(), set()
    if tem("tg_execucao_join"):
        for etapa, doc in con.execute("SELECT etapa, documento FROM tg_execucao_join WHERE processo_sei=? AND etapa IN "
                                      "('EMPENHO','SALDO_NE','NE_CCOR','CREDITO')", (processo,)):
            (ncs if etapa == "CREDITO" else nes).update((RE_NC if etapa == "CREDITO" else RE_NE).findall(doc or ""))
    for ne in nes:
        if mp["ne"].get(ne):
            add(mp["ne"][ne], "NE")
    for nc in ncs:
        if mp["nc"].get(nc):
            add(mp["nc"][nc], "NC")
    for tabela, fonte in (("tg_rap", "RAP (TG)"), ("rpnp_avaliacao", "Avaliação RPNP")):
        if tem(tabela):
            for (p,) in con.execute(f"SELECT DISTINCT ptres FROM {tabela} WHERE processo_sei=?", (processo,)):
                if _ptres(p):
                    add(_ptres(p), fonte)
    if not por:
        for pi in pis or []:
            for p in mp["pi"].get(pi, ()):
                add(p, f"PI {pi}")
    linhas = []
    for p, e in por.items():
        g = mp["info"].get(p, {})
        linhas.append({"ptres": p, "programa_trabalho": _pt(g.get("funcao"), g.get("subfuncao"), g.get("programa"), g.get("acao"), g.get("localizador")),
                       "acao": g.get("acao") or "", "acao_nome": g.get("acao_nome") or "", "po": _po(g.get("po")), "po_nome": g.get("po_nome") or "",
                       "localizador_nome": g.get("localizador_nome") or "", "uo": g.get("uo") or "",
                       "fontes": ", ".join(f"{k} ({v})" if v > 1 else k for k, v in e["fontes"].items()), "_n": sum(e["fontes"].values())})
    # NC de transferência lida no documento SEI: Programa de Trabalho já completo (sem PTRES)
    if tem("siafi_nc_transferencia"):
        conhecidos = {x["programa_trabalho"].replace(".", "") for x in linhas}
        pts: dict[str, int] = {}
        for (pt,) in con.execute("SELECT programa_trabalho FROM siafi_nc_transferencia WHERE processo_sei=?", (processo,)):
            pt = str(pt or "").strip()
            if len(pt) == 17 and pt not in conhecidos:
                pts[pt] = pts.get(pt, 0) + 1
        for pt, n in pts.items():
            acao = pt[9:13]
            g = next((v for v in mp["info"].values() if v.get("acao") == acao), {})
            linhas.append({"ptres": "", "programa_trabalho": f"{pt[:2]}.{pt[2:5]}.{pt[5:9]}.{acao}.{pt[13:]}", "acao": acao, "acao_nome": g.get("acao_nome") or "",
                           "po": "", "po_nome": "", "localizador_nome": "", "uo": "", "fontes": f"NC SEI ({n})" if n > 1 else "NC SEI", "_n": n})
    linhas.sort(key=lambda x: (-x.pop("_n"), x["programa_trabalho"], x["po"]))
    cad = None
    if tem("cadastro"):
        r = con.execute("SELECT acao, plano_orcamentario FROM cadastro WHERE processo_sei=?", (processo,)).fetchone()
        if r and (r[0] or r[1]):
            cad = {"acao": (r[0] or "").strip(), "po": (r[1] or "").strip()}
    man = None
    if tem("instrumentos_classificacao_manual"):
        r = con.execute("SELECT programa_trabalho, acao, po, ptres, alterado_em FROM instrumentos_classificacao_manual WHERE processo_sei=?", (processo,)).fetchone()
        if r:
            man = dict(zip(("programa_trabalho", "acao", "po", "ptres", "alterado_em"), r))
    return {"linhas": linhas, "cadastro": cad, "manual": man,
            "fonte": "PTRES das NE/NC do instrumento, RAP (TG) e Avaliação RPNP; Programa de Trabalho, Ação e PO do PTRES em db_bi_tesgerencial.csv "
                     "(Tesouro Gerencial). Sem NE/NC: PTRES do PI."}
