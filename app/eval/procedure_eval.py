"""Оценка в целевом сценарии «по данным закупки» + подбор весов.

На вход системе даём полную карточку реальной закупки 2025 года (название, позиции, ОКПД2,
заказчик, НМЦ, площадка, СМП), история — только 2024. Сравниваем:
  - «только название» (старый сценарий поисковой строки),
  - «карточка закупки» с ручными весами,
  - «карточка закупки» с весами логистической регрессии (обучение 600 / проверка 200 закупок).

Запуск: python -m app.eval.procedure_eval
"""
import json
import time

import duckdb
import numpy as np
from sklearn.linear_model import LogisticRegression

from app.config import DATA_DIR, DB_PATH
from app.eval.evaluate import K, SPLIT, _history_tables, _sample_queries
from app.search import engine as eng_mod
from app.search.engine import Query

WEIGHTS_PATH = DATA_DIR / "weights_procedure.json"


def _cards(con, lot_ids: list[int]) -> dict[int, dict]:
    rows = con.execute(f"""
        SELECT l.lot_id, l.subject, l.start_price, l.customer_inn, l.platform, l.is_smp,
               list(i.product_name ORDER BY i.product_name) AS items,
               list(i.okpd2_code ORDER BY i.product_name) AS codes
        FROM lots l LEFT JOIN lot_items i USING (lot_id)
        WHERE l.lot_id IN ({",".join(map(str, lot_ids))})
        GROUP BY ALL
    """).fetchall()
    return {r[0]: {"text": r[1], "price": r[2], "customer_inn": r[3], "platform": r[4], "is_smp": r[5],
                   "items": [x for x in (r[6] or []) if x][:50], "okpd_codes": [x for x in (r[7] or []) if x][:50]}
            for r in rows}


def collect(e, qs, cards, full: bool) -> list[dict]:
    out = []
    for row in qs.itertuples(index=False):
        c = cards[row.lot_id]
        q = Query(**c, before_date=SPLIT) if full else Query(text=row.subject, before_date=SPLIT)
        cand = e.candidates(q)
        inns = [inn for inn, *_ in cand["rows"]]
        feats = list(eng_mod.W)
        X = np.array([[f[k] for k in feats] for _, f, *_ in cand["rows"]], dtype=np.float32).reshape(-1, len(feats))
        mask = np.array([0.0 if k in cand["inactive"] else 1.0 for k in feats], dtype=np.float32)
        out.append({"inns": inns, "X": X, "mask": mask, "winners": set(row.winners), "parts": set(row.participants),
                    "y": np.array([1 if i in set(row.winners) else 0 for i in inns])})
    return out


def metrics(data, w: np.ndarray) -> dict:
    hit, rr, rec, ceil = [], [], [], []
    for d in data:
        if not len(d["inns"]):
            hit.append(0); rr.append(0); rec.append(0); ceil.append(0)
            continue
        ww = w * d["mask"]
        order = np.argsort(-(d["X"] @ ww))[:K]
        top = [d["inns"][i] for i in order]
        ranks = [i for i, x in enumerate(top) if x in d["winners"]]
        hit.append(1 if ranks else 0)
        rr.append(1 / (ranks[0] + 1) if ranks else 0)
        rec.append(len(d["parts"] & set(top)) / len(d["parts"]))
        ceil.append(1 if d["y"].any() else 0)
    return {f"Hit@{K}": round(float(np.mean(hit)), 3), "MRR": round(float(np.mean(rr)), 3),
            f"Recall@{K}": round(float(np.mean(rec)), 3), "потолок": round(float(np.mean(ceil)), 3)}


def main() -> None:
    t0 = time.time()
    e = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    e.profile, e.sokpd = _history_tables(con)
    train_q = _sample_queries(con, 600, seed=1)
    test_q = _sample_queries(con, 200, seed=42)
    test_q = test_q[~test_q["lot_id"].isin(train_q["lot_id"])]
    cards = _cards(con, list(train_q["lot_id"]) + list(test_q["lot_id"]))
    con.close()
    print(f"[{time.time() - t0:5.0f}s] train {len(train_q)}, test {len(test_q)}")

    feats = list(eng_mod.W)
    w_hand = np.array([eng_mod.W[k] for k in feats])
    test_name = collect(e, test_q, cards, full=False)
    print(f"[{time.time() - t0:5.0f}s] name-only collected")
    train_full, test_full = collect(e, train_q, cards, True), collect(e, test_q, cards, True)
    print(f"[{time.time() - t0:5.0f}s] full cards collected")

    X = np.vstack([d["X"] for d in train_full if len(d["inns"])])
    y = np.concatenate([d["y"] for d in train_full if len(d["inns"])])
    clf = LogisticRegression(C=1.0, class_weight="balanced", max_iter=3000).fit(X, y)
    w_learned = np.clip(clf.coef_[0], 0, None)
    w_learned = w_learned / w_learned.sum()
    print("raw coef:", dict(zip(feats, np.round(clf.coef_[0], 3))))

    print("TEST (200 закупок 2025, история 2024):")
    print("  только название, ручные веса   ", metrics(test_name, w_hand))
    print("  карточка закупки, ручные веса  ", metrics(test_full, w_hand))
    print("  карточка закупки, обученные    ", metrics(test_full, w_learned))
    weights = {k: round(float(v), 4) for k, v in zip(feats, w_learned)}
    WEIGHTS_PATH.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")
    print("learned:", weights)


if __name__ == "__main__":
    main()
