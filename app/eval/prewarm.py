"""Прогрев кэшей обогащения для демо: прогоняет типовые запросы через запущенный API.

python -m app.eval.prewarm [http://127.0.0.1:8000]
"""
import json
import sys
import time
import urllib.parse
import urllib.request

DEMO = [
    "Поставка бумаги для офисной техники",
    "картриджы для принтера",
    "Оказание услуг по физической охране",
    "Поставка молока и молочной продукции",
    "Ремонт кровли",
    "ГСМ бензин аи-92",
    "Поставка мебели для детского сада",
    "Техническое обслуживание лифтов",
    "Поставка медицинских перчаток",
    "Вывоз твердых коммунальных отходов",
    "Поставка компьютеров и ноутбуков",
    "Услуги по уборке помещений",
    "Поставка моющих средств",
    "Обучение по охране труда",
    "Поставка овощей и фруктов",
]


def main(base: str = "http://127.0.0.1:8000") -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # мимо системного прокси
    for q in DEMO:
        t0 = time.time()
        url = f"{base}/api/search?" + urllib.parse.urlencode({"q": q})
        d = json.load(opener.open(url, timeout=300))
        print(f"{time.time() - t0:5.1f}s  {len(d['suppliers']):2d} + {len(d['external']):2d} new  {q}")


if __name__ == "__main__":
    main(*sys.argv[1:])
