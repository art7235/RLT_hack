"""Ключевые слова из названия закупки."""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

from nlp.speller import pos_tag

ROOT = Path(__file__).resolve().parents[1]
LEMMA_FREQ_PATH = ROOT / "data" / "nlp" / "lemma_freq.json"
N_DOCS = 1_500_000

PROCEDURE = {
    "поставка": "товар", "приобретение": "товар", "закупка": "товар", "товар": "товар",
    "продукция": None, "выполнение": "работа", "работа": "работа",
    "оказание": "услуга", "услуга": "услуга", "предоставление": "услуга",
    "осуществление": None, "проведение": None, "организация": None, "обеспечение": None,
    "право": None, "заключение": None, "контракт": None, "договор": None,
    "государственный": None, "муниципальный": None, "нужда": None, "лот": None,
    "соответствие": None, "согласно": None, "техническое задание": None,
    "эквивалент": None, "необходимость": None, "целях": None, "цель": None,
    "использование": None, "реализация": None, "заказчик": None, "объект": None, "закупаемый": None, "наименование": None,
    "количество": None, "штука": None, "шт": None, "ед": None, "единица": None,
}

ACTION_TYPE = {
    "ремонт": "работа", "строительство": "работа", "реконструкция": "работа",
    "монтаж": "работа", "демонтаж": "работа", "установка": "работа", "замена": "работа",
    "устройство": "работа", "модернизация": "работа", "благоустройство": "работа",
    "изготовление": "работа", "прокладка": "работа", "покраска": "работа",
    "окраска": "работа", "восстановление": "работа", "реставрация": "работа",
    "проектирование": "работа", "капремонт": "работа", "переоборудование": "работа",
    "обслуживание": "услуга", "техобслуживание": "услуга", "уборка": "услуга",
    "вывоз": "услуга", "охрана": "услуга", "перевозка": "услуга", "доставка": "услуга",
    "обучение": "услуга", "стирка": "услуга", "аренда": "услуга", "страхование": "услуга",
    "дезинсекция": "услуга", "дератизация": "услуга", "дезинфекция": "услуга",
    "заправка": "услуга", "перезарядка": "услуга", "поверка": "услуга",
    "испытание": "услуга", "исследование": "услуга", "экспертиза": "услуга",
    "оценка": "услуга", "сопровождение": "услуга", "содержание": "услуга",
    "утилизация": "услуга", "очистка": "услуга", "техосмотр": "услуга",
    "медосмотр": "услуга", "осмотр": "услуга",
    "размещение": "услуга", "транспортировка": "услуга", "диагностика": "услуга",
    "разработка": "услуга", "подготовка": "услуга", "печать": "услуга",
    "клининг": "услуга", "техобслуживание": "услуга", "вывоз": "услуга",
    "изготавливание": "работа", "пошив": "работа", "сборка": "работа",
}

WEAK_TYPE = {"организация": "услуга", "проведение": "услуга", "осуществление": "услуга",
             "питание": "услуга"}

CUSTOMER = {
    "гбоу", "гбдоу", "гбу", "гбуз", "гоу", "гуп", "спбгуп", "гку", "гбпоу", "гбоудо",
    "гбудо", "гбноу", "гбсу", "гау", "фгбу", "фгбоу", "фгуп", "мку", "муп", "оао", "ооо",
    "ао", "пао", "спб", "санкт-петербург", "санкт-петербургский", "петербург",
    "ленинградский", "ленобласть", "россия", "российский", "федерация", "город", "г",
    "район", "администрация", "комитет", "учреждение", "бюджетный", "казённый",
    "казенный", "автономный", "общеобразовательный", "дошкольный", "образовательный",
    "школа", "лицей", "гимназия", "сад", "детский", "ясли", "поликлиника", "больница",
    "стационар", "интернат", "колледж", "центр", "дом", "филиал", "отделение",
    "адмиралтейский", "василеостровский", "выборгский", "калининский", "кировский",
    "колпинский", "красногвардейский", "красносельский", "кронштадтский", "курортный",
    "московский", "невский", "петроградский", "петродворцовый", "приморский",
    "пушкинский", "фрунзенский", "центральный", "адрес", "ул", "улица", "пр", "пр-т",
    "проспект", "пер", "переулок", "наб", "набережная", "ш", "шоссе", "бул", "бульвар",
    "пл", "площадь", "д", "корп", "корпус", "к", "лит", "литера", "лита", "стр",
    "строение", "кв", "пом", "помещ", "п", "пос", "посёлок", "поселок", "дер", "м",
    "номер", "год", "гг", "квартал", "полугодие", "месяц", "период", "январь", "февраль",
    "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
    "декабрь", "срок", "течение", "этап", "часть", "обучаться", "обучающийся",
    "воспитанник", "учащийся", "сотрудник", "работник", "пациент", "получатель",
}

