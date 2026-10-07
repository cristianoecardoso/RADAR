"""PI (Plano Interno) dos documentos ligados aos instrumentos — botão PI do DETALHE (pedido do usuário, 26/09/2026).

  instrumento_pis     PIs ADICIONADOS pelo usuário a um instrumento (botão ADICIONAR PI): o criar_tg_execucao_join.py liga ao
                      instrumento todo documento AINDA SEM VÍNCULO que tenha esse PI (NE, NC, OB pela NE, saldo, execução agregada,
                      planejamento...). Documento já ligado a outro instrumento não é tomado.
  pis_desvinculados   PIs DESVINCULADOS de um instrumento (botão DESVINCULAR PI): todo documento do instrumento com esse PI sai dele
                      (agora e nas próximas cargas) e volta para a base comum.

  divergencias_pi     NEs cujo PI é de um instrumento e o nº de processo (campo da própria NE) é de OUTRO instrumento: aviso no
                      DETALHE dos instrumentos envolvidos; DESVINCULAR PI não tira essas NEs (usuário, 26/09/2026). Refeita a cada join.

pi_da_linha(etapa, documento, mapas): o PI EXPLÍCITO do documento de uma linha do tg_execucao_join (regra do usuário, 26/09/2026:
DESVINCULAR/ADICIONAR PI só afetam documentos que contêm o PI na própria definição) — execução agregada/planejamento: o próprio
documento ("PI" ou "PI [métrica]"); NC: plano_interno da NC; NE, RO, saldo e conta-corrente da NE: PI da NE (tg_ne_pi_historico).
OB, PF e TRF não trazem PI: None.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path

DDL = (
    "CREATE TABLE IF NOT EXISTS instrumento_pis (processo_sei TEXT NOT NULL, pi TEXT NOT NULL, criado_em TEXT, PRIMARY KEY (processo_sei, pi))",
    "CREATE TABLE IF NOT EXISTS pis_desvinculados (processo_sei TEXT NOT NULL, pi TEXT NOT NULL, criado_em TEXT, PRIMARY KEY (processo_sei, pi))",
    "CREATE TABLE IF NOT EXISTS divergencias_pi (ne TEXT NOT NULL, pi TEXT NOT NULL, processo_do_pi TEXT, processo_da_ne TEXT, "
    "processo_campo TEXT, vinculada_a TEXT, detectado_em TEXT, PRIMARY KEY (ne, pi, processo_campo))",
)
ETAPAS_PI_EXPLICITO = {"EMPENHO", "SALDO_NE", "NE_CCOR", "CREDITO", "EXECUCAO_TG_AGREGADA", "PLANEJAMENTO"}
# colunas (NE, PI, nº do processo) dos arquivos de NE do TG
COLUNAS_NE_PI_PROCESSO = (("NE", "NE - PI", "NE - Núm. Processo"), ("Documento Origem", "NE - PI", "NE - Núm. Processo"),
                          ("documento", "cd_ne_pi", "num_processo"))
RE_NE_COMPLETA = re.compile(r"(\d{6})\d{5}(\d{4}NE\d{6})")
RE_NE_CURTA = re.compile(r"\d{4}NE\d{6}")
RE_NC_COMPLETA = re.compile(r"\d{11}\d{4}NC\d{6}")
ETAPAS_PI_NO_DOCUMENTO = {"EXECUCAO_TG_AGREGADA", "PLANEJAMENTO"}
RE_NOME_TERMO = re.compile(r"^(TC|TED|CV|CONV[EÊ]NIO|CONTRATO|CT|TERMO)\b.*\d+\s*/\s*\d{2,4}", re.I)


def criar_tabelas(con: sqlite3.Connection) -> None:
    for sql in DDL:
        con.execute(sql)


def _ler_csv(caminho: Path) -> tuple[list, list]:
    raw = caminho.read_bytes()
    txt = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig") if raw[:3] == b"\xef\xbb\xbf"
           else raw.decode("latin-1")).replace("\x00", "")
    linhas = list(csv.reader(io.StringIO(txt), delimiter=";"))
    return (linhas[0], linhas[1:]) if linhas else ([], [])


def mapas(con: sqlite3.Connection, tg_dir: Path) -> dict:
    """NE completa -> PI, (UG, NE curta) -> PI, NC completa -> PI e PI -> nome."""
    ne_pi, ne_pi_ug, nc_pi, nomes = {}, {}, {}, {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='tg_ne_pi_historico'").fetchone():
        for ne, pi in con.execute("SELECT ne, pi FROM tg_ne_pi_historico"):
            ne_pi[ne] = pi
            m = RE_NE_COMPLETA.fullmatch(ne or "")
            if m:
                ne_pi_ug.setdefault((m.group(1), m.group(2)), pi)
    for nome, c_nc, c_pi in (("nc_unificado_consolidado.csv", "nc", "plano_interno"), ("radar_nc.csv", "NC", "NC Célula - Plano Interno")):
        caminho = tg_dir / nome
        if not caminho.exists():
            continue
        h, dados = _ler_csv(caminho)
        if c_nc in h and c_pi in h:
            i_nc, i_pi = h.index(c_nc), h.index(c_pi)
            for r in dados:
                if max(i_nc, i_pi) < len(r) and r[i_pi].strip() not in ("", "-8", "-9"):
                    nc_pi.setdefault(r[i_nc].strip(), r[i_pi].strip())
    for nome in ("db_bi_tesgerencial.csv", "radar_ne_historico_saldo.csv"):
        caminho = tg_dir / nome
        if not caminho.exists():
            continue
        h, dados = _ler_csv(caminho)
        if "pi" in h and "pi_nome" in h:
            i, j = h.index("pi"), h.index("pi_nome")
            for r in dados:
                if max(i, j) < len(r):
                    nomes.setdefault(r[i].strip(), r[j].strip())
    return {"ne_pi": ne_pi, "ne_pi_ug": ne_pi_ug, "nc_pi": nc_pi, "nomes": nomes}


def ne_do_documento(documento: str) -> str | None:
    m = RE_NE_COMPLETA.search(str(documento or ""))
    return m.group(0) if m else None


def detectar_divergencias(con: sqlite3.Connection, tg_dir: Path, linhas: list, dono_do_pi, processo_cadastrado, agora: str) -> set:
    """Refaz divergencias_pi: NE com PI de um instrumento (dono_do_pi) e nº de processo (campo da NE) de outro instrumento cadastrado
    (processo_cadastrado). `linhas` = linhas do join (processo, etapa, documento, ...), para dizer a quem a NE está vinculada.
    Devolve o conjunto das NEs divergentes."""
    criar_tabelas(con)
    pares: dict[str, set] = {}
    for caminho in sorted(tg_dir.glob("*.csv")):
        h, dados = _ler_csv(caminho)
        for c_ne, c_pi, c_pr in COLUNAS_NE_PI_PROCESSO:
            if c_ne in h and c_pi in h and c_pr in h:
                a, b, c = h.index(c_ne), h.index(c_pi), h.index(c_pr)
                for r in dados:
                    if max(a, b, c) < len(r) and r[b].strip() not in ("", "-8", "-9"):
                        pares.setdefault(r[a].strip(), set()).add((r[b].strip(), r[c].strip()))
                break
    vinc: dict[str, set] = {}
    for u in linhas:
        ne = ne_do_documento(u[2])
        if ne:
            vinc.setdefault(ne, set()).add(u[0])
    regs = []
    for ne, s in pares.items():
        for pi, campo in s:
            p_pi, p_ne = dono_do_pi(pi), processo_cadastrado(campo)
            if p_pi and p_ne and p_pi != p_ne:
                regs.append((ne, pi, p_pi, p_ne, campo, ", ".join(sorted(vinc.get(ne, ()))), agora))
    con.execute("DELETE FROM divergencias_pi")
    con.executemany("INSERT OR REPLACE INTO divergencias_pi VALUES (?,?,?,?,?,?,?)", regs)
    return {r[0] for r in regs}


def divergencias_do_processo(con: sqlite3.Connection, processo: str) -> list[dict]:
    criar_tabelas(con)
    cols = ("ne", "pi", "processo_do_pi", "processo_da_ne", "processo_campo", "vinculada_a")
    return [dict(zip(cols, r)) for r in con.execute(
        "SELECT ne, pi, processo_do_pi, processo_da_ne, processo_campo, vinculada_a FROM divergencias_pi "
        "WHERE processo_do_pi=? OR processo_da_ne=? OR (',' || REPLACE(vinculada_a, ' ', '') || ',') LIKE ? ORDER BY ne",
        (processo, processo, f"%,{processo},%"))]


def pi_da_linha(etapa: str, documento: str, m: dict) -> str | None:
    doc = str(documento or "")
    if etapa not in ETAPAS_PI_EXPLICITO:                  # OB, PF, TRF: sem PI explícito
        return None
    if etapa in ETAPAS_PI_NO_DOCUMENTO:
        pi = doc.split(" [")[0].strip() or None
        # planilha de programação: quando a linha não tem PI, a coluna traz o NOME DO TERMO ("TC nº 969243/2024") — não é PI (02/10/2026)
        if pi and RE_NOME_TERMO.match(pi):
            return None
        return pi
    if etapa == "CREDITO":
        nc = RE_NC_COMPLETA.search(doc)
        return m["nc_pi"].get(nc.group(0)) if nc else None
    ne = RE_NE_COMPLETA.search(doc)
    if ne:
        return m["ne_pi"].get(ne.group(0)) or m["ne_pi_ug"].get((ne.group(1), ne.group(2)))
    return None


import os
from datetime import datetime

def agora_iso() -> str:
    """Retorna a data e hora atual no formato ISO padrão."""
    return datetime.utcnow().isoformat() + "Z"

def garantir_pastas(caminho) -> str:
    """Garante que a pasta de destino exista no servidor."""
    import os
    if not os.path.exists(caminho):
        os.makedirs(caminho, exist_ok=True)
    return str(caminho)
