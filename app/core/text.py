"""Обработка текста для индекса и запроса.

Индексатор и поиск обязаны использовать одну и ту же функцию doc_terms(),
иначе леммы запроса не совпадут с леммами индекса.
Если команда NLP положила data/nlp/stopwords.txt — он подхватывается автоматически.
"""
import re
from functools import lru_cache

from app.config import NLP_DICT_DIR

# тот же токенизатор, что в nlp/tokenize.py у команды NLP
_TOKEN = re.compile(r"[а-яa-z0-9]+(?:[-./][а-яa-z0-9]+)*")


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


# --- шум в названиях закупок -------------------------------------------------
_NOISE = [
    re.compile(r"\(?\s*п\.?\s*\d+\s*ч\.?\s*\d+\s*ст\.?\s*\d+[^)]*\)?", re.I),  # (п.33 ч.1 ст.93 ...)
    re.compile(r"\b\d{2,3}\s*-\s*фз\b", re.I),
    re.compile(r"федеральн\w*\s+закон\w*", re.I),
    re.compile(r"№\s*[\w/-]+", re.I),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),                  # даты
    re.compile(r"\b(?:19|20)\d{2}\s*(?:г\.?|год\w*)?", re.I),      # годы
]


def clean(text: str) -> str:
    text = text or ""
    for rx in _NOISE:
        text = rx.sub(" ", text)
    return text


_BASE_STOPWORDS = """
а и в во на по с со к ко о об от до из за для при под над без у же ли не ни или либо то это
как что чтобы также а также так том той тот та те их его ее их ним них
поставка оказание услуга выполнение работа закупка приобретение обеспечение нужда
осуществление проведение организация предоставление право заключение договор контракт
государственный муниципальный бюджетный казенный учреждение федеральный закон
санкт-петербург спб г город район здание помещение адрес
гбоу гбдоу гбу гку гбуз гоу гбпоу гбудо фгбу спб гуп сзао оао зао ооо ип
школа гимназия лицей детский сад средний общеобразовательный дошкольный образовательный
январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь
год квартал месяц период полугодие
тип шт штука единица количество объем
адмиралтейский василеостровский выборгский калининский кировский колпинский
красногвардейский красносельский кронштадтский курортный московский невский
петроградский петродворцовый приморский пушкинский фрунзенский центральный
""".split()


@lru_cache(maxsize=1)
def stopwords() -> frozenset[str]:
    words = set(_BASE_STOPWORDS)
    f = NLP_DICT_DIR / "stopwords.txt"
    if f.exists():
        words |= {w.strip() for w in f.read_text(encoding="utf-8").splitlines() if w.strip()}
    return frozenset(words)


_morph = None


def _get_morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph


@lru_cache(maxsize=500_000)
def lemma(word: str) -> str:
    # токены с цифрами и латиница — как есть (а4, аи-92, hp)
    if any(c.isdigit() for c in word) or not re.search("[а-я]", word):
        return word
    return _get_morph().parse(word)[0].normal_form.replace("ё", "е")


def doc_terms(text: str) -> list[str]:
    """Текст -> леммы без стоп-слов. Используется и для индекса, и для запроса."""
    sw = stopwords()
    out = []
    for tok in tokenize(clean(text)):
        if len(tok) < 2 and not tok.isdigit():
            continue
        lm = lemma(tok)
        if lm in sw or tok in sw:
            continue
        out.append(lm)
    return out


_INTENT = {"товар": "goods", "услуга": "services", "работа": "services"}


def process_query(raw: str) -> dict:
    """Обработка запроса модулем NLP команды (nlp/query.py: опечатки, ключевые слова, синонимы).
    Если модуля нет или он упал — встроенная обработка."""
    try:
        from nlp.query import process_query as nlp_process  # type: ignore
        res = nlp_process(raw)
        res["engine"] = "nlp"
        res["intent"] = _INTENT.get(res.get("procurement_type") or "")
        res["expansions"] = {e["matched"]: [] for e in res.get("synonyms", [])}
        for e in res.get("synonyms", []):
            res["expansions"][e["matched"]].append(e["term"])
        return res
    except Exception:
        pass
    lemmas = doc_terms(raw)
    return {
        "original": raw,
        "corrected": raw,
        "was_corrected": False,
        "changes": [],
        "lemmas": lemmas,
        "expansions": {},
        "explain": [],
        "search_terms": {t: 1.0 for t in lemmas},
        "intent": None,
        "engine": "fallback",
    }
