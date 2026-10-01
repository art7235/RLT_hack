"""Сборка частотного словаря из текстов закупок.

Источники:
  data/raw/ТРУ_24-25.csv        -> product_name
  data/raw/Извещения_24-25.csv  -> procedure_name, subject

Результат: data/nlp/vocab.json  {"слово": частота}, сортировка по убыванию частоты.

Запуск:  python scripts/build_vocab.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nlp.tokenize import tokenize  # noqa: E402

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "nlp" / "vocab.json"

TRU = RAW / "ТРУ_24-25.csv"
NOTICES = RAW / "Извещения_24-25.csv"

MIN_FREQ = 3
MIN_LEN = 2
CHUNK = 500_000

READ_KW = dict(sep=";", quotechar='"', encoding="utf-8", dtype=str,
               on_bad_lines="skip", engine="c")


def count_column(path: Path, columns: list[str], counter: Counter) -> int:
    """Читает только нужные колонки чанками, дедуплицирует тексты, считает частоты."""
    if not path.exists():
        print(f"!! нет файла {path}", file=sys.stderr)
        return 0
    rows = 0
    seen: set[str] = set()          # уже посчитанные уникальные тексты
    t0 = time.time()
    for chunk in pd.read_csv(path, usecols=columns, chunksize=CHUNK, **READ_KW):
        rows += len(chunk)
        for col in columns:
            # дубликаты текстов убираем до токенизации — это основной ускоритель
            for text in chunk[col].dropna().unique():
                if text in seen:
                    continue
                seen.add(text)
                counter.update(tokenize(text))
        print(f"   {path.name}: {rows:,} строк, уникальных текстов {len(seen):,}, "
              f"{time.time() - t0:.0f}s", flush=True)
    return rows


def main() -> None:
    counter: Counter[str] = Counter()

    print("[1/3] ТРУ: product_name")
    count_column(TRU, ["product_name"], counter)

    print("[2/3] Извещения: procedure_name, subject")
    count_column(NOTICES, ["procedure_name", "subject"], counter)

    print("[3/3] Фильтрация и запись")
    print(f"   всего разных токенов до фильтра: {len(counter):,}")

    vocab = {
        w: c
        for w, c in counter.items()
        if c >= MIN_FREQ and len(w) >= MIN_LEN
    }
    vocab = dict(sorted(vocab.items(), key=lambda kv: (-kv[1], kv[0])))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False)

    print(f"\nУникальных слов в словаре: {len(vocab):,} "
          f"(порог частоты >= {MIN_FREQ}, длина >= {MIN_LEN})")
    print(f"Файл: {OUT.relative_to(ROOT)} "
          f"({OUT.stat().st_size / 1e6:.1f} МБ)")
    print("\nТоп-50:")
    for i, (w, c) in enumerate(list(vocab.items())[:50], 1):
        print(f"{i:>3}. {w:<25} {c:>10,}")


if __name__ == "__main__":
    main()
