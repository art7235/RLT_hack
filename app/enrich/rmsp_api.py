"""Онлайн-API реестра МСП (rmsp.nalog.ru): карточка по ИНН и расширенный поиск по ОКВЭД + региону.

Даёт то, чего нет в датасете: название, основной ОКВЭД, телефон, email, категорию, численность, город.
Всё кэшируется в SQLite (ENRICH_DB), повторные запросы не идут в сеть.

Прогрев кэша для самых активных поставщиков: python -m app.enrich.rmsp_api [limit]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import ENRICH_DB

BASE = "https://rmsp.nalog.ru/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "search.html",
}
CATEGORY = {1: "микро", 2: "малое", 3: "среднее"}
_lock = threading.Lock()
_local = threading.local()


def _db() -> sqlite3.Connection:
    ENRICH_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ENRICH_DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS rmsp (inn TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS rmsp_search (key TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
    return con


def _client() -> httpx.Client:
    if getattr(_local, "client", None) is None:
        # trust_env=False: системный прокси Windows ломает SSL у Python
        _local.client = httpx.Client(trust_env=False, timeout=20, headers=HEADERS, follow_redirects=True)
    return _local.client


def _norm(d: dict) -> dict:
    city = " ".join(x for x in (d.get("citytype"), d.get("cityname")) if x) or None
    return {
        "inn": d.get("inn"),
        "name": d.get("name_ex"),
        "ogrn": d.get("ogrn"),
        "kind": "ИП" if d.get("nptype") == "IP" else "ЮЛ",
        "region_code": d.get("regioncode"),
        "city": city,
        "okved_main": d.get("okved1"),
        "okved_main_name": d.get("okved1name"),
        "msp_category": CATEGORY.get(d.get("category")),
        "employees": d.get("od2_sschr"),
        "phone": d.get("phone"),
        "email": d.get("email"),
        "has_licenses": bool(d.get("has_licenses")),
        "is_hitech": bool(d.get("is_hitech")),
        "has_contracts": bool(d.get("has_contracts")),
        "msp_since": (d.get("dtregistry") or "")[:10] or None,
    }


def _post(data: dict) -> dict:
    r = _client().post(BASE + "search-proc.json", data=data)
    r.raise_for_status()
    return r.json()


def fetch_by_inn(inn: str) -> dict | None:
    j = _post({"mode": "quick", "query": inn, "page": "1", "pageSize": "10"})
    rows = [d for d in j.get("data", []) if d.get("inn") == inn]
    return _norm(rows[0]) if rows else None


def get_many(inns: list[str], live: bool = True, workers: int = 8, max_live: int = 40) -> dict[str, dict]:
    """Кэш + параллельные онлайн-запросы для недостающих ИНН. Отсутствие в реестре тоже кэшируется."""
    con = _db()
    out, known = {}, set()
    for i in range(0, len(inns), 900):
        chunk = inns[i:i + 900]
        for inn, data in con.execute(f"SELECT inn, data FROM rmsp WHERE inn IN ({','.join('?' * len(chunk))})", chunk):
            known.add(inn)
            if data:
                out[inn] = json.loads(data)
    todo = [i for i in inns if i not in known][:max_live] if live else []
    if todo:
        def one(inn):
            try:
                return inn, fetch_by_inn(inn), True
            except Exception:
                return inn, None, False
        with ThreadPoolExecutor(workers) as ex:
            results = list(ex.map(one, todo))
        with _lock:
            for inn, rec, ok in results:
                if ok:
                    con.execute("INSERT OR REPLACE INTO rmsp VALUES (?, ?, ?)",
                                (inn, json.dumps(rec, ensure_ascii=False) if rec else None, time.time()))
                if rec:
                    out[inn] = rec
            con.commit()
    con.close()
    return out


def search_extended(okved: str, region: str, page_size: int = 50, pages: int = 1) -> list[dict]:
    """Компании реестра МСП с данным основным ОКВЭД в регионе (78 — СПб, 47 — ЛО)."""
    key = f"{okved}|{region}|{page_size}|{pages}"
    con = _db()
    row = con.execute("SELECT data FROM rmsp_search WHERE key = ?", [key]).fetchone()
    if row:
        con.close()
        return json.loads(row[0])
    out = []
    try:
        for page in range(1, pages + 1):
            j = _post({"mode": "extended", "okved1": okved, "region": region,
                       "page": str(page), "pageSize": str(page_size)})
            data = j.get("data", [])
            out.extend(_norm(d) for d in data)
            if len(data) < page_size:
                break
    except Exception:
        con.close()
        return out
    con.execute("INSERT OR REPLACE INTO rmsp_search VALUES (?, ?, ?)",
                (key, json.dumps(out, ensure_ascii=False), time.time()))
    con.commit()
    con.close()
    return out


def cached_okved() -> dict[str, str]:
    """ИНН -> основной ОКВЭД по всему, что уже лежит в кэше (для обучения связки ОКПД2 -> ОКВЭД)."""
    con = _db()
    out = {}
    for inn, data in con.execute("SELECT inn, data FROM rmsp WHERE data IS NOT NULL"):
        ok = json.loads(data).get("okved_main")
        if ok:
            out[inn] = ok
    con.close()
    return out


if __name__ == "__main__":
    import duckdb

    from app.config import DB_PATH

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    con = duckdb.connect(str(DB_PATH), read_only=True)
    inns = [r[0] for r in con.execute(
        f"SELECT inn FROM supplier_profile ORDER BY n_wins DESC, n_lots DESC LIMIT {limit}").fetchall()]
    con.close()
    t0 = time.time()
    for i in range(0, len(inns), 200):
        got = get_many(inns[i:i + 200], workers=6, max_live=200)
        print(f"[{time.time() - t0:6.0f}s] {i + 200:,}/{len(inns):,}, in registry: {len(got)}/200", flush=True)
