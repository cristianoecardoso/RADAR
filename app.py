"""Entrypoint do RADAR FNAC.

Uso:
    python3 -m pip install -r requirements.txt
    python3 app.py
    abra http://127.0.0.1:5050/

Os dados ficam em RADAR_FNAC/dados/ (padrão).
Para usar outro local:  export RADAR_DADOS_DIR="/caminho/para/os/dados"
"""

from __future__ import annotations

import os
import threading
import webbrowser

from radar_backend import create_app
from radar_backend.radar_config import BASE_DIR, DADOS_DIR
from radar_backend.radar_util import escrever_log, localizar_html

app = create_app()


def abrir_navegador() -> None:
    try:
        webbrowser.open_new("http://127.0.0.1:5050/")
    except Exception:
        pass


if __name__ == "__main__":
    escrever_log("BACKEND INICIADO")

    print("=" * 68)
    print(" RADAR FNAC - BACKEND LOCAL")
    print("=" * 68)
    print(f" Código:   {BASE_DIR}")
    print(f" Dados:    {DADOS_DIR}")
    print(" Endereço: http://127.0.0.1:5050/")
    print(" Status:   http://127.0.0.1:5050/api/status")
    try:
        print(f" HTML:     {localizar_html().name}")
    except Exception:
        pass
    print()
    print(" Pressione Ctrl+C para encerrar.")
    print("=" * 68)

    # API-TG automática (03/10/2026): ao iniciar, baixa do Google Drive os arquivos novos do Tesouro Gerencial (sem pedir login)
    try:
        from radar_backend.tg_drive import ao_iniciar_radar
        threading.Timer(3.0, ao_iniciar_radar).start()
        from radar_backend.apigov import gov_diario            # API-GOV automático: uma vez ao dia, ao abrir (03/10/2026)
        threading.Timer(8.0, gov_diario).start()
    except Exception as exc:  # noqa: BLE001
        escrever_log(f"ERRO | TG-DRIVE ao iniciar: {exc}")

    # Pode ser ativado definindo RADAR_ABRIR_NAVEGADOR=1
    if os.environ.get("RADAR_ABRIR_NAVEGADOR", "0") == "1":
        threading.Timer(1.0, abrir_navegador).start()

    # host=127.0.0.1 mantém o backend acessível apenas no computador local.
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
