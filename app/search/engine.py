"""Поиск поставщиков по названию закупки.

1. запрос -> леммы -> TF-IDF вектор
2. похожие лоты (косинус по индексу)
3. коды ОКПД2 запроса = взвешенное голосование похожих лотов
4. кандидаты = участники похожих лотов + поставщики с опытом по этим ОКПД2
5. скоринг по объяснимым признакам, причины (reasons) строятся из тех же признаков
"""
from __future__ import annotations

import json
import math
import re
import pickle
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

import duckdb
import numpy as np
import pandas as pd
from scipy import sparse

from app.config import DATA_DIR, DB_PATH, INDEX_DIR, LO_REGION, ROOT, SPB_REGION
from app.core.text import doc_terms, lemma, normalize, process_query, stopwords

TOP_LOTS = 8000          # сколько лотов с общими словами берём в агрегацию (для балла; в счётчики идут только «похожие»)
OKPD_VOTE_LOTS = 300     # по скольким лучшим лотам голосуем за ОКПД2
OKPD_LEVEL = 8           # "26.20.11" — уровень вида продукции
OKPD_ITEMS = 300         # сколько ближайших позиций ТРУ голосуют за ОКПД2
OKPD_MISMATCH = 0.25     # множитель похожести лота, если его ОКПД2 не совпал с запросом
HALF_LIFE_DAYS = 365
WIN_W, PART_W = 1.0, 0.5

# веса итогового балла (подбираются по офлайн-оценке, см. app/eval)
W = {"text": 0.50, "okpd": 0.25, "wins": 0.20, "recency": 0.07, "region": 0.05, "breadth": 0.03, "focus": 0.05,
     "customer_rel": 0.20, "customer_any": 0.05, "price": 0.05, "platform": 0.03}
# Веса проверены на отложенных 200 закупках 2025 года (app/eval/procedure_eval.py, история 2024):
#   эти веса — Hit@10 55.3%, MRR 0.335; связь с заказчиком даёт ~17% балла, причём опыт у заказчика
#   «в этой категории» весит вчетверо больше, чем «работал с ним вообще».
#   Логрегрессия, обученная угадывать победителя, отдаёт заказчику 39% веса и даёт 55.3% — не лучше,
#   зато закрепляет «своих» поставщиков. Поэтому оставлены сбалансированные веса.
W_CARD = W
# факторы, которые считаются только если соответствующее поле закупки заполнено
CONDITIONAL = {"customer_rel": "customer_inn", "customer_any": "customer_inn", "price": "price", "platform": "platform"}
FACTOR_LABELS = {
    "text": "Похожие закупки", "okpd": "Опыт по ОКПД2", "wins": "Доля побед", "recency": "Свежесть опыта",
    "region": "СПб / ЛО", "breadth": "Число заказчиков", "rel_wins": "Победы в похожих", "max_sim": "Макс. похожесть",
    "customer_rel": "Работал с заказчиком (эта категория)", "customer_any": "Работал с заказчиком",
    "price": "Подходит по цене", "platform": "Опыт на площадке", "focus": "Специализация на категории",
}
FACTOR_LABELS["wins"] = "Доля побед в категории"
FACTOR_LABELS["breadth"] = "Заказчиков в похожих закупках"


_GOODS = re.compile(r"\b(поставк|закупк|приобретени|покупк|товар)\w*", re.I)
_SERVICES = re.compile(r"\b(оказани|услуг|выполнени|работ|обслуживани|ремонт|монтаж|обучени|вывоз|уборк|охран|аренд)\w*", re.I)
INTENT_BOOST = 4.0
SIM_OK = 0.35            # с какой похожести закупку считаем «похожей» при оценке уверенности
CONF_SCALE = {"high": 1.0, "medium": 0.85, "low": 0.6}  # множитель балла: слабое совпадение не должно выглядеть как 90+
SYN_SCALE = 1.0  # множитель веса синонимов от модуля NLP (подбирается офлайн-оценкой)


def detect_intent(text: str) -> str | None:
    """Тип закупки по исходной формулировке: goods (ОКПД2 01–33) | services (35+) | None."""
    g, s = _GOODS.search(text or ""), _SERVICES.search(text or "")
    if g and not s:
        return "goods"
    if s and not g:
        return "services"
    if g and s:  # «поставка и монтаж…» — решает то, что раньше в тексте
        return "goods" if g.start() < s.start() else "services"
    return None


def _okpd_is_goods(code: str) -> bool:
    try:
        return int(code[:2]) <= 32  # 33 — ремонт и монтаж оборудования, это услуги
    except ValueError:
        return False


def okpd_prefix(code: str | None) -> str:
    return (code or "")[:OKPD_LEVEL]


