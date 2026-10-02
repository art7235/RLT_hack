"""Реестр МСП ФНС (открытые данные, zip с XML) -> parquet."""
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from multiprocessing import Pool

import duckdb
import pandas as pd

from app.config import DATA_DIR, DB_PATH

ZIP_PATH = DATA_DIR / "ext" / "rmsp.zip"
OUT_PATH = DATA_DIR / "ext" / "rmsp.parquet"
KEEP_REGIONS = {"78", "47"}
CATEGORY = {"1": "микро", "2": "малое", "3": "среднее"}

_known: set[str] = set()


def _init(known: set[str], zip_path) -> None:
    global _known, ZIP_PATH
    _known, ZIP_PATH = known, zip_path


def _federal(doc: ET.Element, org) -> bool:
    """Компании из других регионов, которые имеет смысл показывать заказчику из Петербурга"""
    if org is None:
        return False
    cat = doc.get("КатСубМСП")
    if cat not in ("2", "3"):
        return False
    m = doc.find("СвОКВЭД/СвОКВЭДОсн")
    code = (m.get("КодОКВЭД") if m is not None else "") or ""
    try:
        cls = int(code.split(".")[0])
    except ValueError:
        return False
    return 10 <= cls <= 32 or (cls == 46 and cat == "3")


def _parse_doc(doc: ET.Element) -> dict | None:
    org, ip = doc.find("ОргВклМСП"), doc.find("ИПВклМСП")
    if org is not None:
        inn = org.get("ИННЮЛ")
        name = org.get("НаимОргСокр") or org.get("НаимОрг")
        full_name = org.get("НаимОрг")
        ogrn = org.get("ОГРН")
    elif ip is not None:
        inn = ip.get("ИННФЛ")
        fio = ip.find("ФИОИП")
        parts = [fio.get(k) for k in ("Фамилия", "Имя", "Отчество")] if fio is not None else []
        name = "ИП " + " ".join(p for p in parts if p)
        full_name = name
        ogrn = ip.get("ОГРНИП")
    else:
        return None
    mn = doc.find("СведМН")
    region = mn.get("КодРегион") if mn is not None else None
    if inn not in _known and region not in KEEP_REGIONS and not _federal(doc, org):
        return None
    region_name = None
    if mn is not None and mn.find("Регион") is not None:
        r = mn.find("Регион")
        region_name = f"{r.get('Наим', '')} {r.get('Тип', '')}".strip() or None
    city = None
    if mn is not None:
        for tag in ("Город", "НаселПункт", "Регион"):
            el = mn.find(tag)
            if el is not None and el.get("Наим"):
                city = f"{el.get('Тип', '')} {el.get('Наим')}".strip()
                break
    okved_main = okved_main_name = None
    okved_extra = []
    sv = doc.find("СвОКВЭД")
    if sv is not None:
        m = sv.find("СвОКВЭДОсн")
        if m is not None:
            okved_main, okved_main_name = m.get("КодОКВЭД"), m.get("НаимОКВЭД")
        okved_extra = [e.get("КодОКВЭД") for e in sv.findall("СвОКВЭДДоп") if e.get("КодОКВЭД")]
    products = [(p.get("КодПрод"), p.get("НаимПрод")) for p in doc.findall("СвПрод")]
    return {
        "inn": inn,
        "name": name,
        "full_name": full_name,
        "ogrn": ogrn,
        "region_code": region,
        "region_name": region_name,
        "city": city,
        "okved_main": okved_main,
        "okved_main_name": okved_main_name,
        "okved_extra": ";".join(okved_extra),
        "msp_category": CATEGORY.get(doc.get("КатСубМСП"), doc.get("КатСубМСП")),
        "employees": int(doc.get("ССЧР")) if (doc.get("ССЧР") or "").isdigit() else None,
        "msp_since": doc.get("ДатаВклМСП"),
        "is_social": doc.get("СведСоцПред") == "1",
        "products": ";".join(f"{c}|{n}" for c, n in products if c),
        "n_licenses": len(doc.findall("СвЛиценз")),
        "n_partner_programs": len(doc.findall("СвПрогПарт")),
        "in_dataset": inn in _known,
    }


def _parse_member(member: str) -> list[dict]:
    out = []
    with zipfile.ZipFile(ZIP_PATH) as zf, zf.open(member) as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag == "Документ":
                rec = _parse_doc(el)
                if rec:
                    out.append(rec)
                el.clear()
    return out


def build(zip_path=None) -> None:
    global ZIP_PATH
    if zip_path:
        from pathlib import Path
        ZIP_PATH = Path(zip_path)
    t0 = time.time()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    known = {r[0] for r in con.execute("SELECT inn FROM supplier_profile").fetchall()}
    con.close()
    with zipfile.ZipFile(ZIP_PATH) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".xml")]
    print(f"{len(members):,} xml files, {len(known):,} known INN")

    rows: list[dict] = []
    with Pool(processes=min(6, os.cpu_count() or 2), initializer=_init, initargs=(known, ZIP_PATH)) as pool:
        for i, part in enumerate(pool.imap_unordered(_parse_member, members, chunksize=8)):
            rows.extend(part)
            if i % 500 == 0:
                print(f"[{time.time() - t0:6.0f}s] {i:,}/{len(members):,} files, {len(rows):,} rows")
    df = pd.DataFrame(rows).drop_duplicates("inn")
    df.to_parquet(OUT_PATH, index=False)
    print(f"[{time.time() - t0:6.0f}s] saved {len(df):,} rows "
          f"({df['in_dataset'].sum():,} from dataset) -> {OUT_PATH}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else None)
