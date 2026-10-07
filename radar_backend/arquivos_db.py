"""Arquivos dos documentos SEI e fotos, fora do Instrumentos.db (pedido do usuário, 01/10/2026).

  dados/INSTRUMENTOS/SEI.db
    arquivos_pdf        arquivo original (PDF/Excel) de cada documento SEI — mesma tabela que antes ficava no Instrumentos.db.
                        Todas as referências (documentos, paginas, cache_pdf, documentos_vinculados...) continuam no Instrumentos.db;
                        quem lê o arquivo anexa este banco (anexar_sei: ATTACH ... AS sei) e a consulta "FROM arquivos_pdf" é a mesma.
    documentos_usuario  documentos adicionados pela tela SEI (botão "Adicionar Documento"): instrumento, nome original, hash
    documentos_nomes    nome dado pelo usuário a um documento (o rótulo da lista "Documentos (PDF)" passa a ser esse nome)
  dados/INSTRUMENTOS/fotos.db
    fotos               imagens (fotos) encontradas nos documentos adicionados, por instrumento

  POST /api/instrumentos/documento/adicionar   multipart: processo, nome, arquivo (PDF)
  POST /api/instrumentos/documento/renomear    {documento, nome}
  GET  /api/instrumentos/fotos?processo=        lista das fotos do instrumento
  GET  /api/instrumentos/foto?id=               a imagem
Ao adicionar: o PDF vai para SEI.db, o vínculo ao instrumento para Instrumentos.db (documentos_vinculados, papel documento_usuario,
preservado no rebuild), as fotos para fotos.db e o texto (cache_pdf) é extraído em segundo plano — o próximo
scripts/criar_base_completa.py lê o documento como qualquer outro.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import threading
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from radar_backend.radar_config import BASE_DIR, INSTRUMENTOS_DB
from radar_backend.radar_util import escrever_log, resposta_erro

bp = Blueprint("arquivos_db", __name__)

SEI_DB = INSTRUMENTOS_DB.with_name("SEI.db")
FOTOS_DB = INSTRUMENTOS_DB.with_name("fotos.db")
PAPEL_USUARIO = "documento_usuario"

DDL_SEI = """
CREATE TABLE IF NOT EXISTS arquivos_pdf (documento_sei TEXT PRIMARY KEY, nome TEXT, bytes_originais INTEGER, compressao TEXT, conteudo BLOB);
CREATE TABLE IF NOT EXISTS documentos_usuario (documento_sei TEXT NOT NULL, processo_sei TEXT NOT NULL, nome_original TEXT, sha256 TEXT,
  bytes INTEGER, paginas INTEGER, fotos INTEGER, adicionado_em TEXT, PRIMARY KEY (documento_sei, processo_sei));
CREATE INDEX IF NOT EXISTS ix_documentos_usuario_proc ON documentos_usuario(processo_sei);
CREATE TABLE IF NOT EXISTS documentos_nomes (documento_sei TEXT PRIMARY KEY, nome TEXT NOT NULL, alterado_em TEXT);
"""
DDL_FOTOS = """
CREATE TABLE IF NOT EXISTS fotos (id INTEGER PRIMARY KEY AUTOINCREMENT, processo_sei TEXT NOT NULL, documento_sei TEXT NOT NULL, pagina INTEGER,
  indice INTEGER, largura INTEGER, altura INTEGER, formato TEXT, sha256 TEXT NOT NULL, bytes INTEGER, conteudo BLOB NOT NULL, criado_em TEXT,
  UNIQUE (processo_sei, sha256));
