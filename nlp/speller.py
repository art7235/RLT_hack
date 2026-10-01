"""Исправление поисковых запросов: раскладка, опечатки, бренды, лемматизация.

Публичный API (совместим с первой версией):
    fix_layout(word)     -> str
    correct_word(word)   -> str
    lemmatize(word)      -> str
    correct_query(text)  -> dict   (original, corrected, was_corrected, changes, tokens, lemmas)

Дополнительно:
    correct_tokens(text) -> (tokens, changes_with_reason)   — для объяснимости
    pos_tag(word)        -> str    — часть речи pymorphy3 (NOUN, ADJF, ...)

Работает полностью офлайн: словарь data/nlp/vocab.json + pymorphy3 + rapidfuzz.

Что изменено относительно v1 (см. README_speller.md, раздел «Ревью»):
  * раскладка исправляется ДО токенизации: клавиши ; , . [ ] ' ` < > — это
    русские ж б ю х ъ э ё Б Ю, токенизатор их выкидывал («ve;crjq» → «ve crjq»);
  * скоринг — расстояние Дамерау–Левенштейна (перестановки = 1 правка)
    + бонус за log-частоту вместо «окна ±12 по fuzz.ratio, а дальше самое частое»,
    из-за которого «ноутбк» → «ноутбука», «ремнт» → «ремонту»;
  * корректные слова русского языка, которых нет в словаре закупок,
    больше не «исправляются» («снегоуборка» → «снегоуборщика» — было);
  * бренды кириллицей → латиница («леново» → lenovo, «рз» → hp).
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

from rapidfuzz import process
from rapidfuzz.distance import DamerauLevenshtein

from nlp.tokenize import normalize, tokenize

ROOT = Path(__file__).resolve().parents[1]
VOCAB_PATH = ROOT / "data" / "nlp" / "vocab.json"

MIN_CORRECT_LEN = 4        # слова короче не исправляем
MIN_CAND_FREQ = 3          # кандидат-исправление должен встречаться хотя бы столько раз
FREQ_BONUS = 0.35          # вес log10(частоты) в стоимости кандидата
MAX_COST = 1.0             # кандидат принимается, если distance - bonus <= MAX_COST


def max_edits(n: int) -> int:
    """Сколько правок допускаем в слове длины n."""
    if n <= 5:
        return 1
    if n <= 9:
        return 2
    return 3


_HAS_DIGIT = re.compile(r"\d")
_CYR = re.compile(r"[а-яё]")
_LAT = re.compile(r"[a-z]")

# Полная раскладка QWERTY -> ЙЦУКЕН, включая клавиши-знаки и Shift-варианты.
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`{}:\"<>~"
_RU = "йцукенгшщзхъфывапролджэячсмитьбюёхъжэбюё"
_EN2RU = str.maketrans(_EN, _RU)
_RU2EN = str.maketrans(_RU[:33], _EN[:33])
# «слово» в латинской раскладке: буквы + клавиши, которые на ЙЦУКЕН дают буквы
_LAYOUT_WORD = re.compile(r"[a-z;',.\[\]`{}:\"<>~]+")

# Бренды и марки, которые пишут кириллицей. Ключ — как пишут, значение — как в данных.
BRANDS: dict[str, str] = {
    "леново": "lenovo", "ленова": "lenovo", "самсунг": "samsung", "самсуг": "samsung",
    "кэнон": "canon", "канон": "canon", "кенон": "canon", "ксерокс": "xerox",
    "киосера": "kyocera", "кьюсера": "kyocera", "куосера": "kyocera", "киоцера": "kyocera",
    "эпсон": "epson", "епсон": "epson", "бразер": "brother", "бротхер": "brother",
    "асус": "asus", "асер": "acer", "эйсер": "acer", "делл": "dell",
    "эйчпи": "hp", "хп": "hp", "хьюлетт": "hp", "пантум": "pantum", "рикох": "ricoh",
    "рико": "ricoh", "шарп": "sharp", "тошиба": "toshiba", "филипс": "philips",
    "бош": "bosch", "макита": "makita", "сименс": "siemens", "шнайдер": "schneider",
    "логитек": "logitech", "дефендер": "defender", "сони": "sony", "рз": "hp",
    "элджи": "lg", "хуавей": "huawei", "хуавэй": "huawei", "сяоми": "xiaomi",
    "ксиоми": "xiaomi", "аквариус": "aquarius", "айкон": "aicon", "кайно": "kaino",
    "циско": "cisco", "микротик": "mikrotik", "тплинк": "tp-link", "длинк": "d-link",
    "интел": "intel", "амд": "amd", "виндовс": "windows", "майкрософт": "microsoft",
    "касперский": "kaspersky", "айфон": "iphone", "эппл": "apple", "эпл": "apple",
}

# ---------------------------------------------------------------- ленивое состояние
_VOCAB: dict[str, int] | None = None
_BUCKETS: dict[tuple[str, int], list[str]] | None = None   # (первая буква, длина) -> слова
_BY_LEN: dict[int, list[str]] | None = None                # длина -> слова
_MORPH = None


def _load_vocab() -> dict[str, int]:
    global _VOCAB, _BUCKETS, _BY_LEN
    if _VOCAB is None:
        if not VOCAB_PATH.exists():
            raise FileNotFoundError(
                f"Нет словаря {VOCAB_PATH}. Сначала: python scripts/build_vocab.py"
            )
        with VOCAB_PATH.open(encoding="utf-8") as f:
            vocab = json.load(f)
        buckets: dict[tuple[str, int], list[str]] = {}
        by_len: dict[int, list[str]] = {}
        for w, c in vocab.items():
            # в кандидаты на исправление не берём слова с цифрами и редкий мусор
            if c < MIN_CAND_FREQ or _HAS_DIGIT.search(w):
                continue
            n = len(w)
            buckets.setdefault((w[0], n), []).append(w)
            by_len.setdefault(n, []).append(w)
        _VOCAB, _BUCKETS, _BY_LEN = vocab, buckets, by_len
    return _VOCAB


def _get_morph():
    global _MORPH
    if _MORPH is None:
        import pymorphy3
        _MORPH = pymorphy3.MorphAnalyzer()
    return _MORPH


def _is_numericish(word: str) -> bool:
    """Числа и токены с цифрами: «а4», «аи-92», «3х1.5»."""
    return bool(_HAS_DIGIT.search(word))


@lru_cache(maxsize=200_000)
def _is_known_russian(word: str) -> bool:
    """Слово есть в словаре OpenCorpora (а не угадано по суффиксу)."""
    if not _CYR.search(word):
        return False
    return _get_morph().word_is_known(word)


def _freq(word: str) -> int:
    return _load_vocab().get(word, 0)


# ---------------------------------------------------------------- раскладка
def _layout_en2ru(chunk: str) -> str | None:
    """Пробует прочитать латинский кусок как набранный в английской раскладке."""
    vocab = _load_vocab()
    low = chunk.lower()
    if low in vocab or low in BRANDS.values():      # hp, canon, usb — не трогаем
        return None
    ru = low.translate(_EN2RU)
    if ru == low or _LAT.search(ru):
        return None
    if ru in vocab or _is_known_russian(ru):
        return ru
    # опечатка + раскладка: «ghbynthf» -> «принтера» не нужен, а «ghbyntth» -> «принтеер»
    if len(ru) >= 5:
        fixed = correct_word(ru)
        if fixed != ru and DamerauLevenshtein.distance(ru, fixed) <= 1:
            return fixed
    return None


def _layout_ru2en(word: str) -> str | None:
    """«рз» -> hp, «сфтщт» -> canon: латинский бренд, набранный в русской раскладке."""
    vocab = _load_vocab()
    if not word.isalpha() or not _CYR.search(word) or word in vocab or _is_known_russian(word):
        return None
    en = word.translate(_RU2EN)
    if en != word and re.fullmatch(r"[a-z]+", en) and vocab.get(en, 0) >= 20:
        return en
    return None


def fix_layout(word: str) -> str:
    """Исправляет раскладку одного слова (в обе стороны). Слова из словаря не трогает."""
    if not word:
        return word
    if _CYR.search(word):
        return _layout_ru2en(word) or word
    if not _LAYOUT_WORD.fullmatch(word.lower()):
        return word
    return _layout_en2ru(word) or word


def _fix_layout_text(text: str) -> tuple[str, list[dict]]:
    """Исправляет раскладку в сыром тексте ДО токенизации (знаки-клавиши — это буквы)."""
    changes: list[dict] = []

    def repl(m: re.Match) -> str:
        chunk = m.group(0)
        core = chunk.strip(".,;:")
        if len(core) < 2 or not _LAT.search(core.lower()):
            return chunk
        ru = _layout_en2ru(chunk.strip(".,;:"))
        if ru is None:
            # знаки по краям могли быть обычной пунктуацией: «ghbynth,» -> «принтер,»
            core = chunk.strip(".,;:")
            ru = _layout_en2ru(core) if core != chunk else None
            if ru is None:
                return chunk
        changes.append({"from": chunk.lower(), "to": ru, "reason": "раскладка"})
        return " " + ru + " "

    # не трогаем куски, приклеенные к цифрам/кириллице: «a4», «hp-laserjet» и т.п.
    out = re.sub(r"(?<![0-9а-яё])[A-Za-z;',.\[\]`{}:\"<>~]+(?![0-9а-яё])", repl, text)
    return out, changes


# латинские буквы, похожие на русские (их часто путают в «а4», «аи-92», «ввгнг»)
_HOMO = str.maketrans("aeopcxykmthb", "аеорсхукмтнв")


def _fix_mixed(tok: str) -> tuple[str, str] | None:
    """Токены с цифрами/смесью алфавитов: «a4»(лат.) -> «а4», «f4» -> «а4» (раскладка)."""
    vocab = _load_vocab()
    if not _LAT.search(tok) or not (_HAS_DIGIT.search(tok) or _CYR.search(tok)):
        return None
    f = vocab.get(tok, 0)
    homo = tok.translate(_HOMO)
    if homo != tok and not _LAT.search(homo) and vocab.get(homo, 0) > f:
        return homo, "латинские буквы вместо русских"
    lay = "".join(ch.translate(_EN2RU) if _LAT.match(ch) else ch for ch in tok)
    if lay != tok and vocab.get(lay, 0) > 20 * max(f, 1):
        return lay, "раскладка"
    return None


def canon_token(tok: str) -> str:
    """Канонизация без исправления опечаток — для индексации текстов датасета
    (чтобы «a4» латиницей в данных и «а4» в запросе совпали)."""
    if _LAT.search(tok) and _CYR.search(tok) or (_LAT.search(tok) and _HAS_DIGIT.search(tok)):
        homo = tok.translate(_HOMO)
        if not _LAT.search(homo):
            return homo
    return tok


# ---------------------------------------------------------------- опечатки
def _best(word: str, candidates: list[str], vocab: dict[str, int]) -> tuple[str | None, float]:
    """Лучший кандидат по стоимости: DL-расстояние - FREQ_BONUS*log10(частота)."""
    if not candidates:
        return None, math.inf
    k = max_edits(len(word))
    matches = process.extract(
        word, candidates, scorer=DamerauLevenshtein.distance,
        score_cutoff=k, limit=50,
    )
    best, best_cost = None, math.inf
    for cand, dist, _ in matches:
        if dist == 0:
            return cand, -1.0
        cost = dist - FREQ_BONUS * math.log10(vocab.get(cand, 1))
        # при равной стоимости — короче правка, потом частота
        if cost < best_cost:
            best, best_cost = cand, cost
    # редкий кандидат при максимальной правке — слишком рискованно
    if best is not None and best_cost <= MAX_COST:
        return best, best_cost
    return None, math.inf


@lru_cache(maxsize=200_000)
def correct_word(word: str) -> str:
    """Исправляет опечатку в одном слове (нижний регистр). Слова из словаря не трогает."""
    vocab = _load_vocab()
    if word in BRANDS:
        return BRANDS[word]
    if not word or word in vocab:
        return word
    if _is_numericish(word) or len(word) < MIN_CORRECT_LEN:
        return word
    # нормальное русское слово, которого просто нет в закупках, — не трогаем
    if _is_known_russian(word):
        return word

    n, first = len(word), word[0]
    k = max_edits(n)

    # 1) та же первая буква (самый частый случай опечатки)
    cands: list[str] = []
    for L in range(n - k, n + k + 1):
        cands.extend(_BUCKETS.get((first, L), ()))           # type: ignore[union-attr]
    hit, cost = _best(word, cands, vocab)

    # 2) ошибка в первой букве: все слова длины ±1, принимаем только уверенный вариант
    if hit is None or cost > 0:
        cands = []
        for L in range(n - 1, n + 2):
            cands.extend(_BY_LEN.get(L, ()))                 # type: ignore[union-attr]
        hit2, cost2 = _best(word, cands, vocab)
        if hit2 is not None and cost2 < cost - 0.5:
            hit, cost = hit2, cost2

    return hit or word


# ---------------------------------------------------------------- лемматизация
@lru_cache(maxsize=200_000)
def lemmatize(word: str) -> str:
    if not word or _is_numericish(word) or not _CYR.search(word):
        return word          # латиницу и токены с цифрами не лемматизируем
    parses = _get_morph().parse(word)
    if not _is_known_russian(word):
        # для незнакомых слов pymorphy угадывает по окончанию: «клининг» -> «клининга»,
        # «скуд» -> «скуда». Если среди разборов есть само слово как начальная форма
        # или угаданная лемма в закупках встречается реже самого слова — оставляем слово.
        if any(p.normal_form == word for p in parses):
            return word
        vocab = _load_vocab()
        if len(word) <= 5 and vocab.get(parses[0].normal_form, 0) < vocab.get(word, 0):
            return word         # короткие аббревиатуры: скуд, соуэ, жбо
    return parses[0].normal_form


_ADJ_POS = {"ADJF", "PRTF"}


def _agree(adj, noun) -> bool:
    """Согласование прилагательного с существительным по числу, падежу (и роду в ед. ч.)."""
    a, n = adj.tag, noun.tag
    if a.case != n.case or a.number != n.number:
        return False
    return a.number == "plur" or a.gender is None or n.gender is None or a.gender == n.gender


def lemmatize_tokens(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Леммы и части речи с учётом контекста.

    pymorphy3 разбирает слово изолированно: «горячего питания» -> «горячее» (сущ.),
    «столовая посуда» -> «столовая» (сущ.). Если у слова есть разбор-прилагательное,
    согласованное со следующим существительным, берём его.
    """
    lemmas = [lemmatize(t) for t in tokens]
    tags = [pos_tag(t) for t in tokens]
    morph = None
    for i in range(len(tokens) - 1):
        if tags[i] in ("NUM", "LATN") or not _is_known_russian(tokens[i]):
            continue
        morph = morph or _get_morph()
        parses = morph.parse(tokens[i])
        if parses[0].tag.POS in _ADJ_POS:
            continue
        nxt = [p for p in morph.parse(tokens[i + 1]) if p.tag.POS == "NOUN"]
        if not nxt:
            continue
        for p in parses:
            if p.tag.POS in _ADJ_POS and any(_agree(p, n) for n in nxt):
                lemmas[i], tags[i] = p.normal_form, str(p.tag.POS)
                break
    return lemmas, tags


