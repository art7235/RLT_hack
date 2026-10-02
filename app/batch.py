"""Пакетная обработка: шаблон закупок (CSV / Excel) -> рекомендации по каждой закупке -> Excel.

Шаблон: одна строка = одна позиция спецификации; строки одной закупки имеют общий procedure_id.
Колонки узнаются по нескольким вариантам названий (в т.ч. как в исходном датасете АИС ГЗ).
Пустые поля допустимы: нет ОКПД2 — определим по названию, нет заказчика — фактор не учитывается.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from collections import OrderedDict

import pandas as pd

TEMPLATE_COLUMNS = ["procedure_id", "procedure_name", "product_name", "okpd2_code", "quantity",
                    "start_price", "customer_inn", "platform", "is_smp", "publish_date"]
TEMPLATE_TITLES = {
    "procedure_id": "Номер закупки (общий для всех позиций одной закупки)",
    "procedure_name": "Наименование закупки",
    "product_name": "Наименование позиции (товар / работа / услуга)",
    "okpd2_code": "Код ОКПД2 позиции (если известен)",
    "quantity": "Количество (необязательно)",
    "start_price": "НМЦ закупки, руб.",
    "customer_inn": "ИНН заказчика",
    "platform": "Площадка: ЭМ или АИС ГЗ",
    "is_smp": "Только для СМП: да / нет",
    "publish_date": "Дата публикации (необязательно)",
}
TEMPLATE_EXAMPLE = [
    ["1", "Поставка бумаги для офисной техники", "Бумага для офисной техники А4, 500 л.", "17.12.14.129", "100",
     "45000", "7802141070", "ЭМ", "нет", ""],
    ["1", "Поставка бумаги для офисной техники", "Бумага для офисной техники А3, 500 л.", "17.12.14.129", "20",
     "45000", "7802141070", "ЭМ", "нет", ""],
    ["2", "Оказание услуг по физической охране объекта", "Услуги частной охраны", "80.10.12.000", "",
     "1200000", "", "АИС ГЗ", "да", ""],
]

# варианты названий колонок (сравнение без регистра, пробелов и знаков)
ALIASES = {
    "procedure_id": ["procedure_id", "id", "номер", "номер закупки", "номер процедуры", "№", "n"],
    "lot_id": ["lot_id", "номер лота", "лот", "id лота"],
    "reqnum": ["reqnum", "реестровый номер", "номер извещения", "номер извещения еис"],
    "procedure_name": ["procedure_name", "subject", "name", "наименование закупки", "название закупки",
                       "предмет закупки", "объект закупки", "закупка"],
    "product_name": ["product_name", "item", "позиция", "наименование позиции", "наименование товара",
                     "товар", "тру", "наименование тру", "наименование"],
    "okpd2_code": ["okpd2_code", "okpd2", "окпд2", "код окпд2", "окпд", "код окпд"],
    "quantity": ["quantity", "количество", "кол-во", "колво"],
    "start_price": ["start_price", "price", "нмц", "нмцк", "начальная цена", "начальная максимальная цена",
                    "цена", "сумма"],
    "customer_inn": ["customer_inn", "инн заказчика", "заказчик инн", "заказчик", "инн"],
    "platform": ["platform", "is_eshop_or_aisgz", "площадка", "тип площадки"],
    "is_smp": ["is_smp", "смп", "только смп", "для смп", "субъекты мсп"],
    "publish_date": ["publish_date", "дата", "дата публикации"],
}


def _key(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я№]+", " ", str(s).lower().replace("ё", "е")).strip()


_ALIAS_INDEX = {_key(a): col for col, names in ALIASES.items() for a in names}
_SILENT = {"customer kpp", "кпп заказчика"}  # колонки выгрузки АИС ГЗ, которые нам не нужны — без замечания


# ------------------------------------------------------------------ template
def template_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(TEMPLATE_COLUMNS)
    w.writerows(TEMPLATE_EXAMPLE)
    return buf.getvalue().encode("utf-8-sig")


def template_xlsx() -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame(TEMPLATE_EXAMPLE, columns=TEMPLATE_COLUMNS).to_excel(xw, sheet_name="Закупки", index=False)
        pd.DataFrame({"Колонка": TEMPLATE_COLUMNS, "Описание": [TEMPLATE_TITLES[c] for c in TEMPLATE_COLUMNS]}) \
            .to_excel(xw, sheet_name="Инструкция", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    max(len(str(c.value or "")) for c in col) + 2, 60)
    return out.getvalue()


# ------------------------------------------------------------------ parsing
def _read_table(data: bytes, filename: str = "") -> pd.DataFrame:
    if data[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm")):
        return pd.read_excel(io.BytesIO(data), dtype=str, sheet_name=0)
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    head = "\n".join(text.splitlines()[:5])
    sep = max([";", ",", "\t", "|"], key=head.count)
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False, skip_blank_lines=True,
                       quotechar='"', engine="python")


def _price(v: str) -> float | None:
    s = re.sub(r"[^\d,.\-]", "", str(v or "")).replace(",", ".")
    if s.count(".") > 1:  # «1.234.567.89» -> оставляем последнюю точку как десятичную
        head, tail = s.rsplit(".", 1)
        s = head.replace(".", "") + "." + tail
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _inn(v: str) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) in (9, 11):  # потерянный ведущий ноль (Excel)
        d = "0" + d
    return d if len(d) in (10, 12) else None


def _bool(v: str) -> bool | None:
    s = str(v or "").strip().lower()
    if s in ("да", "true", "1", "yes", "y", "истина", "+"):
        return True
    if s in ("нет", "false", "0", "no", "n", "ложь", "-"):
        return False
    return None


def _platform(v: str) -> str | None:
    s = str(v or "").strip().lower()
    if not s:
        return None
    if "эм" == s or "магазин" in s or "eshop" in s or s == "em":
        return "ЭМ"
    if "аис" in s or "ais" in s or "гз" in s:
        return "АИС ГЗ"
    return None


def _normalize(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Приводит названия колонок к нашим; возвращает таблицу и список нераспознанных колонок."""
    mapping, unknown = {}, []
    for c in df.columns:
        col = _ALIAS_INDEX.get(_key(c))
        if col and col not in mapping.values():
            mapping[c] = col
        elif not col and _key(c) not in _SILENT:
            unknown.append(str(c))  # второй вариант уже распознанной колонки (subject при procedure_name) молча пропускаем
    df = df.rename(columns=mapping)[[c for c in mapping.values()]]
    df = df.fillna("").astype(str).apply(lambda x: x.str.strip())
    return df[(df != "").any(axis=1)], unknown


