"""Оценка в целевом сценарии «по данным закупки».

На вход системе даём полную карточку реальной закупки 2025 года (название, позиции, ОКПД2, заказчик, НМЦ,
площадка, СМП), история — только 2024. Смотрим, попал ли фактический победитель в топ-10.

Воспроизводимость: список тестовых закупок зафиксирован в tests/eval_lots.csv (детерминированный отбор
по хэшу номера лота, без случайной выборки). Для метрик считаются 95%-интервалы (бутстреп).

Сравниваем с бейзлайнами, у которых ТЕ ЖЕ входные данные:
  - «топ по ОКПД2»: самые частые победители 2024 года по кодам ОКПД2 из спецификации;
  - «заказчик + ОКПД2»: кто выигрывал у этого заказчика в этом классе ОКПД2, затем добор топом по коду.

Запуск: python -m app.eval.procedure_eval [n_test=1000] [n_name_only=300]
"""
import json
import pickle
import sys
import time
from collections import defaultdict

import duckdb
import numpy as np
import pandas as pd

from app.config import DATA_DIR, DB_PATH, ROOT
from app.eval.evaluate import K, SPLIT, _history_tables
from app.search import engine as eng_mod
from app.search.engine import OKPD_LEVEL, Query

LOTS_FILE = ROOT / "tests" / "eval_lots.csv"
RESULT_FILE = DATA_DIR / "eval_result.json"


def test_lots(con, n: int) -> pd.DataFrame:
    """Фиксированный список тестовых закупок 2025 года с известным победителем."""
    if LOTS_FILE.exists():
        ids = pd.read_csv(LOTS_FILE)["lot_id"].tolist()
    else:
        ids = [r[0] for r in con.execute(f"""
            SELECT l.lot_id FROM lots l
            WHERE l.publish_date >= DATE '{SPLIT}'
              AND EXISTS (SELECT 1 FROM participations p WHERE p.lot_id = l.lot_id AND p.is_winner)
            ORDER BY hash(l.lot_id) LIMIT 2000
        """).fetchall()]
        LOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"lot_id": ids}).to_csv(LOTS_FILE, index=False)
    ids = ids[:n]
    return con.execute(f"""
        SELECT l.lot_id, l.subject, l.start_price, l.customer_inn, l.platform, l.is_smp,
               (SELECT list(p.inn) FROM participations p WHERE p.lot_id = l.lot_id AND p.is_winner) AS winners,
               (SELECT list(p.inn) FROM participations p WHERE p.lot_id = l.lot_id) AS participants,
               (SELECT list(i.product_name ORDER BY i.product_name) FROM lot_items i WHERE i.lot_id = l.lot_id) AS items,
               (SELECT list(i.okpd2_code ORDER BY i.product_name) FROM lot_items i WHERE i.lot_id = l.lot_id) AS codes
        FROM lots l WHERE l.lot_id IN ({",".join(map(str, ids))})
    """).df()


def _card(row) -> dict:
    items = [x for x in (row.items if row.items is not None else []) if x][:50]
    codes = [x or "" for x in (row.codes if row.codes is not None else [])][:50]
    return {"text": row.subject, "price": None if pd.isna(row.start_price) else float(row.start_price),
            "customer_inn": row.customer_inn, "platform": row.platform, "is_smp": bool(row.is_smp),
            "items": items, "okpd_codes": codes}