_CUT_MARKERS: list[tuple[str, ...]] = [
    ("для", "нужда"), ("для", "обеспечение", "нужда"), ("для", "государственный", "нужда"),
    ("в", "целях"), ("в", "цель"), ("по", "адрес"), ("по", "адресу"), ("в", "соответствие"),
    ("согласно",), ("на", "территория"), ("в", "рамка"), ("на", "период"),
    ("в", "течение"), ("нужда",),
]

_KEEP_POS = {"NOUN", "ADJF", "ADJS", "PRTF", "LATN", "NUM", "UNKN", "COMP"}
_YEAR = re.compile(r"^(19|20)\d\d(-(19|20)?\d\d)?$")
_PURE_NUM = re.compile(r"^\d+([.,]\d+)?$")
UNITS = {"мм", "см", "м", "мл", "л", "г", "гр", "кг", "т", "вт", "квт", "в", "а", "гб", "тб",
         "мб", "ггц", "мгц", "дюйм", "мп", "лм", "ач", "мач", "квм", "м2", "м3", "куб",
         "г/м2", "кг/м3", "м/с", "шт", "уп", "пар", "лист", "листов", "рулон"}

ROLE_FACTOR = {"object": 1.3, "head": 1.3, "noun": 1.0, "brand": 1.0,
               "spec": 0.9, "action": 0.8, "purpose": 0.7, "attr": 0.7}


@lru_cache(maxsize=1)
def _lemma_freq() -> dict[str, int]:
    if not LEMMA_FREQ_PATH.exists():
        return {}
    with LEMMA_FREQ_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def idf(lemma: str) -> float:
    """Информативность леммы в [0.15, 1]: редкое слово — ближе к 1."""
    f = _lemma_freq().get(lemma, 0)
    if not _lemma_freq():
        return 1.0
    val = math.log((N_DOCS + 1) / (f + 1)) / math.log(N_DOCS + 1)
    return round(max(0.15, min(1.0, val)), 3)


def _is_stop(lemma: str, tok: str) -> bool:
    return (lemma in PROCEDURE or lemma in CUSTOMER or tok in CUSTOMER
            or bool(_YEAR.match(tok)) or tok in {"№", "n"})


def _find_cut(lemmas: list[str], tokens: list[str]) -> int:
    """Индекс, с которого начинается «хвост заказчика» (или len, если его нет)."""
    def has_content(upto: int) -> bool:
        return any(not _is_stop(lemmas[j], tokens[j]) and pos_tag(tokens[j]) in _KEEP_POS
                   and lemmas[j] not in ACTION_TYPE for j in range(upto))

    n = len(lemmas)
    for i in range(n):
        for mk in _CUT_MARKERS:
            if tuple(lemmas[i:i + len(mk)]) == mk and has_content(i):
                return i
        if lemmas[i] == "для" and i + 1 < n and lemmas[i + 1] in CUSTOMER and has_content(i):
            return i
        if lemmas[i] in {"в", "на"} and i + 1 < n and _YEAR.match(tokens[i + 1]) and has_content(i):
            return i
    return n


