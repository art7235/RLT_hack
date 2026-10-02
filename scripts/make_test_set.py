"""Generate reproducible procurement CSV fixtures from the read-only hackathon DB."""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "data"
DB = Path(r"C:\tenderhack\data\tender.duckdb")
SEED = 20261001

COLUMNS = [
    "procedure_id", "procedure_name", "product_name", "okpd2_code",
    "quantity", "start_price", "customer_inn", "platform", "is_smp",
    "publish_date",
]
DATASET_COLUMNS = [
    "lot_id", "subject", "product_name", "okpd2_code", "start_price",
    "customer_inn", "is_eshop_or_aisgz", "is_smp",
]
PLATFORMS = ("ЭМ", "АИС ГЗ")
KINDS = ("goods", "services")
SIZES = ("one", "multi")


def candidate_pool(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str, str], list[tuple[int, str]]]:
    """Find eligible lots with exactly one winner and homogeneous item types."""
    rows = con.execute(
        """
        WITH winners AS (
            SELECT lot_id
            FROM participations
            WHERE is_winner AND inn IS NOT NULL
            GROUP BY lot_id
            HAVING count(DISTINCT inn) = 1
        ), items AS (
            SELECT lot_id, count(*) AS n_items,
                   sum(CASE WHEN TRY_CAST(substr(okpd2_code, 1, 2) AS INTEGER)
                                      BETWEEN 1 AND 33 THEN 1 ELSE 0 END) AS goods,
                   sum(CASE WHEN TRY_CAST(substr(okpd2_code, 1, 2) AS INTEGER)
                                      BETWEEN 35 AND 99 THEN 1 ELSE 0 END) AS services,
                   min(substr(okpd2_code, 1, 2)) AS class_code,
                   sum(CASE WHEN product_name IS NULL OR trim(product_name) = ''
                                 OR okpd2_code IS NULL OR trim(okpd2_code) = ''
                            THEN 1 ELSE 0 END) AS missing_items
            FROM lot_items
            GROUP BY lot_id
        )
        SELECT l.lot_id, l.platform,
               CASE WHEN i.goods = i.n_items THEN 'goods' ELSE 'services' END AS kind,
               CASE WHEN i.n_items = 1 THEN 'one' ELSE 'multi' END AS size_band,
               i.class_code
        FROM lots AS l
        JOIN winners AS w USING (lot_id)
        JOIN items AS i USING (lot_id)
        WHERE l.publish_date >= DATE '2025-07-01'
          AND l.platform IN ('ЭМ', 'АИС ГЗ')
          AND (i.n_items = 1 OR i.n_items BETWEEN 5 AND 30)
          AND (i.goods = i.n_items OR i.services = i.n_items)
          AND i.missing_items = 0
          AND l.procedure_id IS NOT NULL
          AND l.subject IS NOT NULL AND trim(l.subject) <> ''
          AND l.start_price IS NOT NULL
          AND l.customer_inn IS NOT NULL AND trim(l.customer_inn) <> ''
          AND l.is_smp IS NOT NULL
        ORDER BY l.lot_id
        """
    ).fetchall()
    buckets: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for lot_id, platform, kind, size_band, class_code in rows:
        buckets[(platform, kind, size_band)].append((lot_id, class_code))
    return buckets


def select_lots(pool: dict[tuple[str, str, str], list[tuple[int, str]]]) -> list[int]:
    """Balance all three dimensions and prefer different OKPD2 classes."""
    rng = random.Random(SEED)
    chosen: list[int] = []
    seen_classes: set[str] = set()
    for pi, platform in enumerate(PLATFORMS):
        for ki, kind in enumerate(KINDS):
            for si, size_band in enumerate(SIZES):
                bucket = list(pool[(platform, kind, size_band)])
                rng.shuffle(bucket)
                quota = 3 if (pi + ki + si) % 2 == 0 else 2
                if len(bucket) < quota:
                    raise ValueError(f"Too few candidates in {(platform, kind, size_band)}")
                for _ in range(quota):
                    index = next(
                        (i for i, (_, cls) in enumerate(bucket) if cls not in seen_classes),
                        0,
                    )
                    lot_id, cls = bucket.pop(index)
                    chosen.append(lot_id)
                    seen_classes.add(cls)
    assert len(chosen) == 20 and len(set(chosen)) == 20
    return chosen


