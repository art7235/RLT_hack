"""Единая точка входа: название закупки -> поисковый запрос для подбора поставщиков."""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

from nlp.keywords import CUSTOMER, PROCEDURE, extract_keywords, idf
from nlp.speller import correct_tokens, lemmatize_tokens
from nlp.synonyms import expand, find_matches

ROOT = Path(__file__).resolve().parents[1]
LEMMA_OKPD2 = ROOT / "data" / "nlp" / "lemma_okpd2.json"

_SKIP_IN_EXPANSION = {"для", "и", "в", "на", "с", "по", "из", "от", "к", "о", "без"}


@lru_cache(maxsize=1)
def _lemma_okpd2() -> dict[str, list[list]]:
    if not LEMMA_OKPD2.exists():
        return {}
    with LEMMA_OKPD2.open(encoding="utf-8") as f:
        return json.load(f)


def okpd2_hints(weighted_lemmas: dict[str, float], top: int = 5) -> list[dict]:
    """Вероятные классы ОКПД2: сумма вес_леммы × P(класс | лемма)."""
    table = _lemma_okpd2()
    if not table:
        return []
    score: dict[str, float] = {}
    why: dict[str, list[str]] = {}
    for lem, w in weighted_lemmas.items():
        for code, p in table.get(lem, []):
            score[code] = score.get(code, 0.0) + w * p
            why.setdefault(code, []).append(lem)
    total = sum(score.values()) or 1.0
    best = sorted(score.items(), key=lambda kv: -kv[1])[:top]
    return [{"code": c, "score": round(s / total, 3), "by": why[c][:5]} for c, s in best]


def process_query(text: str) -> dict:
    t0 = time.perf_counter()
    tokens, changes = correct_tokens(text)
    lemmas, tags = lemmatize_tokens(tokens)

    kw = extract_keywords(tokens, lemmas, tags)
    start, end = kw["subject_span"]
    subject_lemmas = lemmas[start:end]

    expansions = expand(subject_lemmas)

    terms: dict[str, float] = {}
    for k in kw["keywords"]:
        terms[k["lemma"]] = max(terms.get(k["lemma"], 0.0), k["weight"])
    for m in find_matches(subject_lemmas):
        for lem in m.phrase:
            if lem not in _SKIP_IN_EXPANSION and lem not in PROCEDURE and lem not in CUSTOMER:
                terms.setdefault(lem, round(idf(lem), 3))
    expanded: dict[str, float] = {}
    for e in expansions:
        matched_w = max((terms.get(l, 0.0) for l in e["matched_lemmas"]), default=0.0) \
            or max(terms.values(), default=0.5)
        for lem in e["lemmas"]:
            if lem in terms or lem in _SKIP_IN_EXPANSION or lem in PROCEDURE or lem in CUSTOMER:
                continue
            w = round(e["weight"] * matched_w * (0.5 + 0.5 * idf(lem)), 3)
            expanded[lem] = max(expanded.get(lem, 0.0), w)
    terms.update(expanded)
    terms = dict(sorted(terms.items(), key=lambda kv: -kv[1]))

    explain: list[str] = []
    for c in changes:
        explain.append(f"исправлено «{c['from']}» → «{c['to']}» ({c['reason']})")
    if kw["procurement_type"]:
        explain.append(f"тип закупки: {kw['procurement_type']} — {kw['type_reason']}")
    for d in kw["dropped"]:
        explain.append(f"не учитываем «{d['text']}» — {d['reason']}")
    core = [k["lemma"] for k in kw["keywords"] if k["role"] in {"head", "object"}]
    if core:
        explain.append("предмет закупки: " + ", ".join(core))
    syn_by_match: dict[str, list[str]] = {}
    for e in expansions:
        syn_by_match.setdefault(e["matched"], []).append(e["term"])
    for m, lst in syn_by_match.items():
        explain.append(f"«{m}» также ищем как: " + ", ".join(lst[:6]))

    return {
        "original": text,
        "corrected": " ".join(tokens),
        "was_corrected": bool(changes),
        "changes": changes,
        "tokens": tokens,
        "lemmas": lemmas,
        "procurement_type": kw["procurement_type"],
        "keywords": kw["keywords"],
        "phrases": kw["phrases"],
        "synonyms": expansions,
        "search_terms": terms,
        "okpd2_hints": okpd2_hints(terms),
        "explain": explain,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Поставка картриджы для мфу для нужд ГБОУ школа № 548"
    print(json.dumps(process_query(q), ensure_ascii=False, indent=2))