def _merge(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Несколько файлов (например, «извещения» и «позиции» из выгрузки АИС ГЗ) склеиваем по номеру лота
    или номеру закупки. Если общего номера нет — просто ставим строки друг за другом."""
    if len(tables) == 1:
        return tables[0]
    key = next((k for k in ("lot_id", "procedure_id") if all(k in t.columns for t in tables)), None)
    if key is None:
        return pd.concat(tables, ignore_index=True).fillna("")
    items = [t for t in tables if "product_name" in t.columns]
    heads = [t for t in tables if "product_name" not in t.columns]
    if not items or not heads:
        return pd.concat(tables, ignore_index=True).fillna("")
    head = pd.concat(heads, ignore_index=True).drop_duplicates(key)
    body = pd.concat(items, ignore_index=True)
    body = body[[c for c in body.columns if c == key or c not in head.columns]]
    return body.merge(head, on=key, how="outer").fillna("")


def parse_procurements(data, filename: str = "", max_procedures: int = 300) -> tuple[list[dict], list[dict]]:
    """Файл или несколько файлов -> (список закупок, список проблем). Закупка = dict с полями Query.
    data: bytes одного файла или список пар (bytes, имя файла)."""
    files = data if isinstance(data, list) else [(data, filename)]
    errors: list[dict] = []
    tables = []
    for raw, name in files:
        try:
            t, unknown = _normalize(_read_table(raw, name))
        except Exception as e:  # noqa: BLE001 — любая ошибка чтения файла идёт пользователю как проблема
            errors.append({"row": None, "procedure_id": None, "problem": f"не удалось прочитать файл {name}: {e}"})
            continue
        if unknown:
            errors.append({"row": None, "procedure_id": None,
                           "problem": f"{name}: колонки не распознаны и пропущены: " + ", ".join(unknown[:10])})
        if len(t):
            tables.append(t)
    if not tables:
        return [], errors or [{"row": None, "procedure_id": None, "problem": "в файле нет данных"}]
    df = _merge(tables)
    if len(files) > 1 and len(tables) > 1:
        linked = "по номеру лота" if all("lot_id" in t.columns for t in tables) else "по номеру закупки"
        errors.append({"row": None, "procedure_id": None,
                       "problem": f"загружено файлов: {len(tables)}, строки связаны {linked}"})
    if "procedure_name" not in df.columns and "product_name" not in df.columns:
        errors.append({"row": None, "procedure_id": None,
                       "problem": "нет ни названия закупки, ни названий позиций — искать не по чему"})
        return [], errors

    groups: "OrderedDict[str, list]" = OrderedDict()
    for i, row in df.iterrows():
        pid = (row.get("lot_id", "") or row.get("procedure_id", "") or row.get("procedure_name", "")
               or f"строка {i + 2}")
        groups.setdefault(pid, []).append((i, row))

    procs = []
    for pid, rows in groups.items():
        first = rows[0][1]
        name = next((r.get("procedure_name", "") for _, r in rows if r.get("procedure_name", "")), "")
        items = [r.get("product_name", "") for _, r in rows]
        codes = [r.get("okpd2_code", "") for _, r in rows]
        if not name and not any(items):
            errors.append({"row": rows[0][0] + 2, "procedure_id": pid, "problem": "пустая закупка: нет названия и позиций"})
            continue
        cust_raw = next((r.get("customer_inn", "") for _, r in rows if r.get("customer_inn", "")), "")
        cust = _inn(cust_raw)
        if cust_raw and not cust:
            errors.append({"row": rows[0][0] + 2, "procedure_id": pid,
                           "problem": f"ИНН заказчика «{cust_raw}» некорректен — фактор заказчика не учтён"})
        bad_codes = [c for c in codes if c and not re.match(r"^\d{2}(\.\d+)*$", c)]
        if bad_codes:
            errors.append({"row": rows[0][0] + 2, "procedure_id": pid,
                           "problem": f"некорректные коды ОКПД2 ({', '.join(bad_codes[:3])}) — определим по названию"})
        procs.append({
            "procedure_id": first.get("procedure_id", "") or pid,
            "lot_id": first.get("lot_id", ""),
            "reqnum": first.get("reqnum", ""),
            "text": name,
            "items": [x for x in items if x],
            "okpd_codes": [c if re.match(r"^\d{2}(\.\d+)*$", c or "") else "" for c in codes],
            "price": _price(first.get("start_price", "")),
            "customer_inn": cust,
            "platform": _platform(first.get("platform", "")),
            "is_smp": _bool(first.get("is_smp", "")),
        })
        if len(procs) >= max_procedures:
            errors.append({"row": None, "procedure_id": None,
                           "problem": f"обработаны первые {max_procedures} закупок, остальные пропущены"})
            break
    return procs, errors


# ------------------------------------------------------------------ results store + export
_BATCHES: "OrderedDict[str, dict]" = OrderedDict()


def save_batch(result: dict) -> str:
    bid = uuid.uuid4().hex[:12]
    _BATCHES[bid] = result
    while len(_BATCHES) > 20:
        _BATCHES.popitem(last=False)
    return bid


def get_batch(bid: str) -> dict | None:
    return _BATCHES.get(bid)


def _rows(batch: dict) -> tuple[list[dict], list[dict]]:
    summary, recs = [], []
    for p in batch["procedures"]:
        r = p["result"]
        hist, new = r.get("suppliers", []), r.get("external", [])
        summary.append({
            "Номер закупки": p["procedure_id"],
            "Номер лота": p["input"].get("lot_id", ""),
            "Наименование": p["input"]["text"] or "; ".join(p["input"]["items"][:3]),
            "Позиций": len(p["input"]["items"]),
            "ОКПД2": ", ".join(o["code"] for o in r.get("okpd2", [])[:3]),
            "Тип": {"goods": "товар", "services": "работа/услуга"}.get(r["query"].get("intent"), ""),
            "Уверенность подбора": {"high": "высокая", "medium": "средняя", "low": "низкая"}.get(
                (r.get("confidence") or {}).get("level"), ""),
            "Замечание": (r.get("confidence") or {}).get("message", ""),
            "Найдено из истории": len(hist),
            "Новых компаний": len(new),
            "Топ-1": hist[0]["name"] if hist else "",
            "Топ-2": hist[1]["name"] if len(hist) > 1 else "",
            "Топ-3": hist[2]["name"] if len(hist) > 2 else "",
        })
        for rank, s in enumerate(hist + new, 1):
            recs.append({
                "Номер закупки": p["procedure_id"],
                "Номер лота": p["input"].get("lot_id", ""),
                "Место": rank,
                "ИНН": s["inn"],
                "Название": s.get("name"),
                "Источник": "история закупок" if s.get("source") == "dataset" else "новая компания (реестр МСП)",
                "Роль": s.get("role_label"),
                "Статус": s.get("status"),
                "Балл": s.get("score"),
                "Причины": " | ".join(s.get("reasons", [])[:3]),
                "Телефон": s.get("phone") or "",
                "Email": s.get("email") or "",
                "ОКВЭД": (s.get("okved") or {}).get("code", "") if s.get("okved") else "",
            })
    return summary, recs


def export_xlsx(batch: dict) -> bytes:
    summary, recs = _rows(batch)
    errors = [{"Строка файла": e.get("row") or "", "Номер закупки": e.get("procedure_id") or "", "Проблема": e["problem"]}
              for e in batch.get("errors", [])]
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame(summary).to_excel(xw, sheet_name="Сводка", index=False)
        pd.DataFrame(recs).to_excel(xw, sheet_name="Рекомендации", index=False)
        pd.DataFrame(errors or [{"Строка файла": "", "Номер закупки": "", "Проблема": "нет"}]) \
            .to_excel(xw, sheet_name="Ошибки", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    max(len(str(c.value or "")) for c in col[:200]) + 2, 70)
    return out.getvalue()


def export_csv(batch: dict) -> bytes:
    _, recs = _rows(batch)
    return pd.DataFrame(recs).to_csv(sep=";", index=False).encode("utf-8-sig")
