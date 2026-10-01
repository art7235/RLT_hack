"""Частоты ЛЕММ из data/nlp/vocab.json (датасет не нужен).

Нужно для весов ключевых слов (IDF-подобный вес): «поставка» встречается
в каждом втором тексте и почти ничего не говорит о предмете закупки,
а «огнетушитель» — редкое и информативное слово.

Результат: data/nlp/lemma_freq.json  {"лемма": суммарная частота всех форм}
Запуск:    python scripts/build_lemma_freq.py     (~15–30 с)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nlp.speller import VOCAB_PATH, lemmatize  # noqa: E402

OUT = ROOT / "data" / "nlp" / "lemma_freq.json"


def main() -> None:
    t0 = time.time()
    with VOCAB_PATH.open(encoding="utf-8") as f:
        vocab: dict[str, int] = json.load(f)
    lemmas: Counter[str] = Counter()
    for w, c in vocab.items():
        lemmas[lemmatize(w)] += c
    out = dict(sorted(lemmas.items(), key=lambda kv: (-kv[1], kv[0])))
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{len(vocab):,} словоформ -> {len(out):,} лемм, {time.time() - t0:.0f}s -> {OUT}")


if __name__ == "__main__":
    main()
