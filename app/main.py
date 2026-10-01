"""FastAPI: поиск поставщиков + статика фронтенда.

Запуск: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Literal

import duckdb
from fastapi import FastAPI, File, HTTPException, Query as Q, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import batch as batch_mod
from app.config import DB_PATH, ROOT
from app.enrich.store import get_store
from app.search.engine import Query, get_engine

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Поиск поставщиков АИС ГЗ / ЭМ СПб")
FRONT = ROOT / "frontend"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.on_event("startup")
def _warmup() -> None:
    get_engine()
    get_store()


@app.get("/api/health")
def health() -> dict:
    e, s = get_engine(), get_store()
    return {"status": "ok", "lots": len(e.lot_ids), "suppliers": len(e.profile), "rmsp_rows": len(s.rmsp)}


def _penalize_rnp(res: dict) -> None:
    """Действующая запись в реестре недобросовестных поставщиков — балл вдвое ниже, поставщик уходит вниз."""
    for s in res["suppliers"]:
        if (s.get("rnp") or {}).get("in_rnp"):
            s["score"] = round(s["score"] * 0.5, 1)
            s["status_reason"] = "балл снижен вдвое: действующая запись в реестре недобросовестных поставщиков"
    res["suppliers"].sort(key=lambda s: -s["score"])


def _run_search(q: str, platform: str | None, region_only: bool, limit: int,
                external: bool, live_enrich: bool) -> dict:
    if not q.strip():
        raise HTTPException(400, "пустой запрос")
    res = get_engine().search(Query(text=q, platform=platform, platform_only=bool(platform),
                                    region_only=region_only, limit=limit))
    store = get_store()
    store.enrich_cards(res["suppliers"], live=live_enrich)
    _penalize_rnp(res)
    res["external"] = []
    if external:
        known = {s["inn"] for s in res["suppliers"]}
        res["external"] = store.find_new_companies(res["okpd2"], exclude=known)
    return res


@app.get("/api/search")
def search(
    q: str,
    platform: Literal["ЭМ", "АИС ГЗ"] | None = None,
    region_only: bool = False,
    limit: int = Q(20, ge=1, le=100),
    external: bool = True,
    live_enrich: bool = True,
) -> dict:
    return _run_search(q, platform, region_only, limit, external, live_enrich)


@app.get("/api/search.csv")
def search_csv(q: str, platform: Literal["ЭМ", "АИС ГЗ"] | None = None, region_only: bool = False,
               limit: int = Q(50, ge=1, le=200)):
    res = _run_search(q, platform, region_only, limit, external=True, live_enrich=False)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ИНН", "Название", "Роль", "Статус", "Балл", "Источник", "Побед в похожих", "Причины"])
    for s in res["suppliers"] + res["external"]:
        w.writerow([s["inn"], s.get("name"), s.get("role_label"), s.get("status"), s.get("score"),
                    s.get("source"), (s.get("stats") or {}).get("n_wins", ""), " | ".join(s.get("reasons", []))])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return StreamingResponse(data, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=suppliers.csv"})


@app.get("/api/supplier/{inn}")
def supplier(inn: str, okpd: str = "") -> dict:
    """Карточка поставщика. okpd — коды ОКПД2 текущей закупки через запятую: опыт по ним показываем первым."""
    e = get_engine()
    card = {"inn": inn}
    get_store().enrich_cards([card])
    prefixes = [c.strip()[:8] for c in okpd.split(",") if re.fullmatch(r"\d{2}(\.\d+)*", c.strip())][:5]
    rel_sql = " OR ".join(f"s.okpd2_code LIKE '{p}%'" for p in prefixes) or "FALSE"
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute(f"""
        SELECT s.okpd2_code AS code, n.sample_name AS name, s.n_lots, s.n_wins,
               CAST(s.last_date AS VARCHAR) AS last_date, ({rel_sql}) AS relevant
        FROM supplier_okpd s LEFT JOIN okpd2_names n USING (okpd2_code)
        WHERE s.inn = ? ORDER BY relevant DESC, s.n_wins DESC, s.n_lots DESC LIMIT 15
    """, [inn]).df().to_dict("records")
    for r in rows:
        r["official_name"] = e.okpd_names.get(r["code"][:8], "")
    card["okpd2"] = rows
    lots_sql = """
        SELECT DISTINCT l.lot_id, l.subject, CAST(l.publish_date AS VARCHAR) AS date, l.start_price AS price,
               l.platform, p.is_winner, l.customer_inn,
               (SELECT count(*) FROM participations x WHERE x.lot_id = l.lot_id) AS n_bidders
        FROM participations p JOIN lots l USING (lot_id) {join}
        WHERE p.inn = ? {where} ORDER BY date DESC LIMIT {limit}
    """
    card["relevant_lots"] = []
    if prefixes:
        item_rel = " OR ".join(f"i.okpd2_code LIKE '{p}%'" for p in prefixes)
        card["relevant_lots"] = con.execute(lots_sql.format(
            join="JOIN lot_items i USING (lot_id)", where=f"AND ({item_rel})", limit=10), [inn]).df().to_dict("records")
    card["recent_lots"] = con.execute(lots_sql.format(join="", where="", limit=15), [inn]).df().to_dict("records")
    con.close()
    if inn in e.profile.index:
        p = e.profile.loc[inn]
        card["stats"] = {k: (v.item() if hasattr(v, "item") else str(v)) for k, v in p.items()}
    return card


# ------------------------------------------------------------ карточка закупки и пакет
class Procurement(BaseModel):
    """Карточка закупки — основной сценарий: «по данным закупки подобрать контрагентов»."""
    procedure_id: str | None = None
    text: str = ""
    items: list[str] = []
    okpd_codes: list[str] = []
    price: float | None = None
    customer_inn: str | None = None
    platform: Literal["ЭМ", "АИС ГЗ"] | None = None
    is_smp: bool | None = None


def _run_procurement(p: dict, limit: int, n_new: int, live: bool) -> dict:
    fields = {k: p.get(k) for k in ("text", "items", "okpd_codes", "price", "customer_inn", "platform", "is_smp")}
    fields["text"] = fields["text"] or ""
    fields["items"] = fields["items"] or []
    fields["okpd_codes"] = fields["okpd_codes"] or []
    res = get_engine().search(Query(limit=limit, **fields))
    store = get_store()
    store.enrich_cards(res["suppliers"], live=live)
    _penalize_rnp(res)
    known = {s["inn"] for s in res["suppliers"]}
    res["external"] = store.find_new_companies(res["okpd2"], exclude=known, limit=n_new) if n_new else []
    return res


@app.post("/api/procurement")
def procurement(p: Procurement, limit: int = Q(20, ge=1, le=100), n_new: int = Q(10, ge=0, le=30)) -> dict:
    if not p.text.strip() and not any(i.strip() for i in p.items):
        raise HTTPException(400, "укажите наименование закупки или хотя бы одну позицию")
    return _run_procurement(p.model_dump(), limit, n_new, live=True)


@app.get("/api/template.csv")
def template_csv():
    return StreamingResponse(io.BytesIO(batch_mod.template_csv()), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=procurement_template.csv"})


@app.get("/api/template.xlsx")
def template_xlsx():
    return StreamingResponse(io.BytesIO(batch_mod.template_xlsx()), media_type=XLSX,
                             headers={"Content-Disposition": "attachment; filename=procurement_template.xlsx"})


@app.post("/api/batch")
async def batch(file: UploadFile = File(...), limit: int = Q(10, ge=1, le=50), n_new: int = Q(5, ge=0, le=20)) -> dict:
    data = await file.read()
    procs, errors = batch_mod.parse_procurements(data, file.filename or "")
    live = len(procs) <= 5  # большие пакеты — только кэш, чтобы не упираться в лимиты ФНС
    out = []
    for p in procs:
        try:
            res = _run_procurement(p, limit, n_new, live)
        except Exception as e:  # noqa: BLE001 — одна плохая закупка не должна ронять весь пакет
            errors.append({"row": None, "procedure_id": p["procedure_id"], "problem": f"ошибка обработки: {e}"})
            continue
        out.append({"procedure_id": p["procedure_id"], "input": p, "result": res})
    result = {"procedures": out, "errors": errors, "filename": file.filename}
    result["batch_id"] = batch_mod.save_batch(result)
    return result


@app.get("/api/batch/{bid}.xlsx")
def batch_xlsx(bid: str):
    b = batch_mod.get_batch(bid)
    if not b:
        raise HTTPException(404, "результат устарел — загрузите файл ещё раз")
    return StreamingResponse(io.BytesIO(batch_mod.export_xlsx(b)), media_type=XLSX,
                             headers={"Content-Disposition": "attachment; filename=recommendations.xlsx"})


@app.get("/api/batch/{bid}.csv")
def batch_csv(bid: str):
    b = batch_mod.get_batch(bid)
    if not b:
        raise HTTPException(404, "результат устарел — загрузите файл ещё раз")
    return StreamingResponse(io.BytesIO(batch_mod.export_csv(b)), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=recommendations.csv"})


@app.get("/")
def index():
    return FileResponse(FRONT / "index.html")


if FRONT.exists():
    app.mount("/static", StaticFiles(directory=FRONT), name="static")