@lru_cache(maxsize=200_000)
def pos_tag(word: str) -> str:
    """Часть речи: NOUN, ADJF, PRTF, PREP, CONJ, NUMR ... ; LATN для латиницы, NUM для цифр."""
    if _is_numericish(word):
        return "NUM"
    if not _CYR.search(word):
        return "LATN"
    return str(_get_morph().parse(word)[0].tag.POS or "UNKN")


# ---------------------------------------------------------------- запрос целиком
def correct_tokens(text: str) -> tuple[list[str], list[dict]]:
    """Токены исправленного запроса + список правок с причиной (раскладка/опечатка/бренд)."""
    _load_vocab()
    text = normalize(text)
    text, changes = _fix_layout_text(text)

    tokens_out: list[str] = []
    for tok in tokenize(text):
        fixed, reason = tok, ""
        ru2en = _layout_ru2en(tok)
        mixed = _fix_mixed(tok)
        if ru2en:
            fixed, reason = ru2en, "раскладка"
        elif mixed:
            fixed, reason = mixed
        else:
            fixed = correct_word(tok)
            if fixed != tok:
                reason = "бренд" if tok in BRANDS else "опечатка"
        if fixed != tok:
            changes.append({"from": tok, "to": fixed, "reason": reason})
        tokens_out.extend(fixed.split())
    return tokens_out, changes


def correct_query(text: str) -> dict:
    tokens_out, changes = correct_tokens(text)
    lemmas, _ = lemmatize_tokens(tokens_out)
    return {
        "original": text,
        "corrected": " ".join(tokens_out),
        "was_corrected": bool(changes),
        "changes": [{"from": c["from"], "to": c["to"]} for c in changes],
        "tokens": tokens_out,
        "lemmas": lemmas,
    }


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "пастовка бумаги а4"
    print(json.dumps(correct_query(q), ensure_ascii=False, indent=2))
