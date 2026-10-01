"""Индекс позиций ТРУ для определения ОКПД2 запроса (kNN по уникальным названиям позиций).

В лоте «продукты питания» десятки позиций, поэтому голосование по лотам размазывается.
Здесь голосуют сами позиции: «молоко» -> «Молоко питьевое 3,2%» -> 10.51.11.

Сборка: python -m app.search.okpd_index
"""
import pickle
import time

import duckdb
import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

from app.config import DB_PATH, INDEX_DIR
from app.core.text import doc_terms

OKPD_LEVEL = 8


def build() -> None:
    t0 = time.time()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute(f"""
        SELECT product_name, substr(okpd2_code, 1, {OKPD_LEVEL}) AS code8, count(*) AS n
        FROM lot_items
        WHERE okpd2_code IS NOT NULL AND product_name IS NOT NULL AND length(product_name) > 2
        GROUP BY ALL
    """).fetchall()
    con.close()
    print(f"[{time.time() - t0:6.1f}s] {len(rows):,} (name, code) pairs")

    docs = [doc_terms(r[0]) for r in rows]
    print(f"[{time.time() - t0:6.1f}s] tokenized")
    vec = TfidfVectorizer(analyzer=lambda x: x, min_df=2, max_df=0.2, sublinear_tf=True, dtype=np.float32)
    X = vec.fit_transform(docs).tocsr()
    print(f"[{time.time() - t0:6.1f}s] matrix {X.shape}, nnz={X.nnz:,}")

    sparse.save_npz(INDEX_DIR / "items_T.npz", X.T.tocsr())
    np.save(INDEX_DIR / "items_code.npy", np.array([r[1] for r in rows], dtype=object), allow_pickle=True)
    np.save(INDEX_DIR / "items_name.npy", np.array([r[0] for r in rows], dtype=object), allow_pickle=True)
    np.save(INDEX_DIR / "items_n.npy", np.array([r[2] for r in rows], dtype=np.int32))
    with open(INDEX_DIR / "items_vectorizer.pkl", "wb") as f:
        pickle.dump({"vocabulary": vec.vocabulary_, "idf": vec.idf_}, f)
    print(f"[{time.time() - t0:6.1f}s] saved")


if __name__ == "__main__":
    build()
