"""Обогащение карточек поставщиков и поиск новых компаний вне датасета."""
from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import duckdb
import numpy as np
import pandas as pd

from app.config import DATA_DIR, DB_PATH, LO_REGION, SPB_REGION
from app.enrich import egrul as egrul_mod
from app.enrich import rmsp_api
from app.enrich import rnp as rnp_mod
from app.enrich.roles import classify

log = logging.getLogger(__name__)
RMSP_PATH = DATA_DIR / "ext" / "rmsp.parquet"
LIVE_EGRUL_PER_REQUEST = 6
REGION_NAMES = {SPB_REGION: "Санкт-Петербург", LO_REGION: "Ленинградская область"}


_FORMS = [
    ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ПАО"), ("НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"),
    ("ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ЗАО"), ("ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ОАО"),
    ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"), ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"),
    ("ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ", "ФГУП"),
    ("ГОСУДАРСТВЕННОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ", "ГУП"), ("ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ", "ИП"),
    ("АВТОНОМНАЯ НЕКОММЕРЧЕСКАЯ ОРГАНИЗАЦИЯ", "АНО"), ("ПРОИЗВОДСТВЕННЫЙ КООПЕРАТИВ", "ПК"),
]


def short_name(name: str | None, inn: str) -> str | None:
    """«ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СКАЙ"» -> «ООО "СКАЙ"»; ФИО ИП -> «ИП Иванов Иван Иванович»."""
    if not name:
        return None
    name = " ".join(name.split())
    up = name.upper()
    for full, short in _FORMS:
        if up.startswith(full):
            return short + name[len(full):]
        if up.endswith(full):
            return f"{short} {name[:-len(full)].strip()}"
    if len(inn) == 12 and not up.startswith("ИП"):
        return "ИП " + name.title()
    if up.startswith("ИП ") and name[3:].isupper():
        return "ИП " + name[3:].title()
    return name


def _row(series: pd.Series, inn: str) -> dict:
    """Строка реестра -> dict без пропусков (NaN / pd.NA), с Python-типами для JSON."""
    out = {"inn": inn}
    for k, v in series.items():
        if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)):
            continue
        out[k] = v.item() if hasattr(v, "item") else v
    return out