def fetch_lots(con: duckdb.DuckDBPyConnection, lot_ids: list[int]) -> tuple[list[dict], list[dict]]:
    placeholders = ",".join("?" for _ in lot_ids)
    lots = {
        row[0]: row
        for row in con.execute(
            f"""SELECT lot_id, procedure_id, subject, start_price, customer_inn,
                       platform, is_smp, publish_date
                FROM lots WHERE lot_id IN ({placeholders})""",
            lot_ids,
        ).fetchall()
    }
    items: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for lot_id, name, code in con.execute(
        f"""SELECT lot_id, product_name, okpd2_code
            FROM lot_items WHERE lot_id IN ({placeholders})
            ORDER BY lot_id, product_name, okpd2_code""",
        lot_ids,
    ).fetchall():
        items[lot_id].append((name, code))
    participants: dict[int, set[str]] = defaultdict(set)
    winners: dict[int, set[str]] = defaultdict(set)
    for lot_id, inn, is_winner in con.execute(
        f"""SELECT lot_id, inn, is_winner
            FROM participations WHERE lot_id IN ({placeholders})""",
        lot_ids,
    ).fetchall():
        if inn is None:
            continue
        participants[lot_id].add(str(inn))
        if is_winner:
            winners[lot_id].add(str(inn))

    clean: list[dict] = []
    answers: list[dict] = []
    for lot_id in lot_ids:
        _, procedure_id, subject, price, customer_inn, platform, is_smp, date = lots[lot_id]
        if len(winners[lot_id]) != 1 or not items[lot_id]:
            raise ValueError(f"Invalid selected lot: {lot_id}")
        answers.append({
            "procedure_id": str(procedure_id),
            "winner_inn": next(iter(winners[lot_id])),
            "participant_inns": ",".join(sorted(participants[lot_id])),
        })
        for name, code in items[lot_id]:
            clean.append({
                "procedure_id": str(procedure_id),
                "procedure_name": subject,
                "product_name": name,
                "okpd2_code": code,
                "quantity": "",
                "start_price": str(price),
                "customer_inn": str(customer_inn),
                "platform": platform,
                "is_smp": "да" if is_smp else "нет",
                "publish_date": date.isoformat(),
                "_lot_id": lot_id,
            })
    return clean, answers


def write_csv(path: Path, columns: list[str], rows: list[dict], *,
              encoding: str = "utf-8-sig", delimiter: str = ";",
              blank_between: bool = False) -> None:
    with path.open("w", newline="", encoding=encoding) as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        previous = None
        for row in rows:
            current = row.get("procedure_id")
            if blank_between and previous is not None and current != previous:
                writer.writerow({})
            writer.writerow(row)
            previous = current


def messy_price(value: str) -> str:
    formatted = format(Decimal(value), ",.2f")
    return formatted.replace(",", " ").replace(".", ",")