def boot_ci(x: np.ndarray, n: int = 2000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(n, len(x)))].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def main(n_test: int = 1000, n_name: int = 300) -> None:
    t0 = time.time()
    e = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    e.profile, e.sokpd = _history_tables(con)
    qs = test_lots(con, n_test)
    # бейзлайны: победы 2024 года по (заказчик, класс ОКПД2) и по коду
    cust_cls = defaultdict(lambda: defaultdict(int))
    for cust, cls, inn, w in con.execute(f"""
        SELECT l.customer_inn, substr(i.okpd2_code, 1, 5), p.inn, count(DISTINCT l.lot_id)
        FROM participations p JOIN lots l USING (lot_id)
        JOIN (SELECT DISTINCT lot_id, okpd2_code FROM lot_items WHERE okpd2_code IS NOT NULL) i USING (lot_id)
        WHERE p.is_winner AND l.publish_date < DATE '{SPLIT}' GROUP BY ALL
    """).fetchall():
        cust_cls[(cust, cls)][inn] += w
    con.close()
    by_code = {c: g.sort_values("n_wins", ascending=False)["inn"].head(50).tolist()
               for c, g in e.sokpd.groupby("code8")}
    print(f"[{time.time() - t0:5.0f}s] {len(qs)} тестовых закупок (история до {SPLIT})", flush=True)

    feats = list(eng_mod.W)
    rows = []
    for n, row in enumerate(qs.itertuples(index=False)):
        card = _card(row)
        winners, parts = set(row.winners), set(row.participants)
        res = {"lot_id": int(row.lot_id), "platform": row.platform}

        def rank_of(top):
            r = [i for i, x in enumerate(top[:K]) if x in winners]
            return (r[0] + 1) if r else 0

        full = [s["inn"] for s in e.search(Query(limit=K, before_date=SPLIT, **card))["suppliers"]]
        res["card"] = rank_of(full)
        res["card_recall"] = len(parts & set(full)) / len(parts)
        if n < n_name:
            res["name"] = rank_of([s["inn"] for s in e.search(Query(text=row.subject, limit=K, before_date=SPLIT))["suppliers"]])
        # бейзлайны на тех же входных данных
        codes8 = [c[:OKPD_LEVEL] for c in card["okpd_codes"] if c]
        top_code, seen = [], set()
        for c in dict.fromkeys(codes8):
            for inn in by_code.get(c, []):
                if inn not in seen:
                    seen.add(inn); top_code.append(inn)
        res["base_okpd"] = rank_of(top_code)
        cc = defaultdict(int)
        for cls in {c[:5] for c in card["okpd_codes"] if c}:
            for inn, w in cust_cls.get((row.customer_inn, cls), {}).items():
                cc[inn] += w
        strong = sorted(cc, key=cc.get, reverse=True)
        strong += [i for i in top_code if i not in cc]
        res["base_customer"] = rank_of(strong)
        rows.append(res)
        if (n + 1) % 100 == 0:
            print(f"[{time.time() - t0:5.0f}s] {n + 1}/{len(qs)}", flush=True)

    df = pd.DataFrame(rows)
    out = {"n": len(df), "split": str(SPLIT), "k": K, "metrics": {}}
    print(f"\\nИТОГ: {len(df)} закупок 2025 года, история 2024, топ-{K}")
    for key, label in [("card", "наш алгоритм, карточка закупки"), ("base_customer", "бейзлайн: заказчик + ОКПД2"),
                       ("base_okpd", "бейзлайн: топ по ОКПД2"), ("name", "наш алгоритм, только название")]:
        r = df[key].dropna().to_numpy()
        hit = (r > 0).astype(float)
        rr = np.where(r > 0, 1.0 / np.maximum(r, 1), 0.0)
        lo, hi = boot_ci(hit)
        out["metrics"][key] = {"label": label, "n": int(len(r)), "hit": round(hit.mean(), 4), "ci": [round(lo, 4), round(hi, 4)],
                               "mrr": round(rr.mean(), 4)}
        print(f"  {label:34s} n={len(r):4d}  Hit@{K} {hit.mean():.1%} (95% ДИ {lo:.1%}–{hi:.1%})  MRR {rr.mean():.3f}")
    d = (df["card"] > 0).astype(float).to_numpy() - (df["base_customer"] > 0).astype(float).to_numpy()
    lo, hi = boot_ci(d)
    out["gain_vs_strong_baseline"] = {"diff": round(d.mean(), 4), "ci": [round(lo, 4), round(hi, 4)]}
    print(f"  прирост к сильному бейзлайну: {d.mean():+.1%} (95% ДИ {lo:+.1%}…{hi:+.1%})")
    for pl, g in df.groupby("platform"):
        print(f"  {pl}: n={len(g)}, наш {np.mean(g['card'] > 0):.1%}, сильный бейзлайн {np.mean(g['base_customer'] > 0):.1%}")
        out["metrics"][f"card_{pl}"] = {"n": int(len(g)), "hit": round(float(np.mean(g["card"] > 0)), 4),
                                        "baseline": round(float(np.mean(g["base_customer"] > 0)), 4)}
    out["recall_participants"] = round(float(df["card_recall"].mean()), 4)
    RESULT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(DATA_DIR / "eval_rows.pkl", "wb") as fh:
        pickle.dump(df, fh)
    print(f"[{time.time() - t0:5.0f}s] сохранено: {RESULT_FILE}")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:3]))
