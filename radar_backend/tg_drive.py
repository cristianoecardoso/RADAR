"""API-TG automática (usuário, 03/10/2026): baixa da pasta do Google Drive "03.001.1.Radar" (onde o Power Automate grava as
subscrições do Tesouro Gerencial) só os arquivos NOVOS ou ALTERADOS para dados/tg/ e, se algo mudou, reconstrói tg_execucao_join.

  GET  /api/tg/drive/status        estado: credencial, autorização, última sincronização, andamento
  POST /api/tg/drive/sync          sincroniza agora (botão API-TG); sem token, abre o login do Google NESTE Mac (uma vez)
  POST /api/tg/drive/credenciais   recebe o JSON do "ID do cliente OAuth" (App para computador) do Google Cloud

Roda também ao iniciar o RADAR (app.py), sem login interativo: sem token, só registra no log e espera o botão.
Credencial e token ficam em ~/.radar_fnac (fora da Mesa/iCloud). Escopo: drive.readonly (só leitura).
Versão anterior dos arquivos substituídos: ~/RADAR_backups/tg_drive_<data>/ (máx. 3 pastas).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from radar_backend.radar_config import SCRIPTS_DIR, TG_DIR
from radar_backend.radar_util import escrever_log, resposta_erro

bp = Blueprint("tg_drive", __name__)

PASTA_ID_PADRAO = "1SX6fLRByOnCEE-i_LL-Jg8a4t-n4nnPR"          # "SAC - Bases de Dados/03. Dados Brutos/03.001.Orcamento/03.001.1.Radar"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CFG_DIR = Path.home() / ".radar_fnac"
CRED = CFG_DIR / "drive_credentials.json"
TOKEN = CFG_DIR / "drive_token.json"
CFG = CFG_DIR / "drive_config.json"                               # {"folder_id": "..."} para trocar a pasta
ULTIMA = CFG_DIR / "drive_ultima_sync.json"                     # {"data": "AAAA-MM-DD", "quando": ISO} da última sincronização OK
MANIFESTO = TG_DIR / ".drive_sync.json"                           # {file_id: {name, modifiedTime, md5}}
BACKUPS = Path.home() / "RADAR_backups"
EXPORTA = {"application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")}

ESTADO: dict = {"url_auth": None, "rodando": False, "etapa": None, "inicio": None, "fim": None, "ok": None, "mensagem": None,
                "baixados": [], "ignorados": [], "join": None, "precisa": None}
_trava = threading.Lock()


class PrecisaAcao(Exception):
    """Falta credencial (precisa='credenciais') ou autorização (precisa='login')."""
    def __init__(self, precisa: str, msg: str):
        super().__init__(msg)
        self.precisa = precisa


def _pasta_id() -> str:
    try:
        return json.loads(CFG.read_text()).get("folder_id") or PASTA_ID_PADRAO
    except Exception:  # noqa: BLE001
        return PASTA_ID_PADRAO


def _servico(interativo: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not CRED.exists():
        raise PrecisaAcao("credenciais", "Falta o arquivo de credencial do Google (ID do cliente OAuth — App para computador).")
    cred = None
    if TOKEN.exists():
        cred = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if cred and cred.expired and cred.refresh_token:
        try:
            cred.refresh(Request())
        except Exception:  # noqa: BLE001
            cred = None
    if not cred or not cred.valid:
        if not interativo:
            raise PrecisaAcao("login", "Falta autorizar o acesso ao Google Drive (clique em API-TG).")
        import contextlib
        from google_auth_oauthlib.flow import InstalledAppFlow
        ESTADO["etapa"] = "aguardando a autorização no navegador (entre com a conta que acessa a pasta 03.001.1.Radar)"
        flow = InstalledAppFlow.from_client_secrets_file(str(CRED), SCOPES)

        class _Captura(io.StringIO):                     # pega o link de autorização impresso pelo flow -> mostrado na tela (status.url_auth)
            def write(self, t):
                m = re.search(r"https://accounts\.google\.com/\S+", t or "")
                if m:
                    ESTADO["url_auth"] = m.group(0)
                return super().write(t)
        with contextlib.redirect_stdout(_Captura()):
            cred = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300, prompt="consent",
                                         authorization_prompt_message="{url}", success_message="RADAR FNAC autorizado. Pode fechar esta aba.")
        ESTADO["url_auth"] = None
    CFG_DIR.mkdir(exist_ok=True)
    TOKEN.write_text(cred.to_json())
    try:
        TOKEN.chmod(0o600)
    except OSError:
        pass
    return build("drive", "v3", credentials=cred, cache_discovery=False)


def _listar(svc, pasta: str) -> list[dict]:
    itens, token = [], None
    while True:
        r = svc.files().list(q=f"'{pasta}' in parents and trashed=false", pageSize=1000, pageToken=token,
                             fields="nextPageToken, files(id,name,mimeType,modifiedTime,md5Checksum,size)",
                             supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        itens += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return itens


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _baixar(svc, f: dict, destino: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload
    req = (svc.files().export_media(fileId=f["id"], mimeType=EXPORTA[f["mimeType"]][0]) if f["mimeType"] in EXPORTA
           else svc.files().get_media(fileId=f["id"], supportsAllDrives=True))
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req, chunksize=8 << 20)
    feito = False
    while not feito:
        _, feito = dl.next_chunk()
    tmp = destino.with_name(destino.name + ".baixando")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(destino)


def _guardar_anterior(arquivos: list[Path]) -> None:
    """Cópia da versão anterior dos arquivos que vão ser substituídos (fora do iCloud; máx. 3 pastas tg_drive_*)."""
    existentes = [p for p in arquivos if p.exists()]
    if not existentes:
        return
    pasta = BACKUPS / f"tg_drive_{datetime.now():%Y%m%d_%H%M%S}"
    pasta.mkdir(parents=True, exist_ok=True)
    for p in existentes:
        shutil.copy2(p, pasta / p.name)
    for velha in sorted(BACKUPS.glob("tg_drive_*"), key=lambda x: x.stat().st_mtime)[:-3]:
        shutil.rmtree(velha, ignore_errors=True)


def _rodar_join() -> dict:
    from radar_backend.arquivos_db import base_em_atualizacao
    for _ in range(120):                                     # rebuild em andamento: espera (até ~10 min)
        if not base_em_atualizacao():
            break
        ESTADO["etapa"] = "aguardando o fim da atualização da base (rebuild)"
        time.sleep(5)
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import importlib
    import criar_tg_execucao_join as _mod
    importlib.reload(_mod)
    r = _mod.executar()
    return {"linhas": r.get("linhas_apos_dedupe"), "cobertura": r.get("cobertura"), "total": r.get("total_instrumentos"),
            "avisos": len(r.get("avisos") or [])}


def sincronizar(interativo: bool, forcar_join: bool = False) -> dict:
    """Baixa o que mudou na pasta do Drive e, se algo mudou (ou forcar_join), reconstrói tg_execucao_join."""
    ESTADO.update(rodando=True, inicio=datetime.now().isoformat(timespec="seconds"), fim=None, ok=None, mensagem=None,
                  baixados=[], ignorados=[], join=None, batimento=None, precisa=None, etapa="conectando ao Google Drive")
    try:
        svc = _servico(interativo)
        ESTADO["etapa"] = "listando a pasta 03.001.1.Radar"
        itens = _listar(svc, _pasta_id())
        try:
            manif = json.loads(MANIFESTO.read_text())
        except Exception:  # noqa: BLE001
            manif = {}
        TG_DIR.mkdir(parents=True, exist_ok=True)
        alvo = []
        for f in itens:
            mt = f["mimeType"]
            if mt == "application/vnd.google-apps.folder" or (mt.startswith("application/vnd.google-apps.") and mt not in EXPORTA):
                ESTADO["ignorados"].append(f"{f['name']} ({'subpasta' if mt.endswith('folder') else 'documento Google'})")
                continue
            nome = f["name"] + (EXPORTA[mt][1] if mt in EXPORTA and not f["name"].lower().endswith(EXPORTA[mt][1]) else "")
            local = TG_DIR / nome
            ant = manif.get(f["id"]) or {}
            if local.exists() and ant.get("modifiedTime") == f.get("modifiedTime"):
                continue                                     # mesma versão já baixada
            if local.exists() and f.get("md5Checksum") and _md5(local) == f["md5Checksum"]:
                manif[f["id"]] = {"name": nome, "modifiedTime": f.get("modifiedTime"), "md5": f.get("md5Checksum")}
                continue                                     # conteúdo igual (ex.: baixado à mão antes): só registra
            alvo.append((f, local, nome))
        _guardar_anterior([l for _, l, _ in alvo])
        for k, (f, local, nome) in enumerate(alvo, 1):
            ESTADO["etapa"] = f"baixando {k}/{len(alvo)}: {nome}"
            _baixar(svc, f, local)
            manif[f["id"]] = {"name": nome, "modifiedTime": f.get("modifiedTime"), "md5": f.get("md5Checksum")}
            ESTADO["baixados"].append(nome)
        MANIFESTO.write_text(json.dumps(manif, ensure_ascii=False, indent=1))
        if alvo or forcar_join:
            ESTADO["etapa"] = "reconstruindo tg_execucao_join"
            ESTADO["join"] = _rodar_join()
            # BATIMENTO automático (usuário, 03/10/2026): entradas novas x Instrumentos.db; PI de programação aprendido -> join de novo
            ESTADO["etapa"] = "batimento: conferindo as entradas novas com os instrumentos"
            try:
                from radar_backend.batimento import rodar as _bat
                ESTADO["batimento"] = _bat(_rodar_join)
            except Exception as exc:  # noqa: BLE001
                ESTADO["batimento"] = {"erro": str(exc)}
                escrever_log(f"ERRO | BATIMENTO: {exc}")
        n = len(alvo)
        ESTADO["mensagem"] = (f"{n} arquivo(s) novo(s)/alterado(s) baixado(s) do Drive" if n else "Nenhum arquivo novo no Drive") + \
                             (f"; base do TG reconstruída ({ESTADO['join']['linhas']} linhas)" if ESTADO["join"] else "") + \
                             (lambda b: (f"; batimento: {b.get('novos', 0)} entrada(s) nova(s) — {b.get('vinculados', 0) + b.get('auto', 0)} vinculada(s), "
                                         f"{b.get('pendentes', 0)} pendente(s)" if "novos" in b and "baseline" not in b else
                                         (f"; batimento: linha de base criada ({b['baseline']} documentos)" if "baseline" in b else "")))(ESTADO.get("batimento") or {}) + "."
        ESTADO["ok"] = True
        ULTIMA.write_text(json.dumps({"data": datetime.now().date().isoformat(), "quando": datetime.now().isoformat(timespec="seconds"),
                                      "baixados": len(alvo)}))
        escrever_log(f"TG-DRIVE | {ESTADO['mensagem']} {', '.join(ESTADO['baixados'][:20])}")
    except PrecisaAcao as exc:
        ESTADO.update(ok=False, precisa=exc.precisa, mensagem=str(exc), url_auth=None)
        escrever_log(f"TG-DRIVE | {exc}")
    except Exception as exc:  # noqa: BLE001
        ESTADO.update(ok=False, mensagem=f"Falha: {exc}", url_auth=None)
        escrever_log(f"ERRO | TG-DRIVE: {exc}")
    finally:
        ESTADO.update(rodando=False, etapa=None, fim=datetime.now().isoformat(timespec="seconds"))
    return dict(ESTADO)


def iniciar(interativo: bool, forcar_join: bool = False) -> bool:
    with _trava:
        if ESTADO["rodando"]:
            return False
        ESTADO["rodando"] = True
    threading.Thread(target=sincronizar, args=(interativo, forcar_join), daemon=True).start()
    return True


def ultima_sync() -> dict:
    try:
        return json.loads(ULTIMA.read_text())
    except Exception:  # noqa: BLE001
        return {}


def ao_iniciar_radar() -> None:
    """Chamado pelo app.py: atualização DIÁRIA (usuário, 03/10/2026) — ao abrir, só sincroniza se ainda não houve
    sincronização bem-sucedida HOJE; o botão API-TG sincroniza sempre. Sem token: só registra e espera o botão."""
    hoje = datetime.now().date().isoformat()
    if ultima_sync().get("data") == hoje:
        ESTADO["mensagem"] = f"Já atualizado hoje ({ultima_sync().get('quando', '')[11:16]}); clique em API-TG para atualizar de novo."
        escrever_log("TG-DRIVE | ao abrir: já sincronizado hoje — não baixa de novo")
        return
    if CRED.exists() and TOKEN.exists():
        iniciar(interativo=False)
    else:
        ESTADO.update(precisa="credenciais" if not CRED.exists() else "login",
                      mensagem="API-TG automática ainda não configurada: clique em API-TG.")


@bp.get("/api/tg/drive/status")
def api_status():
    return jsonify({"sucesso": True, **ESTADO, "credenciais": CRED.exists(), "autorizado": TOKEN.exists(), "pasta_id": _pasta_id(),
                    "ultima_sync": ultima_sync()})


@bp.post("/api/tg/drive/sync")
def api_sync():
    d = request.get_json(silent=True) or {}
    iniciou = iniciar(interativo=True, forcar_join=bool(d.get("forcar_join", True)))
    return jsonify({"sucesso": True, "iniciado": iniciou, **ESTADO})


@bp.post("/api/tg/drive/credenciais")
def api_credenciais():
    try:
        f = request.files.get("arquivo")
        if not f:
            return jsonify({"sucesso": False, "erro": "Envie o arquivo JSON da credencial."}), 400
        j = json.loads(f.read().decode("utf-8"))
        cli = j.get("installed") or j.get("web")
        if not cli or not cli.get("client_id") or not cli.get("client_secret"):
            return jsonify({"sucesso": False, "erro": "Não é um JSON de 'ID do cliente OAuth' do Google (falta installed/client_id)."}), 400
        if "installed" not in j:
            return jsonify({"sucesso": False, "erro": "Use um cliente do tipo 'App para computador' (Desktop app), não 'Aplicativo da Web'."}), 400
        CFG_DIR.mkdir(exist_ok=True)
        CRED.write_text(json.dumps(j))
        try:
            CRED.chmod(0o600)
        except OSError:
            pass
        if TOKEN.exists():
            TOKEN.unlink()                                   # credencial nova: autoriza de novo
        escrever_log("TG-DRIVE | credencial OAuth gravada em ~/.radar_fnac")
        return jsonify({"sucesso": True})
    except Exception as exc:  # noqa: BLE001
        return resposta_erro(exc)
