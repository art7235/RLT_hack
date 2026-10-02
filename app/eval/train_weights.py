"""Подбор весов скоринга логистической регрессией на истории."""
import json
import time

import duckdb
import numpy as np
from sklearn.linear_model import LogisticRegression

from app.config import DATA_DIR, DB_PATH
from app.eval.evaluate import K, SPLIT, _history_tables, _sample_queries
from app.search import engine as eng_mod
from app.search.engine import Query

FEATURES = ["text", "okpd", "rel_wins", "max_sim", "wins", "recency", "region", "breadth"]
WEIGHTS_PATH = DATA_DIR / "weights.json"


def collect(e, qs) -> list[dict]:
    out = []
    for row in qs.itertuples(index=False):
        c = e.candidates(Query(text=row.subject, before_date=SPLIT))
        inns = [inn for inn, *_ in c["rows"]]
        X = np.array([[f[k] for k in FEATURES] for _, f, *_ in c["rows"]], dtype=np.float32).reshape(-1, len(FEATURES))
        winners, parts = set(row.winners), set(row.participants)
        out.append({"inns": inns, "X": X,
                    "y": np.array([1 if i in winners else 0 for i in inns]),
                    "winners": winners, "parts": parts})
    return out


def metrics(data: list[dict], w: np.ndarray) -> dict:
    hit, rr, rec, ceiling = [], [], [], []
    for d in data:
        if len(d["inns"]) == 0:
            hit.append(0); rr.append(0); rec.append(0); ceiling.append(0)
            continue
        order = np.argsort(-(d["X"] @ w))[:K]
        top = [d["inns"][i] for i in order]
        ranks = [i for i, inn in enumerate(top) if inn in d["winners"]]
        hit.append(1 if ranks else 0)
        rr.append(1 / (ranks[0] + 1) if ranks else 0)
        rec.append(len(d["parts"] & set(top)) / len(d["parts"]))
        ceiling.append(1 if d["y"].any() else 0)
    return {f"Hit@{K}": round(float(np.mean(hit)), 3), "MRR": round(float(np.mean(rr)), 3),
            f"Recall@{K} участников": round(float(np.mean(rec)), 3),
            "потолок (победитель среди кандидатов)": round(float(np.mean(ceiling)), 3)}


def main() -> None:
    t0 = time.time()
    e = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    e.profile, e.sokpd = _history_tables(con)
    train_q = _sample_queries(con, 600, seed=1)
    test_q = _sample_queries(con, 200, seed=42)
    test_q = test_q[~test_q["lot_id"].isin(train_q["lot_id"])]
    known_2024 = set(e.profile.index)
    con.close()

    cold = np.mean([not (set(w) & known_2024) for w in test_q["winners"]])
    print(f"[{time.time() - t0:5.0f}s] train {len(train_q)}, test {len(test_q)}; "
          f"победитель без истории в 2024 (холодный старт): {cold:.1%}")

    train, test = collect(e, train_q), collect(e, test_q)
    print(f"[{time.time() - t0:5.0f}s] features collected")

    X = np.vstack([d["X"] for d in train if len(d["inns"])])
    y = np.concatenate([d["y"] for d in train if len(d["inns"])])
    clf = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000).fit(X, y)
    w_learned = np.clip(clf.coef_[0], 0, None)
    w_learned = w_learned / w_learned.sum()

    w_hand = np.array([eng_mod.W.get(k, 0) for k in FEATURES])
    print("raw coef:", dict(zip(FEATURES, np.round(clf.coef_[0], 3))))
    for name, w in (("hand", w_hand), ("learned", w_learned)):
        print(f"  {name:8s} train {metrics(train, w)}")
        print(f"  {name:8s} test  {metrics(test, w)}")

    weights = {k: round(float(v), 4) for k, v in zip(FEATURES, w_learned)}
    WEIGHTS_PATH.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", weights, "->", WEIGHTS_PATH)


if __name__ == "__main__":
    main()