@dataclass
class Query:
    """Карточка закупки. Обязательно хотя бы одно из: text (название) или items (позиции)."""
    text: str = ""
    platform: str | None = None      # "ЭМ" | "АИС ГЗ" | None
    region_only: bool = False        # только СПб и ЛО
    limit: int = 20
    items: list[str] = field(default_factory=list)        # названия позиций спецификации
    okpd_codes: list[str] = field(default_factory=list)   # ОКПД2 позиций, если известны
    customer_inn: str | None = None
    price: float | None = None                            # НМЦ
    is_smp: bool | None = None                            # закупка только для СМП
    platform_only: bool = False      # искать похожие закупки только на этой площадке (фильтр быстрого поиска)
    exclude_lot_ids: set[int] = field(default_factory=set)  # для офлайн-оценки
    before_date: date | None = None                          # для офлайн-оценки


class SearchEngine:
    def __init__(self) -> None:
        t0 = time.time()
        self.XT = sparse.load_npz(INDEX_DIR / "tfidf_T.npz").tocsr()  # terms x lots
        self.lot_ids = np.load(INDEX_DIR / "lot_ids.npy")
        with open(INDEX_DIR / "vectorizer.pkl", "rb") as f:
            v = pickle.load(f)
        self.vocab: dict[str, int] = v["vocabulary"]
        self.idf: np.ndarray = v["idf"]

        self.IT = sparse.load_npz(INDEX_DIR / "items_T.npz").tocsr()  # terms x items
        item_code = np.load(INDEX_DIR / "items_code.npy", allow_pickle=True)
        self.item_name = np.load(INDEX_DIR / "items_name.npy", allow_pickle=True)
        self.item_n = np.load(INDEX_DIR / "items_n.npy")
        with open(INDEX_DIR / "items_vectorizer.pkl", "rb") as f:
            v = pickle.load(f)
        self.item_vocab: dict[str, int] = v["vocabulary"]
        self.item_idf: np.ndarray = v["idf"]

        con = duckdb.connect(str(DB_PATH), read_only=True)
        lots = con.execute("""
            SELECT lot_id, subject, publish_date, start_price, platform, customer_inn
            FROM lots ORDER BY lot_id
        """).df()
        assert np.array_equal(lots["lot_id"].to_numpy(), self.lot_ids), "индекс и БД рассинхронизированы"
        self.lot_subject = lots["subject"].to_numpy()
        self.lot_date = pd.to_datetime(lots["publish_date"]).to_numpy("datetime64[D]")
        self.lot_price = lots["start_price"].to_numpy()
        # экономия памяти: площадка — int8, заказчик — номер, ОКПД2 лота — CSR из номеров кодов
        self.lot_is_em = (lots["platform"] == "ЭМ").to_numpy(np.int8)
        cust_codes, cust_names = pd.factorize(lots["customer_inn"])
        self.lot_customer = cust_codes.astype(np.int32)
        self.customer_id = {c: i for i, c in enumerate(cust_names)}
        del lots
        lo = con.execute(f"""
            SELECT DISTINCT lot_id, substr(okpd2_code, 1, {OKPD_LEVEL}) AS code8
            FROM lot_items WHERE okpd2_code IS NOT NULL ORDER BY lot_id
        """).df()
        self.code8_names = np.array(sorted(set(lo["code8"]) | set(item_code)), dtype=object)
        self.code8_id = {c: i for i, c in enumerate(self.code8_names)}
        self.lot_okpd_ids = lo["code8"].map(self.code8_id).to_numpy(np.int32)
        self.lot_okpd_ptr = np.searchsorted(np.searchsorted(self.lot_ids, lo["lot_id"].to_numpy()),
                                            np.arange(len(self.lot_ids) + 1))
        self.item_code = np.array([self.code8_id[c] for c in item_code], dtype=np.int32)
        del lo, item_code
        self.max_date = self.lot_date.max()

        part = con.execute("SELECT lot_id, inn, is_winner FROM participations").df()
        part["pos"] = np.searchsorted(self.lot_ids, part["lot_id"].to_numpy())
        part = part.sort_values("pos")
        self.p_pos = part["pos"].to_numpy(np.int32)
        inn_codes, self.inn_names = pd.factorize(part["inn"])
        self.p_inn = inn_codes.astype(np.int32)
        self.inn_names = np.asarray(self.inn_names, dtype=object)
        self.p_win = part["is_winner"].to_numpy()
        self.p_ptr = np.searchsorted(self.p_pos, np.arange(len(self.lot_ids) + 1))

        self.profile = con.execute("SELECT * FROM supplier_profile").df().set_index("inn")
        sokpd = con.execute("SELECT inn, okpd2_code, n_lots, n_wins, last_date FROM supplier_okpd").df()
        sokpd["code8"] = sokpd["okpd2_code"].str[:OKPD_LEVEL]
        self.sokpd = sokpd.groupby(["code8", "inn"], as_index=False).agg(
            n_lots=("n_lots", "sum"), n_wins=("n_wins", "sum"), last_date=("last_date", "max"))

        names = con.execute("SELECT okpd2_code, sample_name, n_items FROM okpd2_names").df()
        names["code8"] = names["okpd2_code"].str[:OKPD_LEVEL]
        sample = (names.sort_values("n_items", ascending=False)
                  .drop_duplicates("code8").set_index("code8")["sample_name"].to_dict())
        # официальные названия из классификатора ОК 034-2014; если кода нет — название родительской группы,
        # и только в крайнем случае — самое частое название позиции из датасета
        official_path = ROOT / "data" / "okpd2_names.json"
        official = json.loads(official_path.read_text(encoding="utf-8")) if official_path.exists() else {}
        self.okpd_names = {}
        for c in set(sample) | set(self.code8_names):
            self.okpd_names[c] = next((official[k] for k in (c, c[:7], c[:5], c[:4], c[:2]) if k in official),
                                      sample.get(c, ""))
        con.close()
        # ИНН поставщиков, которые есть в реестре МСП (для закупок «только для СМП»)
        self.msp_inns: set[str] = set()
        rmsp = DATA_DIR / "ext" / "rmsp.parquet"
        if rmsp.exists():
            m = pd.read_parquet(rmsp, columns=["inn", "in_dataset"])
            self.msp_inns = set(m.loc[m["in_dataset"], "inn"])
        print(f"[engine] loaded in {time.time() - t0:.1f}s: {len(self.lot_ids):,} lots, "
              f"{len(self.profile):,} suppliers, {len(self.vocab):,} terms")

    # ------------------------------------------------------------------ text
    @staticmethod
    def _query_vector(terms: list[str], vocab: dict, idf: np.ndarray,
                      qweights: dict[str, float] | None = None) -> tuple[np.ndarray, np.ndarray]:
        tf = Counter(t for t in terms if t in vocab)
        if not tf:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
        idx = np.array([vocab[t] for t in tf], dtype=np.int64)
        w = np.array([(qweights.get(t, 1.0) if qweights else 1 + math.log(c)) * idf[vocab[t]]
                      for t, c in tf.items()], dtype=np.float32)
        return idx, w / np.linalg.norm(w)

    def similar_lots(self, terms: list[str], q: Query,
                     qweights: dict[str, float] | None = None) -> tuple[np.ndarray, np.ndarray]:
        idx, w = self._query_vector(terms, self.vocab, self.idf, qweights)
        if len(idx) == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
        scores = np.asarray(sparse.csr_matrix(w).dot(self.XT[idx]).todense()).ravel()
        mask = scores > 0
        if q.platform and q.platform_only:
            mask &= self.lot_is_em == (1 if q.platform == "ЭМ" else 0)
        if q.before_date is not None:
            mask &= self.lot_date < np.datetime64(q.before_date)
        cand = np.flatnonzero(mask)
        if q.exclude_lot_ids:
            excl = np.searchsorted(self.lot_ids, np.fromiter(q.exclude_lot_ids, dtype=np.int64))
            cand = np.setdiff1d(cand, excl)
        if len(cand) > TOP_LOTS:
            cand = cand[np.argpartition(-scores[cand], TOP_LOTS)[:TOP_LOTS]]
        cand = cand[np.argsort(-scores[cand])]
        return cand, scores[cand]

    def predict_okpd(self, terms: list[str], pos: np.ndarray, sims: np.ndarray,
                     intent: str | None = None, qweights: dict[str, float] | None = None) -> list[dict]:
        """kNN по позициям ТРУ; если позиции не нашлись — голосование похожих лотов."""
        votes: dict[str, float] = defaultdict(float)
        best_item: dict[str, tuple[float, str]] = {}
        idx, w = self._query_vector(terms, self.item_vocab, self.item_idf, qweights)
        if len(idx):
            sc = np.asarray(sparse.csr_matrix(w).dot(self.IT[idx]).todense()).ravel()
            k = min(OKPD_ITEMS, int((sc > 0).sum()))
            if k:
                top = np.argpartition(-sc, k - 1)[:k]
                for i in top:
                    c, s_ = self.code8_names[self.item_code[i]], float(sc[i])
                    votes[c] += s_ ** 3 * math.log1p(self.item_n[i])
                    if c not in best_item or s_ > best_item[c][0]:
                        best_item[c] = (s_, self.item_name[i])
        if not votes:
            for p, s_ in zip(pos[:OKPD_VOTE_LOTS], sims[:OKPD_VOTE_LOTS]):
                codes = set(self._lot_codes(p))
                for c in codes:
                    votes[c] += float(s_) ** 2 / len(codes)
        if not votes:
            return []
        if intent:  # тип закупки (товар/услуга) усиливает коды соответствующего раздела ОКПД2
            for c in votes:
                if _okpd_is_goods(c) == (intent == "goods"):
                    votes[c] *= INTENT_BOOST
        if intent:  # тип ясен и лидер ему соответствует — коды другого типа (товар vs услуга) отбрасываем
            leader = max(votes, key=votes.get)
            if _okpd_is_goods(leader) == (intent == "goods"):
                votes = {c: v for c, v in votes.items() if _okpd_is_goods(c) == (intent == "goods")}
        total = sum(votes.values())
        ranked = sorted(((c, v / total) for c, v in votes.items()), key=lambda x: -x[1])
        top_share = ranked[0][1]
        return [{"code": c, "share": round(sh, 3),
                 "name": self.okpd_names.get(c, ""),
                 "matched_item": best_item.get(c, (0, ""))[1]}
                for c, sh in ranked[:5] if sh >= top_share * 0.15]

    def _lot_codes(self, p: int) -> list[str]:
        return list(self.code8_names[self.lot_okpd_ids[self.lot_okpd_ptr[p]:self.lot_okpd_ptr[p + 1]]])

    def _lot_okpd_match(self, p: int, code_ids: set[int]) -> float:
        ids = self.lot_okpd_ids[self.lot_okpd_ptr[p]:self.lot_okpd_ptr[p + 1]]
        if not code_ids or len(ids) == 0:
            return 1.0
        return 1.0 if any(int(i) in code_ids for i in ids) else OKPD_MISMATCH

    def _confidence(self, qp: dict, terms: list[str], pos: np.ndarray, sims: np.ndarray,
                    okpd_from_spec: bool) -> dict:
        """Насколько запрос вообще покрыт историей закупок (абсолютная оценка, а не относительная).

        high   — похожих закупок много, все ключевые слова в них встречаются;
        medium — похожих немного или часть ключевых слов в них не встречается;
        low    — похожих закупок единицы: рекомендации приблизительные.
        """
        sw = stopwords()
        core = []
        for k in qp.get("keywords") or []:
            for tok in normalize(k.get("lemma", "")).split():
                t = tok if tok in self.vocab else lemma(tok)
                if t not in sw and len(t) > 1 and t not in core:
                    core.append(t)
        core = core or terms
        top = pos[:30]
        unmatched = []
        for t in core:
            if t not in self.vocab:
                unmatched.append(t)
            elif len(top) and len(np.intersect1d(self.XT[self.vocab[t]].indices, top)) / len(top) < 0.05:
                unmatched.append(t)
        n_sim = int((sims >= SIM_OK).sum())
        top_sim = float(sims[0]) if len(sims) else 0.0
        if n_sim < 10 or top_sim < 0.3:
            level = "low"
        elif unmatched or n_sim < 30:
            level = "medium"
        else:
            level = "high"
        if level == "low" and okpd_from_spec:
            level = "medium"  # текст редкий, но коды ОКПД2 заданы в спецификации — опираемся на них
        if level == "low":
            msg = (f"В истории закупок почти нет похожих (найдено {n_sim}). Рекомендации приблизительные: "
                   "показаны поставщики ближайших по словам закупок. Уточните название или добавьте позиции и ОКПД2.")
        elif unmatched:
            words = ", ".join(f"«{t}»" for t in unmatched[:4])
            msg = (f"В похожих закупках не встречается: {words}. Показаны поставщики, подходящие по остальным словам — "
                   "проверьте, что это то, что вам нужно.")
        elif level == "medium":
            msg = f"Похожих закупок в истории немного ({n_sim}) — рейтинг менее надёжен, чем обычно."
        else:
            msg = ""
        return {"level": level, "message": msg, "similar_lots": n_sim, "top_similarity": round(top_sim, 2),
                "unmatched_terms": unmatched}

    def _weighted_terms(self, qp: dict, raw: str) -> tuple[list[str], dict[str, float] | None]:
        """Термы запроса с весами от модуля NLP (синоним весит меньше исходного слова),
        приведённые к леммам индекса (ё->е, pymorphy для незнакомых индексу слов)."""
        st = qp.get("search_terms")
        if not isinstance(st, dict) or not st:
            return doc_terms(qp.get("corrected") or raw), None
        top = max(st.values()) or 1.0
        sw = stopwords()
        core = {k["lemma"] for k in qp.get("keywords", [])}
        # «более общие» расширения («перчатки» -> «хозтовары», «мед изделия») размывают выдачу — не берём
        broader = {lm for x in qp.get("synonyms", []) if x.get("kind") == "broader" for lm in x.get("lemmas", [])} - core
        weights: dict[str, float] = {}
        for lem, w in st.items():
            if lem in broader or re.fullmatch(r"[\d.,x×х*/-]+", lem):
                continue  # общие слова и «голые» числа (15.6) не ищем
            if core and lem not in core:
                w *= SYN_SCALE  # расширения (синонимы) слабее слов из самого запроса
            for tok in normalize(lem).split():
                t = tok if tok in self.vocab else lemma(tok)
                if t in sw or len(t) < 2:
                    continue
                weights[t] = max(weights.get(t, 0.0), w / top)
        if not weights:
            return doc_terms(qp.get("corrected") or raw), None
        # содержательные слова, которые модуль NLP отбросил («обучающихся»: школьное питание != больничное)
        for t in doc_terms(qp.get("corrected") or raw):
            if t not in weights and t in self.vocab and not re.fullmatch(r"[\d.,-]+", t):
                weights[t] = 0.5
        return list(weights), weights

    # ------------------------------------------------------------- suppliers
    def _given_okpd(self, q: Query) -> list[dict]:
        """ОКПД2 из спецификации закупки (если даны): доля кода = доля позиций с ним."""
        cnt: Counter = Counter()
        sample: dict[str, str] = {}
        for i, code in enumerate(q.okpd_codes):
            c8 = okpd_prefix((code or "").strip())
            if len(c8) >= 5 and c8[:2].isdigit():
                cnt[c8] += 1
                if c8 not in sample and i < len(q.items):
                    sample[c8] = q.items[i]
        total = sum(cnt.values())
        return [{"code": c, "share": round(n / total, 3), "name": self.okpd_names.get(c, ""),
                 "matched_item": sample.get(c, ""), "source": "спецификация закупки"}
                for c, n in cnt.most_common(5)]

    def _customer_history(self, q: Query, okpd_classes: set[str], ref_date) -> dict[str, dict]:
        """Что поставщики раньше делали для этого заказчика (только до ref_date)."""
        cid = self.customer_id.get((q.customer_inn or "").strip())
        out: dict[str, dict] = {}
        if cid is None:
            return out
        lots = np.flatnonzero((self.lot_customer == cid) & (self.lot_date < ref_date))
        for p in lots:
            codes = self._lot_codes(p)
            rel = bool(okpd_classes) and any(c[:5] in okpd_classes for c in codes)
            for k in range(self.p_ptr[p], self.p_ptr[p + 1]):
                inn, win = self.inn_names[self.p_inn[k]], bool(self.p_win[k])
                h = out.setdefault(inn, {"rel_w": 0.0, "any_w": 0.0, "rel_wins": 0, "wins": 0, "lots": 0})
                w = WIN_W if win else PART_W
                h["any_w"] += w
                h["lots"] += 1
                h["wins"] += win
                if rel:
                    h["rel_w"] += w
                    h["rel_wins"] += win
                    h.setdefault("rel_lots", []).append((p, win))
        return out

    def candidates(self, q: Query) -> dict:
        """Кандидаты и их признаки (без итогового скоринга) — общее для поиска и обучения весов."""
        items = [i for i in dict.fromkeys(x.strip() for x in q.items) if i][:50]
        text = q.text.strip() or "; ".join(items[:10])
        qp = process_query(text)
        terms, qweights = self._weighted_terms(qp, text)
        if items and q.text.strip():  # позиции спецификации дополняют название (чуть меньший вес)
            qweights = dict(qweights or {t: 1.0 for t in terms})
            for t in doc_terms(" ".join(items)):
                qweights.setdefault(t, 0.7)
            terms = list(qweights)
        pos, sims = self.similar_lots(terms, q, qweights)
        given = self._given_okpd(q)
        if given:
            okpd_list = given
            goods = sum(o["share"] for o in given if _okpd_is_goods(o["code"]))
            intent = "goods" if goods >= 0.5 else "services"
        else:
            intent = qp.get("intent") or detect_intent(text)
            okpd_list = self.predict_okpd(terms, pos, sims, intent, qweights)
        okpd = [(o["code"], o["share"]) for o in okpd_list]
        okpd_codes = {self.code8_id[c] for c, _ in okpd if c in self.code8_id}

        ref_date = np.datetime64(q.before_date) if q.before_date else self.max_date + np.timedelta64(1, "D")
        # В балл идут все лоты с общими словами (с весом s² и штрафом за чужой ОКПД2),
        # а в счётчики и доказательства — только по-настоящему похожие: s >= SIM_OK и ОКПД2 совпал.
        agg: dict[str, dict] = defaultdict(lambda: {
            "text": 0.0, "lots": [], "wins": 0, "comp_wins": 0, "parts": 0, "customers": set(), "last": None,
            "max_sim": 0.0})
        cid = self.customer_id.get((q.customer_inn or "").strip())
        for p, s in zip(pos, sims):
            age = max(int((ref_date - self.lot_date[p]).astype(int)), 0)
            decay = 0.5 ** (age / HALF_LIFE_DAYS)
            match = self._lot_okpd_match(p, okpd_codes)
            rel = float(s) ** 2 * match
            strict = s >= SIM_OK and match == 1.0
            n_bidders = self.p_ptr[p + 1] - self.p_ptr[p]
            for k in range(self.p_ptr[p], self.p_ptr[p + 1]):
                inn, win = self.inn_names[self.p_inn[k]], bool(self.p_win[k])
                a = agg[inn]
                a["text"] += rel * (WIN_W if win else PART_W) * decay
                a["max_sim"] = max(a["max_sim"], float(s) * match)
                if not strict:
                    continue
                a["wins"] += win
                a["comp_wins"] += win and n_bidders > 1
                a["parts"] += 1
                a["customers"].add(self.lot_customer[p])
                if len(a["lots"]) < 40:
                    a["lots"].append((p, float(s), win, bool(cid is not None and self.lot_customer[p] == cid)))
                d = self.lot_date[p]
                if a["last"] is None or d > a["last"]:
                    a["last"] = d

        # опыт по ОКПД2 (в т.ч. поставщики без текстового совпадения)
        okpd_exp: dict[str, float] = defaultdict(float)
        okpd_detail: dict[str, list] = defaultdict(list)
        if okpd:
            shares = dict(okpd)
            sub = self.sokpd[self.sokpd["code8"].isin(shares)]
            for code8, inn, n_lots, n_wins in sub[["code8", "inn", "n_lots", "n_wins"]].itertuples(index=False):
                okpd_exp[inn] += shares[code8] * (n_wins * WIN_W + (n_lots - n_wins) * PART_W)
                okpd_detail[inn].append((code8, int(n_lots), int(n_wins)))

        # история с этим заказчиком
        cust = self._customer_history(q, {c[:5] for c, _ in okpd}, ref_date)

        cands = (set(agg) | set(sorted(okpd_exp, key=okpd_exp.get, reverse=True)[:500])
                 | {i for i, h in cust.items() if h["rel_w"] > 0})
        if q.region_only:
            cands = {i for i in cands if i in self.profile.index
                     and self.profile.at[i, "region_code"] in (SPB_REGION, LO_REGION)}
        if q.is_smp and self.msp_inns:  # закупка только для малого бизнеса
            cands = {i for i in cands if i in self.msp_inns}

        max_text = math.log1p(10 * max((agg[i]["text"] for i in cands if i in agg), default=1.0)) or 1.0
        max_okpd = math.log1p(max((okpd_exp.get(i, 0) for i in cands), default=1.0)) or 1.0
        max_wins = math.log1p(max((agg[i]["wins"] for i in cands if i in agg), default=1)) or 1.0
        rows = []
        for inn in cands:
            a = agg.get(inn)
            prof = self.profile.loc[inn] if inn in self.profile.index else None
            h = cust.get(inn)
            f = {
                "text": (math.log1p(10 * a["text"]) / max_text) if a else 0.0,
                "okpd": math.log1p(okpd_exp.get(inn, 0)) / max_okpd,
                "rel_wins": math.log1p(a["wins"]) / max_wins if a else 0.0,
                "max_sim": a["max_sim"] if a else 0.0,
                # доля побед в ЭТОЙ категории (сглаженная), а не по всем закупкам компании
                "wins": ((a["wins"] + 1) / (a["parts"] + 3)) if a and a["parts"]
                        else 0.5 * float(prof["win_rate"]) if prof is not None else 0.0,
                "recency": (0.5 ** (max(int((ref_date - a["last"]).astype(int)), 0) / HALF_LIFE_DAYS))
                           if a and a["last"] is not None else 0.0,
                # специализация: какая доля закупок поставщика приходится на ОКПД2 этой закупки
                "focus": min(sum(n for _, n, _ in okpd_detail.get(inn, [])) / float(prof["n_lots"]), 1.0)
                         if prof is not None and prof["n_lots"] else 0.0,
                "region": 1.0 if prof is not None and prof["region_code"] == SPB_REGION else
                          0.5 if prof is not None and prof["region_code"] == LO_REGION else 0.0,
                "breadth": min(len(a["customers"]) / 10, 1.0) if a else 0.0,
                # логарифм вместо жёсткого потолка: 1 и 20 закупок у заказчика дают разный вклад
                "customer_rel": min(math.log1p(h["rel_w"]) / math.log1p(10), 1.0) if h else 0.0,
                "customer_any": min(math.log1p(h["any_w"]) / math.log1p(40), 1.0) if h else 0.0,
                "price": self._price_fit(q.price, prof),
                "platform": self._platform_share(q.platform, prof),
            }
            rows.append((inn, f, a, prof))
        confidence = self._confidence(qp, terms, pos, sims, bool(given))
        inactive = {k for k, field_name in CONDITIONAL.items()
                    if not getattr(q, field_name) or (field_name == "customer_inn" and not cust)}
        return {"qp": qp, "terms": terms, "okpd": okpd_list, "rows": rows, "okpd_detail": okpd_detail,
                "intent": intent, "cust": cust, "inactive": inactive, "confidence": confidence}

    @staticmethod
    def _price_fit(price: float | None, prof) -> float:
        """Насколько НМЦ похожа на типичный размер закупок поставщика (1 — совпадает, ->0 — на порядки)."""
        if not price or prof is None or "median_price" not in prof.index:
            return 0.0
        med = prof["median_price"]
        if med is None or pd.isna(med) or med <= 0:
            return 0.0
        return math.exp(-(math.log(price / med)) ** 2 / (2 * 1.5 ** 2))

    @staticmethod
    def _platform_share(platform: str | None, prof) -> float:
        if not platform or prof is None or not prof["n_lots"]:
            return 0.0
        n = prof["n_eshop"] if platform == "ЭМ" else prof["n_aisgz"]
        return float(n) / float(prof["n_lots"])

    def search(self, q: Query) -> dict:
        t0 = time.time()
        c = self.candidates(q)
        card = bool(q.customer_inn or q.price or q.platform and not q.platform_only or q.items)
        base = W_CARD if card else W
        weights = {k: w for k, w in base.items() if k not in c["inactive"] and w > 0}
        scale = CONF_SCALE[c["confidence"]["level"]]
        norm = (sum(weights.values()) or 1.0) / scale
        scored = sorted(((sum(weights.get(k, 0) * v for k, v in f.items()) / norm, inn, f, a, prof)
                         for inn, f, a, prof in c["rows"]), key=lambda r: -r[0])
        suppliers = [self._supplier_card(score, inn, f, a, prof, c["okpd_detail"].get(inn, []), c["cust"].get(inn))
                     for score, inn, f, a, prof in scored[: q.limit]]
        res = self._response(q, c["qp"], c["terms"], c["okpd"], suppliers, t0, total=len(scored))
        res["query"]["intent"] = c["intent"]
        res["confidence"] = c["confidence"]
        if c["confidence"]["level"] == "low":  # «Проверенный» при единичных совпадениях вводил бы в заблуждение
            for sup in suppliers:
                sup["status"] = "approx"
                sup["status_reason"] = "точных совпадений в истории нет — поставщик из ближайших по словам закупок"
        res["weights"] = {k: round(w / norm, 4) for k, w in weights.items()}
        res["query"]["procurement"] = {
            "items": len(q.items), "customer_inn": q.customer_inn, "price": q.price,
            "platform": q.platform, "is_smp": q.is_smp, "okpd_from_spec": bool(self._given_okpd(q)),
        }
        return res

    # ------------------------------------------------------------- output
    def _status(self, a, okpd_det) -> tuple[str, str]:
        """Статус — по по-настоящему похожим закупкам и с учётом того, как часто поставщик в них побеждает."""
        wins, parts = (a["wins"], a["parts"]) if a else (0, 0)
        comp = a["comp_wins"] if a else 0
        okpd_wins = sum(w for _, _, w in okpd_det)
        rate = wins / parts if parts else 0.0
        if wins >= 3 and rate >= 0.25:  # много участвует, но почти не побеждает — это не «проверенный»
            return "verified", (f"победил в {wins} из {parts} похожих закупок ({rate:.0%})"
                                + (f", из них {comp} — при конкуренции" if comp else ""))
        if wins >= 1 or okpd_wins >= 3:
            return "experienced", (f"победил в {wins} из {parts} похожих закупок ({rate:.0%})" if wins
                                   else f"{okpd_wins} побед по тем же кодам ОКПД2, но не в похожих по названию")
        if parts:
            return "participant", f"участвовал в {parts} похожих закупках, но не побеждал"
        return "category", "работал по тем же кодам ОКПД2 или в закупках с общими словами; близких по предмету нет"

    def _supplier_card(self, score, inn, f, a, prof, okpd_det, cust=None) -> dict:
        reasons = []
        if cust:
            if cust["rel_wins"]:
                reasons.append(f"Уже выигрывал у этого заказчика закупки этой категории: {cust['rel_wins']}")
            elif cust["rel_w"]:
                reasons.append("Участвовал в закупках этого заказчика по этой категории")
            elif cust["wins"]:
                reasons.append(f"Уже работал с этим заказчиком (побед: {cust['wins']})")
        if a and a["parts"]:
            line = (f"Похожие закупки (близкое название и тот же ОКПД2): участвовал в {a['parts']}, "
                    f"победил в {a['wins']}")
            if a["wins"]:
                line += (f", из них при конкуренции (2+ участника) — {a['comp_wins']}" if a["comp_wins"]
                         else "; все победы — в закупках с единственным участником")
            reasons.append(line + f". Последняя — {a['last']}")
            if len(a["customers"]) > 1:
                reasons.append(f"В похожих закупках работал с {len(a['customers'])} разными заказчиками")
        elif a:
            reasons.append("Участвовал в закупках с общими словами в названии, но близких по предмету среди них нет")
        if okpd_det:
            best = sorted(okpd_det, key=lambda x: -x[1])[:2]
            for code8, n_lots, n_wins in best:
                name = self.okpd_names.get(code8, "")
                reasons.append(f"Опыт по ОКПД2 {code8} «{name[:60]}»: {n_lots} закупок, {n_wins} побед")
            if prof is not None and prof["n_lots"] >= 10:
                share = min(sum(n for _, n, _ in okpd_det) / float(prof["n_lots"]), 1.0)
                if share >= 0.4:
                    reasons.append(f"Профильный поставщик: {share:.0%} его закупок — в этой категории")
                elif share < 0.1:
                    reasons.append(f"Универсальный поставщик: на эту категорию приходится {share:.0%} его закупок")
        if prof is not None:
            if prof["region_code"] == SPB_REGION:
                reasons.append("Стоит на налоговом учёте в Санкт-Петербурге (по КПП в заявках)")
            elif prof["region_code"] == LO_REGION:
                reasons.append("Стоит на налоговом учёте в Ленинградской области (по КПП в заявках)")
            if prof["n_lots"] >= 20:
                reasons.append(f"Всего {int(prof['n_lots'])} закупок за 2024–2025, доля побед "
                               f"{float(prof['win_rate']) * 100:.0f}%")
        status, status_reason = self._status(a, okpd_det)
        evidence = []
        # доказательства: сначала закупки этого же заказчика, затем победы, затем самые похожие
        best_lots = sorted(a["lots"], key=lambda x: (not x[3], not x[2], -x[1]))[:3] if a else []
        # причина «выигрывал у этого заказчика» должна подтверждаться примером — добавляем его закупку первой
        if cust and cust.get("rel_lots") and not any(x[3] for x in best_lots):
            p0, win0 = sorted(cust["rel_lots"], key=lambda x: (not x[1], -int(self.lot_date[x[0]].astype(int))))[0]
            best_lots = [(p0, 0.0, win0, True)] + best_lots[:2]
        for p, s, win, same_customer in best_lots:
            evidence.append({
                "same_customer": bool(same_customer),
                "lot_id": int(self.lot_ids[p]),
                "subject": self.lot_subject[p],
                "date": str(self.lot_date[p]),
                "price": None if pd.isna(self.lot_price[p]) else float(self.lot_price[p]),
                "platform": "ЭМ" if self.lot_is_em[p] else "АИС ГЗ",
                "is_winner": win,
                "similarity": round(s, 3),
            })
        return {
            "inn": inn,
            "kpp": None if prof is None or pd.isna(prof["kpp"]) else prof["kpp"],
            "name": None,   # заполняет обогащение
            "role": None,   # заполняет обогащение: manufacturer | distributor | supplier | service
            "source": "dataset",
            "score": round(score * 100, 1),
            "status": status,
            "status_reason": status_reason,
            "factors": {k: round(v, 3) for k, v in f.items()},
            "reasons": reasons,
            "evidence": evidence,
            "stats": None if prof is None else {
                "entity_type": prof["entity_type"],
                "region_code": prof["region_code"],
                "n_lots": int(prof["n_lots"]),
                "n_wins": int(prof["n_wins"]),
                "win_rate": float(prof["win_rate"]),
                "n_customers": int(prof["n_customers"]),
                "n_eshop": int(prof["n_eshop"]),
                "n_aisgz": int(prof["n_aisgz"]),
                "last_date": str(prof["last_date"]),
            },
        }

    def _response(self, q, qp, terms, okpd, suppliers, t0, total=0) -> dict:
        return {
            "query": {
                "original": q.text,
                "corrected": qp.get("corrected"),
                "was_corrected": qp.get("was_corrected", False),
                "changes": qp.get("changes", []),
                "terms": terms,
                "expansions": qp.get("expansions", {}),
                "explain": qp.get("explain", []),
                "nlp_engine": qp.get("engine"),
            },
            "okpd2": okpd,
            "total_candidates": total,
            "weights": W,
            "factor_labels": FACTOR_LABELS,
            "suppliers": suppliers,
            "took_ms": int((time.time() - t0) * 1000),
        }


_engine: SearchEngine | None = None


def get_engine() -> SearchEngine:
    global _engine
    if _engine is None:
        _engine = SearchEngine()
    return _engine
