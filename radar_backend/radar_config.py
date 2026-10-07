import os
from pathlib import Path

# Define as pastas principais para o servidor Linux do Render encontrar os dados
BASE_DIR = Path(__file__).resolve().parent.parent
DADOS_DIR = BASE_DIR / "dados"
