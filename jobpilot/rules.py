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


def location_ok(location: str | None, patterns: list[str]) -> bool:
    """Case-sensitive on purpose, so a pattern for the country code IN doesn't match the word "in"."""
    return bool(location) and any(re.search(p, location) for p in patterns)
