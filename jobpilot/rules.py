"""Cheap checks that run before any LLM call."""

from __future__ import annotations

import re

from .config import Filters


def company_excluded(name: str | None, excluded: list[str]) -> bool:
    """Whole-word, case-insensitive: "Ola" matches "Ola Electric" but not "Motorola"."""
    if not name:
        return False
    lowered = name.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(bad.lower())}(?![a-z0-9])", lowered) for bad in excluded)


def reject_reason(filters: Filters, title: str | None, company: str | None) -> str | None:
    if company_excluded(company, filters.company_exclude):
        return f"{company} is on your exclude list"
    if title:
        for pattern in filters.title_exclude:
            if re.search(pattern, title, re.IGNORECASE):
                return f"title matches exclude pattern {pattern!r}"
        if filters.title_include and not any(re.search(p, title, re.IGNORECASE) for p in filters.title_include):
            return "title matches none of your include patterns"
    return None


SALARY_AMOUNT = re.compile(
    r"(?P<cur>₹|rs\.?|inr|\$|usd)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k|l|lpa|lakhs?|lacs?|cr|crores?|m)?\b",
    re.IGNORECASE)
USD_TO_INR = 88
MONTHLY = re.compile(r"(per\s+month|/\s*mo(nth)?\b|\bmonthly\b|\bp\.?m\.?(?![a-z]))", re.IGNORECASE)


def salary_max_lpa(text: str | None) -> float | None:
    """The top of a pay range like "₹10L - ₹25L", "12-18 LPA" or "$120K - $160K", in ₹ lakh a year (None if unclear)."""
    if not text:
        return None
    best = None
    unit_seen = cur_seen = None
    monthly = bool(MONTHLY.search(text))
    for m in reversed(list(SALARY_AMOUNT.finditer(text))):  # the top of a range is last; it carries the unit
        unit_seen = (m["unit"] or unit_seen or "").lower()
        cur_seen = (m["cur"] or cur_seen or "").lower()
        n = float(m["num"].replace(",", ""))
        dollars = cur_seen in ("$", "usd")
        if unit_seen.startswith(("l", "lpa")):
            lpa = n
        elif unit_seen.startswith("cr"):
            lpa = n * 100
        elif unit_seen == "k":
            lpa = n * 1000 * (USD_TO_INR if dollars else 1) / 1e5
        elif unit_seen == "m":
            lpa = n * 1e6 * (USD_TO_INR if dollars else 1) / 1e5
        elif n >= 1e5 or (monthly and n >= 1e3):
            lpa = n * (USD_TO_INR if dollars else 1) / 1e5
        else:
            continue
        best = lpa if best is None else max(best, lpa)
    if best is not None and monthly:
        best *= 12
    return round(best, 2) if best is not None else None


def below_salary_floor(stated_max_lpa: float | None, floor_lpa: float) -> bool:
    """Only a stated pay under the floor counts; a posting that doesn't say isn't held against the job."""
    return bool(floor_lpa) and stated_max_lpa is not None and stated_max_lpa < floor_lpa


def location_ok(location: str | None, patterns: list[str]) -> bool:
    """Case-sensitive on purpose, so a pattern for the country code IN doesn't match the word "in"."""
    return bool(location) and any(re.search(p, location) for p in patterns)