def write_readme(clean: list[dict]) -> None:
    n_rows = len(clean)
    text = f"""# Тестовые закупки

Создано командой `python scripts/make_test_set.py` из `C:\\tenderhack\\data\\tender.duckdb`.
База открывается только для чтения. Выбор фиксирован (`seed = {SEED}`).
Это 20 реальных закупок с датой не раньше 2025-07-01, ровно одним победителем
и полной спецификацией: 10 с одной позицией и 10 с 5–30 позициями;
по 10 с каждой площадки, по 10 товаров и услуг. Всего строк спецификации: {n_rows}.

| Файл | Назначение |
|---|---|
| `template_example.csv` | Заголовок шаблона и две вымышленные строки одной закупки. |
| `test_clean.csv` | Основной корректный файл: 20 закупок, одна строка на позицию. `quantity` пустая, поскольку её нет в исходных данных. |
| `test_answers.csv` | Исторический победитель и все известные участники для каждого `procedure_id`. ИНН участников разделены запятыми. Победитель — ориентир для оценки, а не единственная возможная подходящая компания. |
| `test_no_okpd.csv` | Те же закупки с пустым `okpd2_code`: проверка поиска по тексту. |
| `test_no_customer.csv` | Те же закупки без `customer_inn` и `start_price`. |
| `test_name_only.csv` | По одной строке на закупку, только `procedure_id` и `procedure_name`. |
| `test_dataset_columns.csv` | Те же позиции с названиями колонок из исходного набора; `lot_id` вместо `procedure_id`, `subject` вместо `procedure_name`, `is_smp` как `true`/`false`. |
| `test_cp1251.csv` | Содержимое `test_clean.csv` в кодировке Windows-1251 и с разделителем `,`. |
| `test_messy.csv` | Лишние пробелы, разные записи ИНН, цена с пробелами и десятичной запятой, пустые строки между закупками и лишняя колонка. |

Все CSV, кроме `test_cp1251.csv`, записаны в UTF-8 с BOM и используют `;`.
`test_clean.csv` и варианты с теми же позициями содержат {n_rows} строк данных.
В ответах и варианте только с названием — по 20 строк.
"""
    (OUT / "README.md").write_text(text, encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)
    try:
        con.execute("SET threads=2")
        pool = candidate_pool(con)
        lot_ids = select_lots(pool)
        clean, answers = fetch_lots(con, lot_ids)
    finally:
        con.close()

    for row in clean:
        for column in COLUMNS:
            str(row[column]).encode("cp1251")

    example = [
        dict(zip(COLUMNS, ["999999001", "Поставка офисной бумаги", "Бумага А4",
                           "17.12.14.110", "", "150000.00", "7801234567", "ЭМ",
                           "да", "2025-08-01"])),
        dict(zip(COLUMNS, ["999999001", "Поставка офисной бумаги", "Бумага А3",
                           "17.12.14.110", "", "150000.00", "7801234567", "ЭМ",
                           "да", "2025-08-01"])),
    ]
    write_csv(OUT / "template_example.csv", COLUMNS, example)
    write_csv(OUT / "test_clean.csv", COLUMNS, clean)
    write_csv(OUT / "test_answers.csv",
              ["procedure_id", "winner_inn", "participant_inns"], answers)
    write_csv(OUT / "test_no_okpd.csv", COLUMNS,
              [dict(row, okpd2_code="") for row in clean])
    write_csv(OUT / "test_no_customer.csv", COLUMNS,
              [dict(row, customer_inn="", start_price="") for row in clean])
    names = []
    seen_lots: set[int] = set()
    for row in clean:
        if row["_lot_id"] not in seen_lots:
            names.append({"procedure_id": row["procedure_id"],
                          "procedure_name": row["procedure_name"]})
            seen_lots.add(row["_lot_id"])
    write_csv(OUT / "test_name_only.csv", ["procedure_id", "procedure_name"], names)
    write_csv(OUT / "test_dataset_columns.csv", DATASET_COLUMNS, [
        {
            "lot_id": row["_lot_id"], "subject": row["procedure_name"],
            "product_name": row["product_name"], "okpd2_code": row["okpd2_code"],
            "start_price": row["start_price"], "customer_inn": row["customer_inn"],
            "is_eshop_or_aisgz": row["platform"],
            "is_smp": "true" if row["is_smp"] == "да" else "false",
        }
        for row in clean
    ])
    write_csv(OUT / "test_cp1251.csv", COLUMNS, clean,
              encoding="cp1251", delimiter=",")

    messy = []
    for i, row in enumerate(clean):
        changed = dict(row)
        changed["procedure_name"] = f"  {row['procedure_name']}  "
        changed["product_name"] = f" {row['product_name']} "
        changed["customer_inn"] = (str(int(row["customer_inn"])) if i % 2 == 0
                                   else f" {row['customer_inn']} ")
        changed["start_price"] = messy_price(row["start_price"])
        changed["комментарий"] = "проверка импорта"
        messy.append(changed)
    write_csv(OUT / "test_messy.csv", COLUMNS + ["комментарий"], messy,
              blank_between=True)
    write_readme(clean)
    print(f"Created 20 procurements, {len(clean)} specification rows in {OUT}")


if __name__ == "__main__":
    main()