def extract_keywords(tokens: list[str], lemmas: list[str],
                     tags: list[str] | None = None) -> dict:
    """Ключевые слова названия закупки."""
    n = len(tokens)
    tags = tags or [pos_tag(t) for t in tokens]
    cut = _find_cut(lemmas, tokens)
    dropped: list[dict] = []
    if cut < n:
        dropped.append({"text": " ".join(tokens[cut:]), "reason": "заказчик/адрес/срок"})

    ptype, reason = None, ""
    for i, lem in enumerate(lemmas[:cut]):
        t = PROCEDURE.get(lem)
        if t:
            ptype, reason = t, f"«{tokens[i]}» в названии"
            break

    items: list[dict] = []
    skip_next = False
    for i in range(cut):
        if skip_next:
            skip_next = False
            continue
        tok, lem = tokens[i], lemmas[i]
        pos = tags[i]
        if _PURE_NUM.match(tok):
            nxt = tokens[i + 1] if i + 1 < cut else ""
            if nxt in UNITS or lemmas[i + 1 if i + 1 < cut else i] in UNITS:
                skip_next = True
                if nxt not in {"шт", "уп", "пар"}:
                    unit = lemmas[i + 1]
                    items.append({"i": i, "token": f"{tok} {nxt}", "lemma": f"{tok} {unit}",
                                  "pos": "NUM"})
                continue
            continue
        if _is_stop(lem, tok):
            if lem in PROCEDURE:
                dropped.append({"text": tok, "reason": "процедурное слово"})
            continue
        if pos not in _KEEP_POS:
            continue
        items.append({"i": i, "token": tok, "lemma": lem, "pos": pos})

    first_noun_seen = False
    prev_action = False
    for it in items:
        lem, pos = it["lemma"], it["pos"]
        if lem in ACTION_TYPE:
            role = "action"
            if ptype is None:
                ptype, reason = ACTION_TYPE[lem], f"действие «{it['token']}»"
        elif pos == "LATN":
            role = "brand"
        elif pos == "NUM":
            role = "spec"
        elif pos in {"ADJF", "ADJS", "PRTF", "COMP"}:
            role = "attr"
        elif first_noun_seen and it["i"] > 0 and "для" in lemmas[max(0, it["i"] - 3):it["i"]]:
            role = "purpose"
        elif prev_action and not first_noun_seen:
            role = "object"
            first_noun_seen = True
        elif not first_noun_seen:
            role = "head"
            first_noun_seen = True
        else:
            role = "noun"
        if role not in {"attr"}:
            prev_action = role == "action"
        it["role"] = role
        it["idf"] = idf(lem) if role != "spec" else min(idf(lem.split()[0]), 0.6)
        it["weight"] = round(it["idf"] * ROLE_FACTOR[role], 3)

    if ptype is None:
        for i, lem in enumerate(lemmas[:cut]):
            if lem in WEAK_TYPE:
                ptype, reason = WEAK_TYPE[lem], f"«{tokens[i]}» в названии"
                break
    if ptype is None and items:
        ptype, reason = "товар", "нет слов-действий — предмет поставки"

    phrases: list[str] = []
    for a, b in zip(items, items[1:]):
        if b["i"] != a["i"] + 1:
            continue
        if (a["role"] == "attr" and b["pos"] == "NOUN") or \
           (a["pos"] == "NOUN" and b["pos"] == "NOUN") or \
           (a["role"] == "action" and b["role"] in {"object", "head", "noun"}) or \
           (a["role"] == "attr" and b["role"] == "purpose"):
            phrases.append(f"{a['lemma']} {b['lemma']}")

    best: dict[str, dict] = {}
    for it in items:
        if it["lemma"] not in best or it["weight"] > best[it["lemma"]]["weight"]:
            best[it["lemma"]] = it
    keywords = sorted(
        ({k: v for k, v in it.items() if k != "i"} for it in best.values()),
        key=lambda x: -x["weight"],
    )
    return {
        "keywords": keywords,
        "subject_span": [0, cut],
        "procurement_type": ptype,
        "type_reason": reason,
        "phrases": phrases,
        "dropped": dropped,
    }
