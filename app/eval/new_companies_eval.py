"""Офлайн-проверка блока «Новые компании».

Вопрос: если бы сервис работал в конце 2024 года, нашёл бы блок «Новые компании» тех поставщиков,
которые впервые появились и победили в 2025 году?

- «новичок» = победитель закупки 2025 года, не участвовавший ни в одной закупке 2024 года;
- пул = реестр МСП (юрлица СПб и ЛО) минус все, кто участвовал в 2024 году;
- связка «ОКПД2 -> типичные ОКВЭД победителей» учится только на 2024 годе;
- по кодам ОКПД2 закупки считаем балл каждой компании пула (та же формула, что в выдаче) и смотрим место новичка.

Запуск: python -m app.eval.new_companies_eval [n_lots=3000]
"""
import json
import sys
import time
from collections import Counter

import duckdb
import numpy as np

from app.config import DATA_DIR, DB_PATH, LO_REGION, SPB_REGION
from app.enrich.store import EnrichStore
from app.eval.evaluate import SPLIT


def main(n_lots: int = 3000) -> None:
    t0 = time.time()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    hist = {r[0] for r in con.execute(f"""
        SELECT DISTINCT p.inn FROM participations p JOIN lots l USING (lot_id) WHERE l.publish_date < DATE '{SPLIT}'
    """).fetchall()}
    class_wins = con.execute(f"""
        SELECT p.inn, substr(i.okpd2_code, 1, 5) AS cls, count(DISTINCT p.lot_id) AS w
        FROM participations p JOIN lots l USING (lot_id)
        JOIN (SELECT DISTINCT lot_id, okpd2_code FROM lot_items WHERE okpd2_code IS NOT NULL) i USING (lot_id)
        WHERE p.is_winner AND l.publish_date < DATE '{SPLIT}' GROUP BY ALL
    """).fetchall()
    lots = con.execute(f"""
        SELECT l.lot_id, p.inn,
               (SELECT list(substr(i.okpd2_code, 1, 8)) FROM lot_items i
                WHERE i.lot_id = l.lot_id AND i.okpd2_code IS NOT NULL) AS codes
        FROM lots l JOIN participations p USING (lot_id)
        WHERE p.is_winner AND l.publish_date >= DATE '{SPLIT}'
        ORDER BY hash(l.lot_id)
    """).fetchall()
    con.close()
    total_2025 = len(lots)
    newcomers = [(lot, inn, codes) for lot, inn, codes in lots if inn not in hist and codes]
    print(f"[{time.time() - t0:4.0f}s] закупок 2025 с победителем: {total_2025:,}; "
          f"победил новичок (не было в 2024): {len(newcomers):,} ({len(newcomers) / total_2025:.1%})", flush=True)

    st = EnrichStore()
    # пул «как в конце 2024 года»: всё, чего не было в истории 2024
    in_hist = st.rmsp.index.astype(object).isin(hist)
    # те же правила, что в рабочем пуле: местные юрлица + иногородние производители (малые и средние)
    # и средние оптовики
    region = st.rmsp["region_code"].astype(object)
    cls = st.rmsp["okved_main"].astype(object).fillna("").str.extract(r"^(\d+)")[0].fillna("0").astype(int)
    cat = st.rmsp["msp_category"].astype(object)
    federal = (((cls >= 10) & (cls <= 32) & cat.isin(["малое", "среднее"])) | ((cls == 46) & (cat == "среднее"))).to_numpy()
    mask = (~in_hist & np.asarray(st.rmsp.index.astype(object).str.len() == 10)
            & (region.isin([SPB_REGION, LO_REGION]).to_numpy() | federal))
    st.pool = st.rmsp[mask]
    st._build_pool_index()
    st._class_wins, st._okved_by_class, st._mapping_built_at = class_wins, {}, 0.0
    pos_of = {inn: i for i, inn in enumerate(st.pool_inns)}
    print(f"[{time.time() - t0:4.0f}s] пул: {len(st.pool):,} компаний", flush=True)

    sample = newcomers[:n_lots]
    eligible, ranks, matched_sizes = 0, [], []
    why_not = Counter()
    for lot, inn, codes in sample:
        if inn not in pos_of:
            why_not["ИП" if len(inn) == 12 else "нет в пуле (крупная компания или иногородняя не из выгрузки)"] += 1
            continue
        eligible += 1
        cnt = Counter(codes)
        okpd = [{"code": c, "share": n / len(codes)} for c, n in cnt.most_common(2)]
        sc = st.pool_scores(okpd)
        mine = sc[pos_of[inn]]
        matched_sizes.append(int((sc > 0).sum()))
        ranks.append(int((sc > mine).sum()) + 1 if mine > 0 else 0)
    r = np.array(ranks)
    n = len(sample)
    out = {
        "newcomer_share_of_winners": round(len(newcomers) / total_2025, 4),
        "sample": n,
        "eligible_share": round(eligible / n, 4),
        "not_eligible": dict(why_not),
        "matched_by_okved_among_eligible": round(float((r > 0).mean()), 4),
        "recall_among_eligible": {f"top{k}": round(float(((r > 0) & (r <= k)).mean()), 4) for k in (10, 50, 200)},
        "recall_among_all_newcomers": {f"top{k}": round(float(((r > 0) & (r <= k)).sum() / n), 4) for k in (10, 50, 200)},
        "median_candidates_per_lot": int(np.median(matched_sizes)) if matched_sizes else 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    (DATA_DIR / "eval_new_companies.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{time.time() - t0:4.0f}s] готово")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:2]))
