"""ЕГРЮЛ/ЕГРИП (egrul.nalog.ru): название, ОГРН, регион, руководитель, статус по ИНН."""
from __future__ import annotations

import json
import sqlite3
import sys
import time

import httpx

from app.config import ENRICH_DB

URL = "https://egrul.nalog.ru/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://egrul.nalog.ru/index.html",
}
MIN_INTERVAL = 0.4


def _db() -> sqlite3.Connection:
    ENRICH_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ENRICH_DB)
    con.execute("CREATE TABLE IF NOT EXISTS egrul (inn TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
    return con


class Egrul:
    def __init__(self) -> None:
        self.client = httpx.Client(trust_env=False, timeout=20, headers=HEADERS, follow_redirects=True)
        self.client.get(URL + "index.html")
        self._last = 0.0

    def _throttle(self) -> None:
        dt = time.time() - self._last
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        self._last = time.time()

    def fetch(self, inn: str) -> dict | None:
        self._throttle()
        r = self.client.post(URL, data={"query": inn, "vyp3CaptchaToken": "", "page": ""})
        r.raise_for_status()
        j = r.json()
        if j.get("captchaRequired"):
            raise RuntimeError("captcha required")
        token = j["t"]
        for _ in range(10):
            res = self.client.get(f"{URL}search-result/{token}", params={"r": int(time.time() * 1000)}).json()
            if res.get("status") != "wait":
                break
            time.sleep(0.3)
        rows = [x for x in res.get("rows", []) if x.get("i") == inn]
        if not rows:
            return None
        x = rows[0]
        return {
            "inn": inn,
            "name": x.get("c") or x.get("n"),
            "full_name": x.get("n"),
            "ogrn": x.get("o"),
            "kpp": x.get("p"),
            "region": x.get("rn"),
            "head": x.get("g"),
            "registered": x.get("r"),
            "liquidated": x.get("e"),
            "is_active": not x.get("e"),
            "kind": "ИП" if x.get("k") == "fl" else "ЮЛ",
        }


def get_cached(inns: list[str]) -> dict[str, dict]:
    con = _db()
    out = {}
    for i in range(0, len(inns), 900):
        chunk = inns[i:i + 900]
        q = f"SELECT inn, data FROM egrul WHERE inn IN ({','.join('?' * len(chunk))})"
        for inn, data in con.execute(q, chunk):
            if data:
                out[inn] = json.loads(data)
    con.close()
    return out


def fetch_many(inns: list[str], log_every: int = 100) -> None:
    con = _db()
    done = {r[0] for r in con.execute("SELECT inn FROM egrul")}
    todo = [i for i in inns if i not in done]
    print(f"egrul: {len(todo):,} to fetch ({len(done):,} cached)")
    eg = Egrul()
    t0 = time.time()
    for n, inn in enumerate(todo, 1):
        try:
            rec = eg.fetch(inn)
        except Exception as e:
            print(f"  {inn}: {e}; pause 30s")
            time.sleep(30)
            for _ in range(10):
                try:
                    eg = Egrul()
                    break
                except Exception:
                    time.sleep(30)
            continue
        con.execute("INSERT OR REPLACE INTO egrul VALUES (?, ?, ?)",
                    (inn, json.dumps(rec, ensure_ascii=False) if rec else None, time.time()))
        if n % log_every == 0:
            con.commit()
            print(f"  [{time.time() - t0:6.0f}s] {n:,}/{len(todo):,}")
    con.commit()
    con.close()


if __name__ == "__main__":
    import duckdb

    from app.config import DATA_DIR, DB_PATH

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rmsp = DATA_DIR / "ext" / "rmsp.parquet"
    sql = "SELECT inn FROM supplier_profile"
    if rmsp.exists():
        sql += f" WHERE inn NOT IN (SELECT inn FROM read_parquet('{rmsp.as_posix()}'))"
    sql += " ORDER BY n_lots DESC"
    if limit:
        sql += f" LIMIT {limit}"
    inns = [r[0] for r in con.execute(sql).fetchall()]
    con.close()
    fetch_many(inns)
