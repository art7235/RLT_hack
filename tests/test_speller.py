"""Тесты модуля исправления запросов.

Запуск:  python -m pytest tests/ -v
Требуется собранный словарь: python scripts/build_vocab.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nlp.speller import VOCAB_PATH, correct_query, correct_word, fix_layout, lemmatize  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def warm_up():
    """Прогрев: загрузка словаря и pymorphy3 до замеров скорости."""
    if not VOCAB_PATH.exists():
        pytest.skip("нет data/nlp/vocab.json — запустите scripts/build_vocab.py")
    correct_query("прогрев словаря бумага")


@pytest.fixture(scope="session")
def vocab() -> dict[str, int]:
    with VOCAB_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------- опечатки
def test_postavka():
    assert correct_query("пастовка бумаги")["tokens"][0] == "поставка"


def test_kartridzh_lemma():
    assert "картридж" in correct_query("картриджы hp")["lemmas"]


def test_latin_in_vocab_untouched():
    assert "hp" in correct_query("картриджы hp")["tokens"]


# ------------------------------------------------------------- раскладка
def test_layout():
    assert correct_query("ghbynth")["tokens"] == ["принтер"]


def test_layout_keeps_known_latin():
    for w in ("hp", "canon", "usb"):
        assert fix_layout(w) == w


# ------------------------------------------------------------- токены с цифрами
def test_fuel_mark():
    assert "аи-92" in correct_query("бензин аи-92")["tokens"]


def test_paper_format():
    assert "а4" in correct_query("бумага а4")["tokens"]


def test_short_and_numeric_not_corrected():
    assert correct_word("3х1.5") == "3х1.5"
    assert correct_word("абв") == "абв"


# ------------------------------------------------------------- без изменений
def test_no_change_flag():
    assert correct_query("бумага")["was_corrected"] is False


# ------------------------------------------------------------- лемматизация
def test_lemma_ognetushitel():
    assert "огнетушитель" in correct_query("огнетушителей")["lemmas"]


def test_lemmatize_skips_latin_and_digits():
    assert lemmatize("hp") == "hp"
    assert lemmatize("а4") == "а4"


# ------------------------------------------------------------- структура ответа
def test_result_shape():
    r = correct_query("пастовка бумаги а4")
    assert set(r) == {"original", "corrected", "was_corrected",
                      "changes", "tokens", "lemmas"}
    assert r["original"] == "пастовка бумаги а4"
    assert r["corrected"] == " ".join(r["tokens"])
    assert {"from": "пастовка", "to": "поставка"} in r["changes"]
    assert len(r["tokens"]) == len(r["lemmas"])


# ------------------------------------------------------------- скорость
def test_speed_100_queries():
    base = ["поставка бумаги а4", "картриджы hp", "ghbynth лазерный",
            "бензин аи-92", "огнетушителей порошковых", "ремонт кровли",
            "мониторв 24 дюйма", "клавиатруа мышь", "молоко питьевое",
            "уборка помещенй"]
    queries = [f"{base[i % len(base)]} лот {i}" for i in range(100)]
    assert len(set(queries)) == 100

    t0 = time.perf_counter()
    for q in queries:
        correct_query(q)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"100 запросов за {elapsed:.2f}s (лимит 2s)"


# ------------------------------------------------------------- диагностика словаря
@pytest.mark.parametrize("word", ["поставка", "картридж", "принтер",
                                  "hp", "а4", "аи-92", "огнетушитель", "бумага"])
def test_words_present_in_vocab(vocab, word):
    assert word in vocab, (
        f"слова '{word}' нет в vocab.json — тест падает из-за данных, "
        f"а не из-за логики; снизьте MIN_FREQ в scripts/build_vocab.py"
    )
