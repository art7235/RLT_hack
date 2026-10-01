"""Тесты v2: регрессии спеллера, ключевые слова, синонимы, process_query.

Запуск:  python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nlp.query import process_query  # noqa: E402
from nlp.speller import VOCAB_PATH, correct_query, correct_word, lemmatize_tokens  # noqa: E402
from nlp.synonyms import expand  # noqa: E402

pytestmark = pytest.mark.skipif(not VOCAB_PATH.exists(), reason="нет vocab.json")


def toks(q):
    return correct_query(q)["tokens"]


# ------------------------------------------------------------- регрессии спеллера v1
@pytest.mark.parametrize("q, expected", [
    ("ноутбк", ["ноутбук"]),            # v1: «ноутбука» (выбирал самую частую форму)
    ("ремнт", ["ремонт"]),              # v1: «ремонту»
    ("снегоуборка", ["снегоуборка"]),   # v1: «снегоуборщика» (портил правильное слово)
    ("ve;crjq rjcn.v", ["мужской", "костюм"]),   # v1: ; и . терялись при токенизации
    ("gjcnfdrf <evfub", ["поставка", "бумаги"]),  # < — это «Б»
    ("ghbynth,", ["принтер"]),
    ("леново", ["lenovo"]),
    ("сфтщт", ["canon"]),               # латинский бренд в русской раскладке
    ("бумага a4", ["бумага", "а4"]),    # латинская «a» -> русская «а»
    ("laptop", ["laptop"]),             # английское слово не «переводим» в абракадабру
    ("wi-fi роутер", ["wi-fi", "роутер"]),
])
def test_speller_regressions(q, expected):
    assert toks(q) == expected


def test_lemma_unknown_abbreviation_kept():
    assert correct_query("скуд")["lemmas"] == ["скуд"]       # pymorphy: «скуда»
    assert correct_query("клининг")["lemmas"] == ["клининг"]  # pymorphy: «клининга»


def test_context_lemmatization():
    lemmas, tags = lemmatize_tokens(["горячего", "питания"])
    assert lemmas == ["горячий", "питание"] and tags[0] == "ADJF"


def test_known_word_not_corrected():
    assert correct_word("патока") == "патока"


# ------------------------------------------------------------- ключевые слова
def test_customer_tail_dropped():
    r = process_query("Поставка картриджей для МФУ для нужд ГБОУ школа № 548 "
                      "Калининского района Санкт-Петербурга в 2025 году")
    lemmas = {k["lemma"] for k in r["keywords"]}
    assert {"картридж", "мфу"} <= lemmas
    assert not lemmas & {"гбоу", "школа", "548", "калининский", "2025", "поставка", "нужда"}
    assert r["procurement_type"] == "товар"


def test_address_dropped_and_object():
    r = process_query("Выполнение работ по ремонту кровли по адресу: СПб, ул. Ленина, д. 5")
    roles = {k["lemma"]: k["role"] for k in r["keywords"]}
    assert roles.get("кровля") == "object" and roles.get("ремонт") == "action"
    assert "ленин" not in roles and "ленина" not in roles
    assert r["procurement_type"] == "работа"


@pytest.mark.parametrize("q, t", [
    ("Оказание услуг по уборке помещений", "услуга"),
    ("Выполнение работ по капитальному ремонту фасада", "работа"),
    ("Поставка бумаги А4", "товар"),
    ("вывоз мусора", "услуга"),
    ("огнетушитель оп-4", "товар"),
])
def test_procurement_type(q, t):
    assert process_query(q)["procurement_type"] == t


def test_purpose_weighted_lower_than_head():
    kw = {k["lemma"]: k for k in process_query("картридж для принтера")["keywords"]}
    assert kw["картридж"]["role"] == "head" and kw["принтер"]["role"] == "purpose"
    assert kw["картридж"]["weight"] > kw["принтер"]["weight"]


# ------------------------------------------------------------- синонимы
def test_abbreviation_both_ways():
    terms = process_query("скуд")["search_terms"]
    assert {"контроль", "управление", "доступ"} <= set(terms)
    terms = process_query("система контроля и управления доступом")["search_terms"]
    assert "скуд" in terms


@pytest.mark.parametrize("q, syn", [
    ("вывоз тбо", "тко"),
    ("клининг", "уборка"),
    ("медикаменты", "препарат"),
    ("системный блок", "компьютер"),
    ("гсм", "бензин"),
    ("спецодежда", "одежда"),
])
def test_synonym_expansion(q, syn):
    assert syn in process_query(q)["search_terms"]


def test_synonym_never_outweighs_original():
    r = process_query("вывоз тбо")
    t = r["search_terms"]
    assert all(t["тбо"] >= w for k, w in t.items() if k != "тбо")


def test_no_expansion_of_customer():
    # «ГБОУ» и «школа» не должны тянуть синонимы
    r = process_query("Поставка бумаги для нужд ГБОУ школа № 5")
    assert all(e["matched"] not in {"гбоу", "школа"} for e in r["synonyms"])


def test_narrower():
    terms = expand(["оргтехника"])
    assert any(e["kind"] == "narrower" and e["term"] == "принтер" for e in terms)


# ------------------------------------------------------------- формат / объяснимость / скорость
def test_result_shape_and_explain():
    r = process_query("Поставка картриджы для мфу для нужд ГБОУ")
    for key in ("original", "corrected", "changes", "tokens", "lemmas", "procurement_type",
                "keywords", "phrases", "synonyms", "search_terms", "okpd2_hints", "explain"):
        assert key in r
    assert any("картриджы" in e for e in r["explain"])
    assert {"from": "картриджы", "to": "картридж", "reason": "опечатка"} in r["changes"]


def test_speed_process_query():
    process_query("прогрев")
    base = ["Поставка бумаги а4 для нужд ГБОУ", "картриджы hp", "ghbynth лазерный",
            "Оказание услуг по уборке помещений", "Выполнение работ по ремонту кровли",
            "вывоз тбо", "скуд обслуживание", "медикаменты", "ноутбк леново", "бензин аи-95"]
    queries = [f"{base[i % len(base)]} {i}" for i in range(100)]
    t0 = time.perf_counter()
    for q in queries:
        process_query(q)
    assert time.perf_counter() - t0 < 2.0


# ------------------------------------------------------------- одежда, обувь, шины
@pytest.mark.parametrize("q, syn", [
    ("толстовка", "худи"),
    ("кросовки", "кроссовок"),
    ("зимняя резина", "шина"),
    ("Поставка спецодежды для нужд ГБУ", "халат"),
    ("Поставка школьной формы", "брюки"),
    ("рабочая обувь", "ботинок"),
])
def test_clothes_and_tires(q, syn):
    assert syn in process_query(q)["search_terms"]


def test_clothes_type_is_goods():
    r = process_query("Поставка форменной одежды для сотрудников охраны")
    assert r["procurement_type"] == "товар"
    assert "одежда" in {k["lemma"] for k in r["keywords"]}