CREATE INDEX IF NOT EXISTS ix_fotos_proc ON fotos(processo_sei);
"""


def garantir_esquemas() -> None:
    for caminho, ddl in ((SEI_DB, DDL_SEI), (FOTOS_DB, DDL_FOTOS)):
        con = sqlite3.connect(caminho, timeout=60)
        con.executescript(ddl)
        if caminho == SEI_DB:                                       # colunas acrescentadas depois (análise do Adicionar SEI)
            cols = {r[1] for r in con.execute("PRAGMA table_info(documentos_usuario)")}
            for c in ("tipo_detectado", "data_documento", "processo_documento"):   # processo SEI do documento (principal ou relacionado), 02/10/2026
                if c not in cols:
                    con.execute(f"ALTER TABLE documentos_usuario ADD COLUMN {c} TEXT")
        con.close()


def anexar_sei(con: sqlite3.Connection) -> sqlite3.Connection:
    """Anexa o SEI.db à conexão do Instrumentos.db: "FROM arquivos_pdf" passa a ler sei.arquivos_pdf."""
    if not any(r[1] == "sei" for r in con.execute("PRAGMA database_list")):
        con.execute("ATTACH DATABASE ? AS sei", (str(SEI_DB),))
    return con


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cb():
    """Funções de leitura de PDF do construtor (OCR, limpeza de página, tabelas) — carregadas só quando necessárias."""
    spec = importlib.util.spec_from_file_location("criar_base_sei", BASE_DIR / "scripts" / "criar_base_sei.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def cache_documento(doc: str, dados: bytes) -> dict:
    """Mesmo conteúdo que scripts/extrair_pdfs_cache.py grava em cache_pdf (texto/OCR por página + tabelas)."""
    import fitz
    cb = _cb()
    sha = hashlib.sha256(dados).hexdigest()
    d = fitz.open(stream=dados, filetype="pdf")
    paginas, tabs, n_ocr = [], [], 0
    for i, pg in enumerate(d, 1):
        t, metodo = pg.get_text().strip(), "texto"
        if len(t) < 30:
            o = cb.ocr_pagina(pg)
            if o:
                t, metodo, n_ocr = o, "ocr", n_ocr + 1
        paginas.append([i, metodo, cb.limpar_pagina(t, doc)])
        try:
            for ti, tb in enumerate(pg.find_tables().tables, 1):
                for li, cel in enumerate(cb.tabela_limpa(tb.extract()), 1):
                    tabs.append([i, ti, li, len(cel), cel])
        except Exception:  # noqa: BLE001
            pass
    return dict(sha256=sha, paginas=paginas, tabs=tabs, n_ocr=n_ocr)


def _gravar_cache(doc: str, dados: bytes, processo: str | None = None) -> None:
    try:
        c = cache_documento(doc, dados)
        con = sqlite3.connect(INSTRUMENTOS_DB, timeout=120)
        con.execute("INSERT OR REPLACE INTO cache_pdf VALUES (?,?,?)", (doc, c["sha256"], json.dumps(c, ensure_ascii=False)))
        con.commit()
        if processo:                                               # campos VAZIOS do instrumento pré-preenchidos com o documento (01/10/2026)
            con.row_factory = sqlite3.Row
            anexar_sei(con)
            feitos = _prefill(con, processo, doc, analisar_documento(c))
            con.commit()
            if feitos:
                escrever_log(f"SEI.db | {doc}: campos vazios preenchidos — " + "; ".join(feitos))
        con.close()
        escrever_log(f"SEI.db | cache_pdf gravado para {doc} ({len(c['paginas'])} pág., OCR {c['n_ocr']})")
    except Exception as exc:  # noqa: BLE001
        escrever_log(f"ERRO | cache_pdf de {doc}: {exc}")


# ---------------------------------------------------------------- fotos
# Foto = imagem do documento que não é logotipo/brasão (pequena ou repetida em várias páginas), faixa decorativa (muito alongada)
# nem página escaneada inteira (cobre quase toda a página).
MIN_LADO, MIN_AREA_PAG, MAX_AREA_PAG, MAX_PROPORCAO = 300, 0.03, 0.90, 5.0


def extrair_fotos(dados: bytes) -> list[dict]:
    import fitz
    d = fitz.open(stream=dados, filetype="pdf")
    infos, paginas_por_xref = [], {}
    for pno, pg in enumerate(d, 1):
        area_pag = abs(pg.rect) or 1
        for info in pg.get_image_info(xrefs=True):
            xref = info.get("xref") or 0
            if not xref:
                continue
            paginas_por_xref.setdefault(xref, set()).add(pno)
            infos.append((pno, xref, info, abs(fitz.Rect(info["bbox"])) / area_pag))
    fotos, vistos = [], set()
    for pno, xref, info, frac in infos:
        w, h = info.get("width") or 0, info.get("height") or 0
        if min(w, h) < MIN_LADO or max(w, h) / max(min(w, h), 1) > MAX_PROPORCAO:
            continue
        if not (MIN_AREA_PAG <= frac < MAX_AREA_PAG):
            continue
        if len(d) > 2 and len(paginas_por_xref[xref]) >= max(3, len(d) // 2):      # logotipo/cabeçalho repetido
            continue
        try:
            img = d.extract_image(xref)
            conteudo, ext = img["image"], img["ext"].lower()
            if ext not in ("jpeg", "jpg", "png") or img.get("smask"):
                pix = fitz.Pixmap(d, xref)
                if pix.n - pix.alpha >= 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                conteudo, ext = pix.tobytes("png"), "png"
        except Exception:  # noqa: BLE001
            continue
        sha = hashlib.sha256(conteudo).hexdigest()
        if sha in vistos:
            continue
        vistos.add(sha)
        fotos.append(dict(pagina=pno, largura=w, altura=h, formato="jpeg" if ext == "jpg" else ext, sha256=sha, conteudo=conteudo))
    return fotos


# ---------------------------------------------------------------- documento adicionado
RE_SEI_NUM = re.compile(r"\bSEI\s*(?:n[º°o.]?\s*)?:?\s*(\d{7,9})\b", re.I)


def _numero_sei(dados: bytes, nome: str) -> str | None:
    """Nº do documento SEI: nome do arquivo baixado do SEI ("SEI_12345678...") ou, sem ele, rodapé "SEI nº 12345678" (o mais citado)."""
    m = re.search(r"SEI[_ -]?(?:n[º°o]?[_ ]?)?(\d{7,9})(?!\d)", nome, re.I) or re.match(r"(\d{7,9})(?!\d)", nome)
    if m:
        return m.group(1)
    import fitz
    try:
        texto = "\n".join(pg.get_text() for pg in fitz.open(stream=dados, filetype="pdf"))
    except Exception:  # noqa: BLE001
        texto = ""
    achados = RE_SEI_NUM.findall(texto)
    return max(set(achados), key=achados.count) if achados else None


@bp.post("/api/instrumentos/documento/adicionar")
def api_documento_adicionar():
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar para adicionar documentos."}), 409
    try:
        processo = (request.form.get("processo") or "").strip()
        arq = request.files.get("arquivo")
        if not processo or not arq:
            return jsonify({"sucesso": False, "erro": "Informe o instrumento e o arquivo."}), 400
        dados = arq.read()
        nome_original = arq.filename or "documento.pdf"
        if not dados.startswith(b"%PDF"):
            return jsonify({"sucesso": False, "erro": "O arquivo não é um PDF."}), 400
        nome = (request.form.get("nome") or "").strip() or re.sub(r"\.pdf$", "", nome_original, flags=re.I)
        sha = hashlib.sha256(dados).hexdigest()
        # REGRA (usuário, 01/10/2026): o documento é gravado SEMPRE pelo nº do documento SEI; se o nº já existe na base, é
        # "documento já existente" e nada é gravado (não lê o conteúdo para comparar). O nome visual pode repetir.
        doc = re.sub(r"\D", "", request.form.get("numero") or "") or _numero_sei(dados, nome_original)
        if not doc:
            return jsonify({"sucesso": False, "pedir_numero": True,
                            "erro": "Não achei o nº do documento SEI no nome do arquivo nem no rodapé. Informe o nº do documento SEI."}), 400
        garantir_esquemas()
        con = anexar_sei(sqlite3.connect(INSTRUMENTOS_DB, timeout=60))
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM instrumentos WHERE processo_sei=?", (processo,)).fetchone():
            con.close()
            return jsonify({"sucesso": False, "erro": f"Instrumento {processo} não encontrado."}), 404
        # PROCESSO DO DOCUMENTO (usuário, 02/10/2026): o principal do instrumento ou um processo RELACIONADO a ele
        pdoc = (request.form.get("processo_documento") or "").strip() or None
        if pdoc:
            from radar_backend.instrumentos_edicao import processos_do_instrumento
            if pdoc not in [x["processo"] for x in processos_do_instrumento(con, processo)]:
                con.close()
                return jsonify({"sucesso": False, "erro": f"O processo {pdoc} não é o principal nem um relacionado deste instrumento."}), 400
        if con.execute("SELECT 1 FROM sei.arquivos_pdf WHERE documento_sei=?", (doc,)).fetchone():
            con.close()
            return jsonify({"sucesso": False, "ja_existente": True, "documento_sei": doc,
                            "erro": f"documento já existente (SEI nº {doc}). Nada foi gravado."}), 409
        con.execute("INSERT INTO sei.arquivos_pdf VALUES (?,?,?,?,?)", (doc, f"{doc}.pdf", len(dados), "none", dados))
        import fitz
        n_pag = len(fitz.open(stream=dados, filetype="pdf"))
        fotos = extrair_fotos(dados)
        con.execute("INSERT OR REPLACE INTO sei.documentos_usuario (documento_sei, processo_sei, nome_original, sha256, bytes, paginas, fotos, "
                    "adicionado_em) VALUES (?,?,?,?,?,?,?,?)", (doc, processo, nome_original, sha, len(dados), n_pag, len(fotos), _agora()))
        con.execute("INSERT OR REPLACE INTO sei.documentos_nomes VALUES (?,?,?)", (doc, nome, _agora()))
        if pdoc:
            con.execute("UPDATE sei.documentos_usuario SET processo_documento=? WHERE documento_sei=? AND processo_sei=?", (pdoc, doc, processo))
        con.execute("INSERT OR IGNORE INTO documentos_vinculados VALUES (?,?,?,?,?)",
                    (processo, PAPEL_USUARIO, doc, f"adicionado pela tela SEI (arquivo {nome_original})", _agora()))
        con.commit()
        con.close()
        novas = 0
        if fotos:
            cf = sqlite3.connect(FOTOS_DB, timeout=60)
            for k, f in enumerate(fotos, 1):
                cur = cf.execute("INSERT OR IGNORE INTO fotos (processo_sei, documento_sei, pagina, indice, largura, altura, formato, sha256, bytes, "
                                 "conteudo, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (processo, doc, f["pagina"], k, f["largura"], f["altura"], f["formato"], f["sha256"], len(f["conteudo"]),
                                  f["conteudo"], _agora()))
                novas += cur.rowcount
            cf.commit()
            cf.close()
        if request.path.endswith("/documento/adicionar"):          # o Adicionar SEI lê o texto na hora (análise)
            threading.Thread(target=_gravar_cache, args=(doc, dados, processo), daemon=True).start()
        escrever_log(f"SEI.db | documento {doc} adicionado a {processo} ('{nome}', {n_pag} pág., {len(fotos)} foto(s), {novas} nova(s))")
        return jsonify({"sucesso": True, "documento_sei": doc, "nome": nome, "paginas": n_pag,
                        "fotos": len(fotos), "fotos_novas": novas})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.post("/api/instrumentos/documento/renomear")
def api_documento_renomear():
    try:
        p = request.get_json(silent=True) or {}
        doc, nome = str(p.get("documento") or "").strip(), str(p.get("nome") or "").strip()
        if not doc:
            return jsonify({"sucesso": False, "erro": "Informe o documento."}), 400
        garantir_esquemas()
        con = sqlite3.connect(SEI_DB, timeout=60)
        if nome:
            con.execute("INSERT OR REPLACE INTO documentos_nomes VALUES (?,?,?)", (doc, nome, _agora()))
        else:                                                          # nome vazio = volta ao rótulo automático
            con.execute("DELETE FROM documentos_nomes WHERE documento_sei=?", (doc,))
        con.commit()
        con.close()
        return jsonify({"sucesso": True, "documento_sei": doc, "nome": nome})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/fotos")
def api_fotos():
    processo = (request.args.get("processo") or "").strip()
    try:
        garantir_esquemas()
        con = sqlite3.connect(FOTOS_DB, timeout=60)
        con.row_factory = sqlite3.Row
        fotos = [dict(r) for r in con.execute("SELECT id, documento_sei, pagina, largura, altura, formato, bytes, criado_em FROM fotos "
                                              "WHERE processo_sei=? ORDER BY criado_em, documento_sei, pagina, indice", (processo,))]
        con.close()
        nomes = {}
        if fotos:
            cs = sqlite3.connect(SEI_DB, timeout=60)
            nomes = dict(cs.execute("SELECT documento_sei, nome FROM documentos_nomes"))
            cs.close()
        for f in fotos:
            f["documento_nome"] = nomes.get(f["documento_sei"]) or f["documento_sei"]
        return jsonify({"sucesso": True, "processo": processo, "fotos": fotos})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


@bp.get("/api/instrumentos/foto")
def api_foto():
    try:
        con = sqlite3.connect(FOTOS_DB, timeout=60)
        r = con.execute("SELECT formato, conteudo FROM fotos WHERE id=?", (int(request.args.get("id") or 0),)).fetchone()
        con.close()
        if not r:
            return jsonify({"sucesso": False, "erro": "Foto não encontrada."}), 404
        return Response(r[1], mimetype=f"image/{r[0] or 'png'}", headers={"Cache-Control": "max-age=86400"})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)


# ---------------------------------------------------------------- análise do documento adicionado pelo ✏️ do cartão (usuário, 01/10/2026)
# "Adicionar SEI" no popup de edição faz o mesmo que "Adicionar Documento" e ainda lê o documento com as MESMAS funções do rebuild
# (scripts/criar_base_completa.py: tipo_doc, assinaturas, datas de vigência, valores):
#   - Plano de Trabalho / Cronograma Físico-Financeiro mais recente que o atual -> vínculo papel "pt" (substitui o PT atual) e rebuild
#     em segundo plano para as telas Plano de Trabalho / Físico-Financeiro passarem a lê-lo;
#   - instrumento inicial ou prorrogação -> SUGESTÃO SEI para Assinatura e Vigência até; valores lidos -> SUGESTÃO para Valor atual e
#     Contrapartida. O usuário grava (vira edição do ✏️, reaplicada no rebuild) ou descarta.
_CBC = None
TIPOS_INICIAIS = ("Termo de Compromisso", "Termo de Execução Descentralizada", "Convênio", "Contrato")
RE_VALOR_PASSA = re.compile(r"(?:valor\s+(?:global|total)[^.;]{0,120}?)?passa(?:r[áa])?\s+a\s+ser\s+de\s+R\$\s*([\d.]+,\d{2})", re.I)
RE_CONTRAP = re.compile(r"contrapartida[^.;]{0,80}?R\$\s*([\d.]+,\d{2})", re.I)


def _cbc():
    """scripts/criar_base_completa.py como módulo (só as funções; main() não roda)."""
    global _CBC
    if _CBC is None:
        import sys
        scripts = str(BASE_DIR / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        spec = importlib.util.spec_from_file_location("criar_base_completa_funcoes", BASE_DIR / "scripts" / "criar_base_completa.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _CBC = m
    return _CBC


def _br_num(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def analisar_documento(cache: dict) -> dict:
    m = _cbc()
    cb = m.cb
    paginas = cache["paginas"]
    full = "\n\n".join(p[2] for p in paginas)
    tipo = m.tipo_doc(full)
    cab = re.sub(r"\s+", " ", full[:3000]).upper()
    classe = tipo
    if tipo == "Plano de Trabalho" and "CRONOGRAMA" in cab[:400] and "PLANO DE TRABALHO" not in cab[:400]:
        classe = "Cronograma Físico-Financeiro"
    ass = cb.assinaturas(full)
    dd = cb.data_documento(full) or m.data_doc_alt(full)
    ck = m.datas_chave(full)
    cvig = m.clausula_vigencia(full)
    datas = m.datas_do_documento(paginas, ass, dd)
    ref = min([x for x in (dd, min((a[2] for a in ass), default=None)) if x], default=None)
    vig = sorted(d for _, d, r, _ in datas if r == "vigencia" and (ref is None or d >= ref))
    vig_fim = vig[-1] if vig else (ck.get("fim_vigencia") or cvig.get("fim"))
    data_ass = max((a[2] for a in ass), default=None) or dd or ck.get("assinatura")
    vt, vr, cp, _orig = cb.valores(full)
    if vt is None and tipo not in ("Termo Aditivo", "Apostila", "Nota Técnica", "Plano de Trabalho"):
        vt, _ = m.valor_principal(m.valores_rotulados(paginas))
    if vt is None and tipo in ("Termo Aditivo", "Apostila"):
        mv = RE_VALOR_PASSA.search(full)
        vt = _br_num(mv.group(1)) if mv else None
    if cp is None:
        mc = RE_CONTRAP.search(full)
        cp = _br_num(mc.group(1)) if mc else None
    prorrogacao = tipo in ("Termo Aditivo", "Apostila") and bool(re.search(r"prorrog", full, re.I))
    # Valor atual = valor TOTAL do instrumento, contrapartida incluída (decisão do usuário, 01/10/2026)
    valor_atual = vt if vt is not None else (round(vr + (cp or 0), 2) if vr is not None else None)
    obj = cb.objeto(full) or (m.objeto_generico(full) if tipo not in ("Termo Aditivo", "Apostila", "Nota Técnica") else None)
    ni, _ti = m.numero_instrumento(full)
    return {"tipo_documento": tipo, "classe": classe, "data_documento": data_ass or dd, "data_assinatura": data_ass,
            "objeto": cb.formatar_objeto(obj) if obj else None, "numero_instrumento": ni,
            "cnpjs": list(dict.fromkeys(re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", full))),
            "vigencia_fim": vig_fim, "inicial": tipo in TIPOS_INICIAIS, "prorrogacao": prorrogacao,
            "plano_trabalho": tipo == "Plano de Trabalho", "valor_atual": valor_atual, "contrapartida": cp, "valor_total_lido": vt,
            "numero_documento": m.numero_documento(full)}


def _pt_atual(con: sqlite3.Connection, processo: str, doc_novo: str) -> tuple[str | None, str | None]:
    """(documento, data) do Plano de Trabalho mais recente já existente no processo."""
    melhor = (None, None)
    try:
        for d, data in con.execute("SELECT documento_sei, data_versao FROM pt_cronograma_docs WHERE processo_sei=? AND data_versao IS NOT NULL", (processo,)):
            if d != doc_novo and (melhor[1] is None or data > melhor[1]):
                melhor = (d, data)
    except sqlite3.Error:
        pass
    for d, data in con.execute("SELECT documento_sei, COALESCE(data_assinatura, data_documento) FROM documentos WHERE tipo_documento='Plano de Trabalho' AND "
                               "(processo_sei=? OR documento_sei IN (SELECT documento_sei FROM documentos_vinculados WHERE processo_sei=? AND papel IN ('pt','pti')))",
                               (processo, processo)):
        if d != doc_novo and data and (melhor[1] is None or data > melhor[1]):
            melhor = (d, data)
    return melhor


def _prefill(con: sqlite3.Connection, processo: str, doc: str, a: dict) -> list[str]:
    from radar_backend.aeroportos import preencher_vazios
    from radar_backend.apigov import executoras
    feitos = []
    vals = {"numero_instrumento": a.get("numero_instrumento"), "objeto": a.get("objeto") if a.get("inicial") else None,
            "valor_total": a.get("valor_total_lido"), "contrapartida": a.get("contrapartida") or None}
    if a.get("inicial") or a.get("prorrogacao"):
        vals["vigencia_fim"] = a.get("vigencia_fim")
    if a.get("inicial"):
        vals["data_assinatura"] = a.get("data_assinatura")
    feitos += preencher_vazios(con, processo, vals, doc)
    # Nº do Instrumento / Término da Vigência: campos do INSTRUMENTO (✏️ do cartão), preenchidos só se vazios
    from radar_backend.instrumentos_edicao import editar_campo_instrumento
    ins = con.execute("SELECT numero_instrumento, vigencia_mais_futura, data_assinatura_instrumento FROM instrumentos WHERE processo_sei=?",
                      (processo,)).fetchone()
    if ins is not None:
        if not ins[0] and a.get("numero_instrumento") and editar_campo_instrumento(con, processo, "numero_instrumento", a["numero_instrumento"]):
            feitos.append(f"Nº do Instrumento: {a['numero_instrumento']} (doc. {doc})")
        if not ins[1] and vals.get("vigencia_fim") and editar_campo_instrumento(con, processo, "vigencia_mais_futura", vals["vigencia_fim"]):
            feitos.append(f"Término da vigência: {vals['vigencia_fim']} (doc. {doc})")
        if not ins[2] and vals.get("data_assinatura") and editar_campo_instrumento(con, processo, "data_assinatura_instrumento", vals["data_assinatura"]):
            feitos.append(f"Assinatura: {vals['data_assinatura']} (doc. {doc})")
    # CNPJ da executora vazio: o CNPJ do documento que não é de órgão federal comum (aparece em 5+ processos)
    con.execute("CREATE TABLE IF NOT EXISTS instrumentos_executora (processo_sei TEXT PRIMARY KEY, cnpj TEXT, nome TEXT, alterado_em TEXT)")
    if a.get("cnpjs") and not (executoras(con).get(processo) or {}).get("cnpj"):
        procs = {}
        for p_, t in con.execute("SELECT processo_sei, texto_completo FROM documentos WHERE texto_completo LIKE '%CNPJ%' AND processo_sei IS NOT NULL"):
            for c in set(re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", t or "")):
                procs.setdefault(c, set()).add(p_)
        cand = [c for c in a["cnpjs"] if len(procs.get(c, ())) < 5]
        if cand:
            nome = (con.execute("SELECT nome FROM instrumentos_executora WHERE processo_sei=?", (processo,)).fetchone() or [None])[0]
            if "fonte" not in {r[1] for r in con.execute("PRAGMA table_info(instrumentos_executora)")}:
                con.execute("ALTER TABLE instrumentos_executora ADD COLUMN fonte TEXT")
            con.execute("INSERT OR REPLACE INTO instrumentos_executora (processo_sei, cnpj, nome, alterado_em, fonte) VALUES (?,?,?,?,?)",
                        (processo, re.sub(r"\D", "", cand[0]), nome, _agora(), f"documento SEI nº {doc} (pré-preenchido)"))
            feitos.append(f"CNPJ da executora: {cand[0]} (doc. {doc})")
    return feitos


# ---------------------------------------------------------------- rebuild em segundo plano (PT substituído)
REBUILD = {"rodando": False, "inicio": None, "fim": None, "ok": None, "motivo": None, "log": []}
_trava_rebuild = threading.Lock()


def base_em_atualizacao() -> bool:
    return REBUILD["rodando"]


def _rodar_rebuild(motivo: str) -> None:
    import subprocess
    import sys
    from pathlib import Path
    REBUILD.update(rodando=True, inicio=_agora(), fim=None, ok=None, motivo=motivo, log=[])
    try:
        pasta = Path.home() / "RADAR_backups"                      # backup antes (máx. 3 cópias — usuário, 28/09/2026)
        pasta.mkdir(exist_ok=True)
        destino = pasta / f"Instrumentos_{datetime.now():%Y%m%d_%H%M%S}_antes_rebuild_pt.db"
        src = sqlite3.connect(INSTRUMENTOS_DB)
        dst = sqlite3.connect(destino)
        src.backup(dst)
        dst.close()
        src.close()
        for velho in sorted(pasta.glob("Instrumentos_*.db"), key=lambda p: p.stat().st_mtime)[:-3]:
            velho.unlink()
        for script in ("extrair_pdfs_cache.py", "criar_base_completa.py"):
            p = subprocess.run([sys.executable, str(BASE_DIR / "scripts" / script)], cwd=str(BASE_DIR), capture_output=True, text=True)
            linhas = [x for x in (p.stdout + p.stderr).splitlines() if x.strip() and "MuPDF" not in x]
            REBUILD["log"] = (REBUILD["log"] + [f"== {script} (código {p.returncode})"] + linhas[-12:])[-40:]
            if p.returncode != 0:
                raise RuntimeError(f"{script} terminou com erro: {linhas[-1] if linhas else p.returncode}")
        REBUILD["ok"] = True
        escrever_log(f"REBUILD | concluído ({motivo})")
    except Exception as exc:  # noqa: BLE001
        REBUILD["ok"] = False
        REBUILD["log"].append(f"ERRO: {exc}")
        escrever_log(f"ERRO | rebuild ({motivo}): {exc}")
    finally:
        REBUILD.update(rodando=False, fim=_agora())


def iniciar_rebuild(motivo: str) -> bool:
    with _trava_rebuild:
        if REBUILD["rodando"]:
            return False
        REBUILD["rodando"] = True
    threading.Thread(target=_rodar_rebuild, args=(motivo,), daemon=True).start()
    return True


@bp.get("/api/base/rebuild")
def api_rebuild_status():
    return jsonify({"sucesso": True, **REBUILD})


@bp.post("/api/instrumentos/sei/adicionar")
def api_sei_adicionar():
    """Adicionar SEI (popup ✏️ do cartão): grava como o Adicionar Documento e devolve a análise + sugestões."""
    if base_em_atualizacao():
        return jsonify({"sucesso": False, "erro": "A base está sendo atualizada (rebuild). Aguarde terminar para adicionar outro documento."}), 409
    resp = api_documento_adicionar()
    r = resp[0] if isinstance(resp, tuple) else resp
    dados_resp = r.get_json() or {}
    if not dados_resp.get("sucesso"):
        return resp
    try:
        processo = (request.form.get("processo") or "").strip()
        doc = dados_resp["documento_sei"]
        con = anexar_sei(sqlite3.connect(INSTRUMENTOS_DB, timeout=60))
        con.row_factory = sqlite3.Row
        linha = con.execute("SELECT conteudo FROM cache_pdf WHERE documento_sei=?", (doc,)).fetchone()
        if linha:
            cache = json.loads(linha[0])
        else:                                                      # precisa do texto agora (a thread do Adicionar Documento também grava)
            blob = con.execute("SELECT compressao, conteudo FROM sei.arquivos_pdf WHERE documento_sei=?", (doc,)).fetchone()
            import zlib
            cache = cache_documento(doc, zlib.decompress(blob[1]) if blob[0] == "zlib" else blob[1])
            con.execute("INSERT OR REPLACE INTO cache_pdf VALUES (?,?,?)", (doc, cache["sha256"], json.dumps(cache, ensure_ascii=False)))
        a = analisar_documento(cache)
        con.execute("UPDATE sei.documentos_usuario SET tipo_detectado=?, data_documento=? WHERE documento_sei=? AND processo_sei=?",
                    (a["classe"], a["data_documento"], doc, processo))
        atual = dict(con.execute("SELECT data_assinatura_instrumento, vigencia_mais_futura, valor_atual, contrapartida FROM instrumentos "
                                 "WHERE processo_sei=?", (processo,)).fetchone() or {})
        sug = {}
        if a["inicial"] or a["prorrogacao"]:
            if a["data_assinatura"] and a["data_assinatura"] != atual.get("data_assinatura_instrumento"):
                sug["data_assinatura_instrumento"] = a["data_assinatura"]
            if a["vigencia_fim"] and a["vigencia_fim"] != atual.get("vigencia_mais_futura"):
                sug["vigencia_mais_futura"] = a["vigencia_fim"]
        for campo in ("valor_atual", "contrapartida"):
            v = a[campo]
            if v is not None and (atual.get(campo) is None or abs(float(atual[campo]) - v) > 0.005):
                sug[campo] = v
        # campos VAZIOS do DETALHE do aeroporto e da executora são pré-preenchidos com o que o documento traz (usuário, 01/10/2026)
        preenchidos = _prefill(con, processo, doc, a)
        pt = None
        if a["plano_trabalho"] or a["tipo_documento"] in ("Termo Aditivo", "Apostila"):
            # PT novo (ou aditivo com PT): vira SUGESTÃO para o PLANO DE TRABALHO / METAS-ETAPAS do popup ✏️ — o usuário decide
            # ATUALIZAR, MANTER ou EDITAR (usuário, 01/10/2026). Leitura com o mesmo leitor do rebuild (pt_cronogramas).
            from radar_backend import plano_trabalho as PT
            blob = con.execute("SELECT compressao, conteudo FROM sei.arquivos_pdf WHERE documento_sei=?", (doc,)).fetchone()
            import zlib
            pdf = zlib.decompress(blob[1]) if blob[0] == "zlib" else blob[1]
            lido = PT.ler_pt_documento(doc, processo, a, cache, pdf)
            doc_ant, data_ant = _pt_atual(con, processo, doc)
            n = PT.registrar_sugestao(con, processo, doc, lido, a.get("vigencia_fim")) if (lido["cronograma"] or lido["metas_etapas"]) else {}
            pt = {"anterior": doc_ant, "data_anterior": data_ant, "data_novo": a["data_documento"], "sugestoes": n,
                  "mais_recente": data_ant is None or a["data_documento"] is None or a["data_documento"] > data_ant}
        con.commit()
        con.close()
        return jsonify({**dados_resp, "analise": a, "sugestoes": sug, "plano_trabalho": pt, "preenchidos": preenchidos})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