class EnrichStore:
    def __init__(self) -> None:
        t0 = time.time()
        self.rmsp = pd.DataFrame()
        if RMSP_PATH.exists():
            import pyarrow.parquet as pq
            cols = ["inn", "name", "full_name", "ogrn", "region_code", "region_name", "city", "okved_main", "okved_main_name", "okved_extra",
                    "msp_category", "employees", "products", "msp_since", "in_dataset"]
            cols = [c for c in cols if c in pq.read_schema(RMSP_PATH).names]
            self.rmsp = pd.read_parquet(RMSP_PATH, columns=cols, dtype_backend="pyarrow").set_index("inn")
            self.rmsp["in_dataset"] = self.rmsp["in_dataset"].astype(bool)
        con = duckdb.connect(str(DB_PATH), read_only=True)
        self.n_classes = dict(con.execute(
            "SELECT inn, count(DISTINCT substr(okpd2_code, 1, 2)) FROM supplier_okpd GROUP BY inn").fetchall())
        self.dataset_inns = set(self.n_classes) | {r[0] for r in con.execute("SELECT inn FROM supplier_profile").fetchall()}
        self.top_okpd = {inn: (cls, share) for inn, cls, share in con.execute("""
            SELECT inn, arg_max(cls, w), max(w) / sum(w)
            FROM (SELECT inn, substr(okpd2_code, 1, 2) AS cls, sum(n_wins) AS w
                  FROM supplier_okpd WHERE n_wins > 0 GROUP BY ALL)
            GROUP BY inn
        """).fetchall()}
        self._class_wins = con.execute("""
            SELECT inn, substr(okpd2_code, 1, 5) AS cls, sum(n_wins) AS w
            FROM supplier_okpd WHERE n_wins > 0 GROUP BY ALL
        """).fetchall()
        con.close()
        self._egrul: egrul_mod.Egrul | None = None
        self.pool = pd.DataFrame()
        if not self.rmsp.empty:
            mask = (~self.rmsp["in_dataset"].to_numpy(bool)
                    & np.asarray(self.rmsp.index.astype(object).str.len() == 10))
            self.rmsp = self.rmsp[mask | self.rmsp["in_dataset"].to_numpy(bool)]
            self.pool = self.rmsp[~self.rmsp["in_dataset"].to_numpy(bool)]
            self._build_pool_index()
        self._okved_by_class: dict[str, Counter] = {}
        self._mapping_built_at = 0.0
        log.info("enrich store ready in %.1fs (rmsp offline rows: %d)", time.time() - t0, len(self.rmsp))

    def _registry(self, inns: list[str], live: bool) -> dict[str, dict]:
        """Выгрузка реестра МСП (ОКВЭД осн./доп.) + кэш онлайн-API (телефон, email). Сеть — только для отсутствующих."""
        offline = {}
        if not self.rmsp.empty:
            for inn in inns:
                if inn in self.rmsp.index:
                    offline[inn] = _row(self.rmsp.loc[inn], inn)
        api = rmsp_api.get_many(inns, live=False)
        missing = [i for i in inns if i not in offline and i not in api]
        if live and missing:
            api.update(rmsp_api.get_many(missing, live=True))
        out = {}
        for inn in inns:
            if inn in offline or inn in api:
                out[inn] = api.get(inn, {}) | offline.get(inn, {}) | {
                    k: api[inn][k] for k in ("phone", "email", "has_licenses") if inn in api and api[inn].get(k)}
        return out

    def enrich_cards(self, cards: list[dict], live: bool = True) -> None:
        inns = [c["inn"] for c in cards]
        reg = self._registry(inns, live)
        eg = egrul_mod.get_cached(inns)
        missing = [i for i in inns if i not in reg and i not in eg]
        if live and missing:
            eg.update(self._live_egrul(missing[:LIVE_EGRUL_PER_REQUEST]))
        rnp = rnp_mod.get_many(inns, live=live)
        for c in cards:
            c.update(self._company(c["inn"], reg.get(c["inn"]), eg.get(c["inn"])))
            self._apply_rnp(c, rnp.get(c["inn"]))

    @staticmethod
    def _apply_rnp(card: dict, rec: dict | None) -> None:
        """Красный флаг: поставщик в реестре недобросовестных (или был в нём)."""
        card["rnp"] = rec
        if rec is None:
            return
        if rec["in_rnp"]:
            since = next((r["included"] for r in rec["records"] if r["active"] and r["included"]), None)
            card.setdefault("reasons", []).insert(
                0, "Внимание: состоит в реестре недобросовестных поставщиков" + (f" с {since}" if since else ""))
        elif rec["was_in_rnp"]:
            card.setdefault("reasons", []).append("Ранее состоял в реестре недобросовестных поставщиков (исключён)")

    def _company(self, inn: str, r: dict | None, eg: dict | None) -> dict:
        r, eg = r or {}, eg or {}
        name = r.get("name") or eg.get("name")
        products = r.get("products") or ""
        role = classify(
            okved_main=r.get("okved_main"),
            okved_main_name=r.get("okved_main_name"),
            okved_extra=r.get("okved_extra"),
            products=products,
            name=name,
            n_okpd_classes=self.n_classes.get(inn),
            top_okpd=self.top_okpd.get(inn),
        )
        sources = (["Реестр МСП ФНС"] if r else []) + (["ЕГРЮЛ"] if eg else [])
        emp = r.get("employees")
        return {
            "name": short_name(name, inn) or f"ИНН {inn}",
            "full_name": r.get("full_name") or eg.get("full_name") or name,
            "ogrn": r.get("ogrn") or eg.get("ogrn"),
            "okved": {"code": r["okved_main"], "name": r.get("okved_main_name")} if r.get("okved_main") else None,
            "city": r.get("city"),
            "msp_category": r.get("msp_category"),
            "size": {"микро": "micro", "малое": "small", "среднее": "medium"}.get(r.get("msp_category"), "unknown"),
            "size_label": (f"{r['msp_category']} предприятие" if r.get("msp_category")
                           else "размер не определён (нет в реестре МСП)"),
            "region_name": REGION_NAMES.get(r.get("region_code")) or r.get("region_name"),
            "employees": None if emp is None or (isinstance(emp, float) and math.isnan(emp)) else int(emp),
            "phone": r.get("phone"),
            "email": r.get("email"),
            "has_licenses": r.get("has_licenses"),
            "products": [{"code": p.split("|")[0], "name": p.split("|", 1)[-1]} for p in products.split(";") if p][:5],
            "head": eg.get("head"),
            "is_active": eg.get("is_active", True),
            "liquidated": eg.get("liquidated"),
            "in_msp_registry": bool(r),
            "enrich_sources": sources,
            **role,
        }

    def _live_egrul(self, inns: list[str]) -> dict[str, dict]:
        out = {}
        try:
            if self._egrul is None:
                self._egrul = egrul_mod.Egrul()
            con = egrul_mod._db()
            for inn in inns:
                try:
                    rec = self._egrul.fetch(inn)
                except Exception:
                    self._egrul = egrul_mod.Egrul()
                    rec = self._egrul.fetch(inn)
                con.execute("INSERT OR REPLACE INTO egrul VALUES (?, ?, ?)",
                            (inn, json.dumps(rec, ensure_ascii=False) if rec else None, time.time()))
                if rec:
                    out[inn] = rec
            con.commit()
            con.close()
        except Exception as e:
            log.warning("live egrul failed: %s", e)
            self._egrul = None
        return out

    def _build_pool_index(self) -> None:
        """Инвертированный индекс ОКВЭД(XX.YY) -> позиции компаний пула + статическая часть балла."""
        t0 = time.time()
        main5 = self.pool["okved_main"].astype(object).fillna("").str[:5].to_numpy()
        self.pool_main5 = main5
        self.pool_main: dict[str, np.ndarray] = {
            k: np.asarray(v, dtype=np.int64) for k, v in pd.Series(np.arange(len(main5))).groupby(main5).groups.items()}
        extra = self.pool["okved_extra"].astype(object).fillna("").str.split(";").explode()
        extra = extra[extra.str.len() > 0].str[:5]
        pos = pd.Series(np.arange(len(self.pool)), index=self.pool.index).loc[extra.index].to_numpy()
        df = pd.DataFrame({"ok": extra.to_numpy(), "pos": pos}).drop_duplicates()
        self.pool_extra: dict[str, np.ndarray] = {k: g["pos"].to_numpy() for k, g in df.groupby("ok")}
        emp = self.pool["employees"].astype("float64").fillna(0).to_numpy(dtype=float)
        self.pool_static = 0.25 * np.log1p(emp) + 0.2 * (self.pool["region_code"].astype(object).to_numpy() == SPB_REGION)
        self.pool_inns = self.pool.index.astype(object).to_numpy()
        self.pool_local = self.pool["region_code"].astype(object).isin([SPB_REGION, LO_REGION]).to_numpy()
        log.info("pool index: %d companies, %d okved keys in %.1fs", len(self.pool), len(self.pool_extra), time.time() - t0)

    def _okved_mapping(self) -> dict[str, Counter]:
        """Какие основные ОКВЭД у победителей по классу ОКПД2 (XX.YY) — выучено на датасете."""
        if time.time() - self._mapping_built_at < 600 and self._okved_by_class:
            return self._okved_by_class
        okved = rmsp_api.cached_okved()
        if not self.rmsp.empty:
            okved.update(self.rmsp["okved_main"].astype(object).dropna().to_dict())
        m: dict[str, Counter] = defaultdict(Counter)
        for inn, cls, w in self._class_wins:
            ok = okved.get(inn)
            if ok:
                m[cls][ok[:5]] += w
        self._okved_by_class, self._mapping_built_at = m, time.time()
        return m

    def find_new_companies(self, okpd: list[dict], exclude: set[str], limit: int = 10,
                           local_only: bool = False) -> list[dict]:
        """Компании из реестра МСП, которых нет в истории закупок, но вид деятельности совпадает с закупкой"""
        if not okpd:
            return []
        mapping = self._okved_mapping()
        scores: dict[str, float] = defaultdict(float)
        why: dict[str, list[str]] = defaultdict(list)
        recs: dict[str, dict] = {}
        plans: list[tuple[str, float, str]] = []
        for o in okpd[:2]:
            cls, share = o["code"][:5], o["share"]
            plan = [(cls, 2.0, f"ОКВЭД {{ok}} соответствует ОКПД2 {cls} закупки")]
            typical = mapping.get(cls, Counter())
            total = sum(typical.values()) or 1
            for ok, w in typical.most_common(4):
                if ok != cls and w / total >= 0.08:
                    plan.append((ok, 1.5 * w / total + 0.5,
                                 f"ОКВЭД {{ok}} у {w / total:.0%} победителей закупок по ОКПД2 {cls}"))
            if not self.pool.empty:
                plans.extend((ok, weight * share, reason) for ok, weight, reason in plan)
                continue
            jobs = [(ok, weight, reason, region) for ok, weight, reason in plan for region in (SPB_REGION, LO_REGION)]
            with ThreadPoolExecutor(8) as ex:
                found = list(ex.map(lambda j: rmsp_api.search_extended(j[0], j[3], page_size=50), jobs))
            for (ok, weight, reason, region), rows in zip(jobs, found):
                for r in rows:
                    inn = r["inn"]
                    if not inn or inn in self.dataset_inns or inn in exclude:
                        continue
                    recs[inn] = r
                    scores[inn] += weight * share
                    why[inn].append(reason.format(ok=r.get("okved_main") or ok))
        if plans:
            return self._new_from_pool(plans, exclude, limit, federal=self._is_goods(okpd) and not local_only)
        match = dict(scores)
        for inn, r in recs.items():
            emp = r.get("employees") or 0
            scores[inn] += 0.25 * math.log1p(emp)
            scores[inn] += 0.2 if r.get("region_code") == SPB_REGION else 0
            scores[inn] += 0.2 if (r.get("phone") or r.get("email")) else 0
            scores[inn] -= 0.3 if r.get("kind") == "ИП" and not emp else 0
        top, per_okved = [], Counter()
        for inn in sorted(scores, key=scores.get, reverse=True):
            ok = (recs[inn].get("okved_main") or "")[:5]
            if per_okved[ok] >= max(2, int(limit * 0.6)):
                continue
            top.append(inn)
            per_okved[ok] += 1
            if len(top) >= limit:
                break
        return self._new_cards(top, recs, why, match)

    @staticmethod
    def _is_goods(okpd: list[dict]) -> bool:
        """Товар (классы ОКПД2 01–32) можно привезти из другого региона; услуги и работы — местные."""
        try:
            return bool(okpd) and int(okpd[0]["code"][:2]) <= 32
        except ValueError:
            return False

    def pool_scores(self, okpd: list[dict]) -> np.ndarray:
        """Баллы всех компаний пула по кодам ОКПД2 закупки (та же формула, что в выдаче; -1 — не подходит)."""
        mapping = self._okved_mapping()
        sc = np.zeros(len(self.pool))
        for o in okpd[:2]:
            cls, share = o["code"][:5], o["share"]
            typical = mapping.get(cls, Counter())
            total = sum(typical.values()) or 1
            plan = [(cls, 2.0)] + [(ok, 1.5 * w / total + 0.5) for ok, w in typical.most_common(4)
                                   if ok != cls and w / total >= 0.08]
            for ok, w in plan:
                sc[self.pool_main.get(ok, [])] += w * share
                sc[self.pool_extra.get(ok, [])] += 0.4 * w * share
        sc = np.where(sc > 0, sc + self.pool_static, -1)
        if not self._is_goods(okpd):
            sc[~self.pool_local] = -1
        return sc

    def _new_from_pool(self, plans: list[tuple[str, float, str]], exclude: set[str], limit: int,
                       federal: bool = True) -> list[dict]:
        """Офлайн-подбор по выгрузке реестра МСП: векторный скоринг по индексу ОКВЭД."""
        sc = np.zeros(len(self.pool))
        for ok, w, _ in plans:
            sc[self.pool_main.get(ok, [])] += w
            sc[self.pool_extra.get(ok, [])] += 0.4 * w
        matched = sc > 0
        sc = np.where(matched, sc + self.pool_static, -1)
        if not federal:
            sc[~self.pool_local] = -1
        order = np.argsort(-sc)[: limit * 40]
        top, per_okved, n_federal = [], Counter(), 0
        for p in order:
            if sc[p] <= 0:
                break
            inn = self.pool_inns[p]
            if inn in exclude:
                continue
            ok = self.pool_main5[p]
            if per_okved[ok] >= max(2, int(limit * 0.6)):
                continue
            if not self.pool_local[p]:
                if n_federal >= max(1, int(limit * 0.4)):
                    continue
                n_federal += 1
            top.append(p)
            per_okved[ok] += 1
            if len(top) >= limit:
                break
        recs, why, match = {}, {}, {}
        for p in top:
            inn = self.pool_inns[p]
            recs[inn] = _row(self.pool.iloc[p], inn)
            why[inn], match[inn] = [], 0.0
            for ok, w, reason in plans:
                if p in set(self.pool_main.get(ok, [])):
                    why[inn].append(reason.format(ok=ok) + " — это основной вид деятельности компании")
                    match[inn] = max(match[inn], w)
                elif p in set(self.pool_extra.get(ok, [])):
                    why[inn].append(reason.format(ok=ok) + " — это дополнительный вид деятельности компании")
                    match[inn] = max(match[inn], 0.4 * w)
        return self._new_cards([self.pool_inns[p] for p in top], recs, why, match)

    def _new_cards(self, top: list[str], recs: dict, why: dict, match: dict) -> list[dict]:
        """Балл новой компании — абсолютный (максимум 80: без истории закупок 100 не бывает) и с разбивкой"""
        contacts = rmsp_api.get_many(top, live=True)
        rnp = rnp_mod.get_many(top, live=True)
        out = []
        for inn in top:
            r = recs[inn] | {k: contacts[inn][k] for k in ("phone", "email") if inn in contacts and contacts[inn].get(k)}
            emp = int(r["employees"]) if r.get("employees") else 0
            year = int(str(r["msp_since"])[-4:]) if str(r.get("msp_since") or "")[-4:].isdigit() else None
            points = [
                ["Вид деятельности совпадает с закупкой", round(min(25 * match.get(inn, 0), 50), 1)],
                ["Размер компании", round(15 * min(math.log1p(emp) / math.log1p(100), 1), 1)],
                ["Стаж в реестре МСП", 5.0 if year and year <= 2023 else 2.0 if year else 0.0],
                ["Санкт-Петербург / ЛО", 5.0 if r.get("region_code") == SPB_REGION
                 else 3.0 if r.get("region_code") == LO_REGION else 0.0],
                ["Есть контакты в реестре", 5.0 if (r.get("phone") or r.get("email")) else 0.0],
            ]
            company = self._company(inn, r, None)
            okved = company.get("okved") or {}
            reasons = list(dict.fromkeys(why[inn]))[:2]
            if okved.get("code"):
                reasons.insert(0, f"Основной вид деятельности: {okved['code']} {okved.get('name') or ''}".strip())
            local = r.get("region_code") in REGION_NAMES
            size = [REGION_NAMES.get(r.get("region_code")) or r.get("region_name") or f"регион {r.get('region_code')}"]
            if not local:
                reasons.append("Компания из другого региона — показана, потому что товар можно поставить в Петербург")
            if r.get("msp_category"):
                size.append(f"{r['msp_category']} предприятие")
            size.append(f"{emp} сотрудников" if emp else "численность не указана")
            if year:
                size.append(f"в реестре МСП с {year} года")
            reasons.append(", ".join(x for x in size if x))
            card = {"inn": inn, "source": "external", "status": "new",
                    "status_reason": ("в закупках АИС ГЗ и ЭМ за 2024–2025 не участвовал; подобран по виду деятельности "
                                      f"{okved.get('code', '')} из реестра МСП"),
                    "score": round(sum(v for _, v in points), 1), "points": [x for x in points if x[1] > 0],
                    "reasons": reasons, "evidence": [], "stats": None, "factors": {}}
            card.update(company)
            self._apply_rnp(card, rnp.get(inn))
            if (card.get("rnp") or {}).get("in_rnp"):
                card["score"] = round(card["score"] * 0.5, 1)
            out.append(card)
        out.sort(key=lambda c: -c["score"])
        return out


_store: EnrichStore | None = None


def get_store() -> EnrichStore:
    global _store
    if _store is None:
        _store = EnrichStore()
    return _store
