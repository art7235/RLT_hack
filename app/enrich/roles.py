"""Классификация роли контрагента: производитель / дистрибьютор / поставщик / подрядчик / исполнитель услуг."""
from __future__ import annotations

import re

ROLE_LABELS = {
    "manufacturer": "Производитель",
    "distributor": "Дистрибьютор (оптовая торговля)",
    "supplier": "Поставщик (торговля)",
    "contractor": "Подрядчик",
    "service": "Исполнитель услуг",
}

_NAME_RULES = [
    (re.compile(r"завод|фабрик|комбинат|мануфактур|производств|\bнпо\b|\bнпп\b|\bпк\b|"
                r"агрофирм|птицефабрик|молокозавод|хлебозавод|типографи", re.I), "manufacturer",
     "в названии есть признак производства"),
    (re.compile(r"торгов\w* дом|\bтд\b|трейд|trade|дистрибуц|дистрибьют|снаб|оптов|\bопт\b|импорт", re.I),
     "distributor", "в названии есть признак оптовой торговли"),
    (re.compile(r"строй|ремонт|монтаж|инжиниринг", re.I), "contractor", "в названии есть признак подрядных работ"),
]


def okved_role(code: str | None) -> tuple[str | None, str]:
    """Роль по коду ОКВЭД и пояснение."""
    if not code:
        return None, ""
    try:
        cls = int(code.split(".")[0])
    except ValueError:
        return None, ""
    if 1 <= cls <= 3:
        return "manufacturer", "сельхозпроизводство"
    if 5 <= cls <= 9:
        return "manufacturer", "добыча полезных ископаемых"
    if 10 <= cls <= 33:
        if code.startswith("33."):
            return "service", "ремонт и монтаж оборудования"
        return "manufacturer", "обрабатывающее производство"
    if 41 <= cls <= 43:
        return "contractor", "строительство"
    if cls == 45:
        return ("service", "ремонт автотранспорта") if code.startswith("45.2") else ("distributor", "торговля автотранспортом и запчастями")
    if cls == 46:
        return "distributor", "оптовая торговля"
    if cls == 47:
        return "supplier", "розничная торговля"
    return "service", "услуги"


def okpd_role(cls: int) -> tuple[str, str]:
    """Роль по классу ОКПД2, в котором поставщик чаще всего побеждает (если ОКВЭД неизвестен)."""
    if cls <= 32:
        return "supplier", "товары"
    if 41 <= cls <= 43:
        return "contractor", "строительные работы"
    return "service", "услуги"


def classify(okved_main: str | None, okved_main_name: str | None = None, okved_extra: str | None = None,
             products: str | None = None, name: str | None = None,
             n_okpd_classes: int | None = None, top_okpd: tuple[str, float] | None = None) -> dict:
    score: dict[str, float] = {r: 0.0 for r in ROLE_LABELS}
    reasons: dict[str, list[str]] = {r: [] for r in ROLE_LABELS}

    role, why = okved_role(okved_main)
    if role:
        score[role] += 3.0
        label = f"{okved_main} {okved_main_name}" if okved_main_name else okved_main
        reasons[role].append(f"основной ОКВЭД {label} — {why}")

    extra = [c for c in (okved_extra or "").split(";") if c]
    if extra:
        extra_roles: dict[str, int] = {}
        for c in extra:
            r, _ = okved_role(c)
            if r:
                extra_roles[r] = extra_roles.get(r, 0) + 1
        for r, n in extra_roles.items():
            score[r] += 1.5 * n / len(extra)
        top = max(extra_roles, key=extra_roles.get) if extra_roles else None
        if top and extra_roles[top] >= 3:
            reasons[top].append(f"{extra_roles[top]} из {len(extra)} доп. ОКВЭД — {ROLE_LABELS[top].lower()}")

    if products:
        n = len([p for p in products.split(";") if p])
        score["manufacturer"] += 2.0
        reasons["manufacturer"].append(f"в реестре МСП указана собственная продукция ({n} видов)")

    for rx, r, why in _NAME_RULES:
        if name and rx.search(name):
            score[r] += 1.5
            reasons[r].append(why)

    if top_okpd and not okved_main:
        cls, share = top_okpd
        r, what = okpd_role(int(cls))
        score[r] += 1.5
        reasons[r].append(f"ОКВЭД неизвестен; {share:.0%} побед в закупках по ОКПД2 {cls}.xx ({what})")

    if n_okpd_classes is not None and n_okpd_classes >= 8:
        score["supplier"] += 1.0 + min((n_okpd_classes - 8) / 10, 1.0)
        reasons["supplier"].append(f"поставляет товары из {n_okpd_classes} разных классов ОКПД2 — широкий ассортимент")

    total = sum(score.values())
    if total == 0:
        return {"role": None, "role_label": "Не определена", "confidence": 0.0, "confidence_label": "",
                "role_reasons": [], "role_alt": None}
    ranked = sorted(score, key=score.get, reverse=True)
    best, second = ranked[0], ranked[1]
    share = score[best] / total
    n_signals = len(reasons[best])
    level = "высокая" if share >= 0.8 and n_signals >= 2 else "средняя" if share >= 0.55 else "низкая"
    out_reasons = list(reasons[best])
    if best == "distributor":
        out_reasons.append("роль определена по виду деятельности; партнёрство с производителями не проверялось")
    alt = None
    if score[second] >= 0.3 * score[best] and reasons[second]:
        alt = {"role": second, "role_label": ROLE_LABELS[second], "reasons": reasons[second]}
        out_reasons.append(f"есть и признаки роли «{ROLE_LABELS[second]}»: {'; '.join(reasons[second])}")
    return {
        "role": best,
        "role_label": ROLE_LABELS[best],
        "confidence": round(share, 2),
        "confidence_label": level,
        "role_reasons": out_reasons,
        "role_alt": alt,
    }
