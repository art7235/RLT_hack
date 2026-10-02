"""Добыча знаний из датасета: аббревиатуры и связь «слово -> ОКПД2»."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nlp.keywords import CUSTOMER, PROCEDURE  # noqa: E402
from nlp.speller import canon_token, lemmatize, pos_tag  # noqa: E402
from nlp.tokenize import tokenize  # noqa: E402

OUT_SYN = ROOT / "data" / "nlp" / "synonyms_mined.json"
OUT_OKPD = ROOT / "data" / "nlp" / "lemma_okpd2.json"

READ_KW = dict(sep=";", quotechar='"', encoding="utf-8", dtype=str,
               on_bad_lines="skip", engine="c")
CHUNK = 500_000

MIN_ABBR = 3
MIN_LEMMA = 5
TOP_OKPD = 5
MIN_P = 0.05
OKPD_LEVEL = 5

_ABBR = re.compile(r"\(\s*([А-ЯЁA-Z]{2,8})\s*\)")
_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]+(?:-[А-Яа-яЁёA-Za-z]+)*")
_SKIP = {"и", "в", "во", "на", "по", "для", "с", "со", "к", "о", "об", "от", "из", "или", "а"}


def _initials(words: list[str]) -> str:
    out = []
    for w in words:
        if w.lower() in _SKIP:
            continue
        for part in w.split("-"):
            if part:
                out.append(part[0].lower())
    return "".join(out)


def find_abbreviations(text: str) -> list[tuple[str, str]]:
    """[(аббр, расшифровка)] из одного текста."""
    res = []
    for m in _ABBR.finditer(text):
        abbr = m.group(1).lower().replace("ё", "е")
        before = _WORD.findall(text[max(0, m.start() - 200):m.start()])
        for k in range(1, min(len(before), len(abbr) + 4) + 1):
            tail = before[-k:]
            if tail[0].lower() in _SKIP:
                continue
            if _initials(tail) == abbr:
                res.append((abbr, " ".join(w.lower() for w in tail).replace("ё", "е")))
                break
    return res


def read_texts(path: Path, columns: list[str]):
    if not path.exists():
        print(f"!! нет файла {path}", file=sys.stderr)
        return
    for chunk in pd.read_csv(path, usecols=columns, chunksize=CHUNK, **READ_KW):
        yield chunk


def content_lemmas(text: str) -> set[str]:
    out = set()
    for tok in tokenize(text):
        tok = canon_token(tok)
        lem = lemmatize(tok)
        if lem in PROCEDURE or lem in CUSTOMER or len(lem) < 2:
            continue
        if pos_tag(tok) in {"NOUN", "ADJF", "PRTF", "LATN", "NUM", "UNKN"}:
            out.add(lem)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    args = ap.parse_args()
    raw = Path(args.raw)
    tru, notices = raw / "ТРУ_24-25.csv", raw / "Извещения_24-25.csv"
    t0 = time.time()

    pairs: Counter[tuple[str, str]] = Counter()
    seen: set[str] = set()
    sources = [(notices, ["procedure_name", "subject"]), (tru, ["product_name"])]
    for path, cols in sources:
        for chunk in read_texts(path, cols):
            for col in cols:
                for text in chunk[col].dropna().unique():
                    if text in seen or "(" not in text:
                        continue
                    seen.add(text)
                    for p in find_abbreviations(text):
                        pairs[p] += 1
    by_abbr: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (abbr, full), c in pairs.items():
        if c >= MIN_ABBR:
            by_abbr[abbr].append((full, c))
    groups = []
    for abbr, lst in sorted(by_abbr.items()):
        total = sum(c for _, c in lst)
        fulls = [f for f, c in sorted(lst, key=lambda x: -x[1]) if c / total >= 0.15][:4]
        if fulls:
            groups.append([abbr] + fulls)
    OUT_SYN.write_text(json.dumps(
        {"_doc": "Сгенерировано scripts/mine_synonyms.py: «расшифровка (АББР)» из датасета",
         "synonyms": groups, "narrower": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[1/2] аббревиатур: {len(groups)} (из {len(pairs):,} кандидатов), "
          f"{time.time() - t0:.0f}s -> {OUT_SYN.name}")
    for g in groups[:15]:
        print("     ", g)

    lem_cnt: Counter[str] = Counter()
    lem_cls: dict[str, Counter] = defaultdict(Counter)
    seen_pairs: set[tuple[str, str]] = set()
    for chunk in read_texts(tru, ["product_name", "okpd2_code"]):
        chunk = chunk.dropna().drop_duplicates()
        for text, code in zip(chunk["product_name"], chunk["okpd2_code"]):
            cls = str(code).strip()[:OKPD_LEVEL]
            key = (text, cls)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            for lem in content_lemmas(text):
                lem_cnt[lem] += 1
                lem_cls[lem][cls] += 1
    table = {}
    for lem, n in lem_cnt.items():
        if n < MIN_LEMMA:
            continue
        top = [[c, round(k / n, 3)] for c, k in lem_cls[lem].most_common(TOP_OKPD) if k / n >= MIN_P]
        if top:
            table[lem] = top
    OUT_OKPD.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    print(f"[2/2] лемм с ОКПД2: {len(table):,}, {time.time() - t0:.0f}s -> {OUT_OKPD.name}")


if __name__ == "__main__":
    main()
