"""Офлайн-оценка качества рекомендаций.

Честный split по времени: история = 2024 год (лоты, опыт по ОКПД2, доля побед),
запросы = реальные лоты 2025 года (берём их предмет как текст запроса).
Смотрим, попадают ли фактические победитель и участники в топ-K.

Запуск: python -m app.eval.evaluate [n_queries]
"""
import sys
import time
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from app.config import DB_PATH
from app.search import engine as eng_mod
from app.search.engine import OKPD_LEVEL, Query, SearchEngine

SPLIT = date(2025, 1, 1)
K = 10


def _history_tables(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    profile = con.execute(f"""
        SELECT p.inn,
               mode(p.kpp) AS kpp,
               CASE WHEN length(p.inn) = 12 THEN 'ИП' ELSE 'ЮЛ' END AS entity_type,
               substr(coalesce(mode(p.kpp), p.inn), 1, 2) AS region_code,
               count(DISTINCT lot_id) AS n_lots,
               count(DISTINCT lot_id) FILTER (WHERE is_winner) AS n_wins,
               round(count(DISTINCT lot_id) FILTER (WHERE is_winner) / count(DISTINCT lot_id), 3) AS win_rate,
               count(DISTINCT customer_inn) AS n_customers,
               count(DISTINCT lot_id) FILTER (WHERE platform = 'ЭМ') AS n_eshop,
               count(DISTINCT lot_id) FILTER (WHERE platform = 'АИС ГЗ') AS n_aisgz,
               max(publish_date) AS last_date,
               median(start_price) AS median_price
        FROM participations p JOIN lots l USING (lot_id)
        WHERE l.publish_date < DATE '{SPLIT}'
        GROUP BY p.inn
    """).df().set_index("inn")
    sokpd = con.execute(f"""
        WITH li AS (SELECT DISTINCT lot_id, substr(okpd2_code, 1, {OKPD_LEVEL}) AS code8
                    FROM lot_items WHERE okpd2_code IS NOT NULL)
        SELECT li.code8, p.inn,
               count(DISTINCT p.lot_id) AS n_lots,
               count(DISTINCT p.lot_id) FILTER (WHERE p.is_winner) AS n_wins,
               max(l.publish_date) AS last_date
        FROM participations p JOIN li USING (lot_id) JOIN lots l USING (lot_id)
        WHERE l.publish_date < DATE '{SPLIT}'
        GROUP BY li.code8, p.inn
    """).df()
    return profile, sokpd


def _sample_queries(con, n: int, seed: int = 42) -> pd.DataFrame:
    return con.execute(f"""
        SELECT * FROM (
        SELECT l.lot_id, l.subject, l.platform,
               list(p.inn) FILTER (WHERE p.is_winner) AS winners,
               list(p.inn) AS participants
        FROM lots l JOIN participations p USING (lot_id)
        WHERE l.publish_date >= DATE '{SPLIT}'
        GROUP BY ALL
        HAVING count(*) FILTER (WHERE p.is_winner) > 0
        ) USING SAMPLE reservoir({n} ROWS) REPEATABLE ({seed})
    """).df()


def baseline_okpd_popularity(e: SearchEngine, text: str, q: Query) -> list[str]:
    """Бейзлайн: ОКПД2 запроса -> самые активные поставщики по этому коду."""
    from app.core.text import doc_terms
    terms = doc_terms(text)
    pos, sims = e.similar_lots(terms, q)
    okpd = e.predict_okpd(terms, pos, sims)
    if not okpd:
        return []
    sub = e.sokpd[e.sokpd["code8"] == okpd[0]["code"]]
    return sub.sort_values("n_wins", ascending=False)["inn"].head(K).tolist()


def evaluate(n: int = 300) -> dict:
    t0 = time.time()
    e = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    e.profile, e.sokpd = _history_tables(con)  # подменяем на историю до SPLIT
    qs = _sample_queries(con, n)
    con.close()
    print(f"[{time.time() - t0:5.0f}s] {len(qs)} queries")

    res = {"model": {"hit": [], "rr": [], "part_recall": []},
           "baseline": {"hit": [], "rr": [], "part_recall": []}}
    for row in qs.itertuples(index=False):
        winners, parts = set(row.winners), set(row.participants)
        q = Query(text=row.subject, limit=K, before_date=SPLIT)
        ranked_model = [s["inn"] for s in e.search(q)["suppliers"]]
        ranked_base = baseline_okpd_popularity(e, row.subject, q)
        for name, ranked in (("model", ranked_model), ("baseline", ranked_base)):
            r = res[name]
            ranks = [i for i, inn in enumerate(ranked[:K]) if inn in winners]
            r["hit"].append(1.0 if ranks else 0.0)
            r["rr"].append(1.0 / (ranks[0] + 1) if ranks else 0.0)
            r["part_recall"].append(len(parts & set(ranked[:K])) / len(parts))
    out = {name: {f"Hit@{K} (победитель в топе)": round(float(np.mean(r["hit"])), 3),
                  "MRR": round(float(np.mean(r["rr"])), 3),
                  f"Recall@{K} участников": round(float(np.mean(r["part_recall"])), 3)}
           for name, r in res.items()}
    print(f"[{time.time() - t0:5.0f}s] done")
    for name, m in out.items():
        print(f"  {name:9s} {m}")
    return out


if __name__ == "__main__":
    evaluate(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
