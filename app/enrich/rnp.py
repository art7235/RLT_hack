"""РНП — реестр недобросовестных поставщиков (zakupki.gov.ru, ЕИС).

По ИНН возвращает записи реестра: действующие (поставщик сейчас в «чёрном списке») и исключённые (был раньше).
Кэш в SQLite (ENRICH_DB). Сайт отдаёт сертификат Минцифры, которому не доверяют стандартные
хранилища, поэтому verify=False (источник — официальный государственный портал).

Массовый прогрев: python -m app.enrich.rnp [limit]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import ENRICH_DB

URL = "https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
_local = threading.local()
_lock = threading.Lock()
_TAG = re.compile(r"<[^>]+>")


def _db() -> sqlite3.Connection:
    ENRICH_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ENRICH_DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS rnp (inn TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
    return con


def _client() -> httpx.Client:
    if getattr(_local, "client", None) is None:
        _local.client = httpx.Client(trust_env=False, verify=False, timeout=15, headers=HEADERS,
                                     follow_redirects=True)
    return _local.client


def _text(html: str) -> list[str]:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return [p.strip() for p in _TAG.sub("|", html).split("|") if p.strip()]


def parse(html: str, inn: str) -> dict:
    records = []
    for block in html.split("search-registry-entry-block")[1:]:
        parts = _text(block[:8000])
        if inn not in parts:
            continue  # поиск по строке мог зацепить чужую запись
        rec = {"law": next((x for x in parts[:5] if x.endswith("-ФЗ")), None), "number": None, "status": None,
               "name": None, "included": None, "excluded": None}
        for i, p in enumerate(parts):
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            if p.startswith("№") and rec["number"] is None:
                rec["number"] = p.lstrip("№ ").strip()
                rec["status"] = nxt
            elif p.startswith("Наименование"):
                rec["name"] = nxt
            elif p == "Включено":
                rec["included"] = nxt
            elif p == "Исключено" and nxt and re.match(r"\d{2}\.\d{2}\.\d{4}", nxt):
                rec["excluded"] = nxt
        rec["active"] = not rec["excluded"] and (rec["status"] or "").lower() != "исключено"
        records.append(rec)
    return {"in_rnp": any(r["active"] for r in records), "was_in_rnp": bool(records), "records": records[:5]}


def fetch_by_inn(inn: str) -> dict:
    r = _client().get(URL, params={"searchString": inn, "fz94": "on", "fz223": "on", "recordsPerPage": "_10"})
    r.raise_for_status()
    if "dishonestsupplier" not in r.text:
        raise RuntimeError("unexpected page")
    return parse(r.text, inn)


def get_many(inns: list[str], live: bool = True, workers: int = 6, max_live: int = 30) -> dict[str, dict]:
    """Кэш + параллельные онлайн-запросы. Сетевые ошибки не кэшируются и не всплывают наружу."""
    con = _db()
    out, known = {}, set()
    for i in range(0, len(inns), 900):
        chunk = inns[i:i + 900]
        for inn, data in con.execute(f"SELECT inn, data FROM rnp WHERE inn IN ({','.join('?' * len(chunk))})", chunk):
            known.add(inn)
            out[inn] = json.loads(data)
    todo = [i for i in inns if i not in known][:max_live] if live else []
    if todo:
        def one(inn):
            try:
                return inn, fetch_by_inn(inn)
            except Exception:  # noqa: BLE001 — недоступность ЕИС не должна ломать поиск
                return inn, None
        with ThreadPoolExecutor(workers) as ex:
            results = list(ex.map(one, todo))
        with _lock:
            for inn, rec in results:
                if rec is not None:
                    con.execute("INSERT OR REPLACE INTO rnp VALUES (?, ?, ?)",
                                (inn, json.dumps(rec, ensure_ascii=False), time.time()))
                    out[inn] = rec
            con.commit()
    con.close()
    return out


if __name__ == "__main__":
    import duckdb

    from app.config import DB_PATH

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    con = duckdb.connect(str(DB_PATH), read_only=True)
    inns = [r[0] for r in con.execute(
        f"SELECT inn FROM supplier_profile ORDER BY n_wins DESC, n_lots DESC LIMIT {limit}").fetchall()]
    con.close()
    t0, flagged = time.time(), 0
    for i in range(0, len(inns), 120):
        got = get_many(inns[i:i + 120], workers=6, max_live=120)
        flagged += sum(1 for v in got.values() if v.get("was_in_rnp"))
        print(f"[{time.time() - t0:6.0f}s] {min(i + 120, len(inns)):,}/{len(inns):,}, есть записи РНП: {flagged}", flush=True)
