"""Пути и настройки. Тяжёлые данные лежат вне OneDrive (DATA_DIR)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TH_DATA_DIR", r"C:\tenderhack\data"))

RAW_DIR = DATA_DIR / "raw"
RAW_NOTICES = RAW_DIR / "Извещения_24-25.csv"
RAW_SUPPLIERS = RAW_DIR / "Поставщики_24-25.csv"
RAW_TRU = RAW_DIR / "ТРУ_24-25.csv"

DB_PATH = DATA_DIR / "tender.duckdb"
INDEX_DIR = DATA_DIR / "index"
ENRICH_DB = DATA_DIR / "enrich.sqlite"

# словари от команды NLP (маленькие, лежат в репозитории)
NLP_DICT_DIR = ROOT / "data" / "nlp"

SPB_REGION = "78"
LO_REGION = "47"
