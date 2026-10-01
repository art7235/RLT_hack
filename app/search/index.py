"""TF-IDF индекс по лотам: предмет лота + позиции ТРУ.

Строится один раз (python -m app.search.index), грузится в память при старте API.
Позиции ТРУ чище, чем предмет (там часто заказчик/район), поэтому весим их выше.
"""
import pickle
import time

import duckdb
import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

from app.config import DB_PATH, INDEX_DIR
from app.core.text import doc_terms

ITEMS_WEIGHT = 2  # позиции ТРУ повторяем дважды
MAX_ITEMS_CHARS = 3000


def _doc(subject: str, items: str) -> list[str]:
    return doc_terms(subject) + doc_terms((items or "")[:MAX_ITEMS_CHARS]) * ITEMS_WEIGHT


def build() -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute("SELECT lot_id, subject, items FROM lot_docs ORDER BY lot_id").fetchall()
    con.close()
    print(f"[{time.time() - t0:6.1f}s] loaded {len(rows):,} docs")

    lot_ids = np.array([r[0] for r in rows], dtype=np.int64)
    docs = []
    for i, (_, subject, items) in enumerate(rows):
        docs.append(_doc(subject, items))
        if i % 100_000 == 0:
            print(f"[{time.time() - t0:6.1f}s] tokenized {i:,}")

    vec = TfidfVectorizer(
        analyzer=lambda x: x,  # уже токенизировано
        min_df=2,
        max_df=0.3,
        sublinear_tf=True,
        dtype=np.float32,
    )
    X = vec.fit_transform(docs).tocsr()
    print(f"[{time.time() - t0:6.1f}s] matrix {X.shape}, nnz={X.nnz:,}")

    # храним транспонированную матрицу: по терму сразу получаем лоты (как инвертированный индекс)
    sparse.save_npz(INDEX_DIR / "tfidf_T.npz", X.T.tocsr())
    np.save(INDEX_DIR / "lot_ids.npy", lot_ids)
    with open(INDEX_DIR / "vectorizer.pkl", "wb") as f:
        pickle.dump({"vocabulary": vec.vocabulary_, "idf": vec.idf_}, f)
    print(f"[{time.time() - t0:6.1f}s] saved -> {INDEX_DIR}")


if __name__ == "__main__":
    build()
