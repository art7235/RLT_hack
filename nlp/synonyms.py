"""Синонимы и расширение запроса для поиска по закупкам."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from nlp.speller import lemmatize_tokens
from nlp.tokenize import tokenize

ROOT = Path(__file__).resolve().parents[1]
CURATED = ROOT / "data" / "nlp" / "synonyms_curated.json"
MINED = ROOT / "data" / "nlp" / "synonyms_mined.json"

MAX_NGRAM = 6
W_SYNONYM = 0.85
W_MINED = 0.75
W_NARROWER = 0.5
W_BROADER = 0.4


Phrase = tuple[str, ...]


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    phrase: Phrase
    text: str


def _lem_phrase(text: str) -> Phrase:
    return tuple(lemmatize_tokens(tokenize(text))[0])


class _Index:
    def __init__(self) -> None:
        self.groups: list[list[tuple[Phrase, str, str]]] = []
        self.by_phrase: dict[Phrase, list[int]] = {}
        self.narrower: dict[Phrase, list[tuple[Phrase, str]]] = {}
        self.broader: dict[Phrase, list[tuple[Phrase, str]]] = {}
        self.display: dict[Phrase, str] = {}

    def add_group(self, terms: list[str], source: str) -> None:
        items = []
        for t in terms:
            ph = _lem_phrase(t)
            if ph:
                items.append((ph, t, source))
                self.display.setdefault(ph, t)
        if len(items) < 2:
            return
        gid = len(self.groups)
        self.groups.append(items)
        for ph, _, _ in items:
            self.by_phrase.setdefault(ph, []).append(gid)

    def add_narrower(self, broad: str, narrow: list[str]) -> None:
        bp = _lem_phrase(broad)
        self.display.setdefault(bp, broad)
        lst = self.narrower.setdefault(bp, [])
        for n in narrow:
            ph = _lem_phrase(n)
            if ph and ph not in [x[0] for x in lst]:
                lst.append((ph, n))
                self.display.setdefault(ph, n)
                br = self.broader.setdefault(ph, [])
                if bp not in [x[0] for x in br]:
                    br.append((bp, broad))

    def known(self, ph: Phrase) -> bool:
        return ph in self.by_phrase or ph in self.narrower or ph in self.broader


def _load_file(idx: _Index, path: Path, source: str) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    for group in data.get("synonyms", []):
        idx.add_group(group, source)
    for broad, narrow in data.get("narrower", {}).items():
        idx.add_narrower(broad, narrow)
    for broad in data.get("narrower", {}):
        bp = _lem_phrase(broad)
        if bp not in idx.by_phrase:
            idx.by_phrase.setdefault(bp, [])


@lru_cache(maxsize=1)
def get_index() -> _Index:
    idx = _Index()
    _load_file(idx, CURATED, "словарь")
    _load_file(idx, MINED, "датасет")
    return idx


def find_matches(lemmas: list[str]) -> list[Match]:
    """Жадный поиск фраз словаря в последовательности лемм (длинные — первыми)."""
    idx = get_index()
    used = [False] * len(lemmas)
    found: list[Match] = []
    for n in range(min(MAX_NGRAM, len(lemmas)), 0, -1):
        for i in range(0, len(lemmas) - n + 1):
            if any(used[i:i + n]):
                continue
            ph = tuple(lemmas[i:i + n])
            if idx.known(ph):
                found.append(Match(i, i + n, ph, idx.display.get(ph, " ".join(ph))))
                for j in range(i, i + n):
                    used[j] = True
    return sorted(found, key=lambda m: m.start)


def expand(lemmas: list[str], max_per_match: int = 8) -> list[dict]:
    """Варианты расширения запроса."""
    idx = get_index()
    out: list[dict] = []
    seen: set[Phrase] = set()
    for m in find_matches(lemmas):
        seen.add(m.phrase)
    for m in find_matches(lemmas):
        added = 0
        for gid in idx.by_phrase.get(m.phrase, []):
            for ph, text, source in idx.groups[gid]:
                if ph in seen or added >= max_per_match:
                    continue
                seen.add(ph)
                added += 1
                out.append({
                    "matched": m.text, "matched_lemmas": list(m.phrase),
                    "term": text, "lemmas": list(ph), "kind": "synonym", "source": source,
                    "weight": W_SYNONYM if source == "словарь" else W_MINED,
                })
        for ph, text in idx.narrower.get(m.phrase, [])[:max_per_match]:
            if ph in seen:
                continue
            seen.add(ph)
            out.append({
                "matched": m.text, "matched_lemmas": list(m.phrase),
                "term": text, "lemmas": list(ph), "kind": "narrower", "source": "словарь", "weight": W_NARROWER,
            })
        for ph, text in idx.broader.get(m.phrase, [])[:max_per_match]:
            if ph in seen:
                continue
            seen.add(ph)
            out.append({
                "matched": m.text, "matched_lemmas": list(m.phrase),
                "term": text, "lemmas": list(ph), "kind": "broader",
                "source": "словарь", "weight": W_BROADER,
            })
    return out
