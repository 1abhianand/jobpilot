"""Y Combinator data, all public and login-free.

- yc-oss publishes the YC directory's "currently hiring" list as JSON, refreshed daily.
- Each company page on ycombinator.com embeds its data (founders with LinkedIn URLs, open
  jobs) as JSON in a data-page attribute; so does each job page (full description plus the
  application's custom questions).
Applications themselves go through Work at a Startup, which needs your login.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx

from .config import YCSettings
from .rules import company_excluded

HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
YC_URL = "https://www.ycombinator.com"
WAAS_JOB_URL = "https://www.workatastartup.com/jobs/{id}"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
SEASONS = {"winter": "W", "summer": "S", "fall": "F", "spring": "X"}
TECH_TITLES = re.compile(r"\b(CTO|technical|engineering|tech lead|chief technology)\b", re.IGNORECASE)
CEO_TITLES = re.compile(r"\b(CEO|chief executive)\b", re.IGNORECASE)


def batch_year(batch: str | None) -> int | None:
    batch = (batch or "").strip()
    if found := re.search(r"\b(19|20)\d{2}\b", batch):
        return int(found.group())
    if found := re.fullmatch(r"[WSFX](\d{2})", batch):
        return 2000 + int(found.group(1))
    return None


def short_batch(batch: str | None) -> str | None:
    """'Winter 2024' -> 'W24'; already-short names pass through."""
    batch = (batch or "").strip()
    if re.fullmatch(r"[WSFX]\d{2}", batch):
        return batch
    season, _, year = batch.partition(" ")
    if season.lower() in SEASONS and year.isdigit():
        return f"{SEASONS[season.lower()]}{year[-2:]}"
    return batch or None


def prefilter(companies: list[dict[str, Any]], settings: YCSettings, exclude: list[str]) -> list[dict[str, Any]]:
    wanted_regions = {r.lower() for r in settings.regions}
    kept = []
    for company in companies:
        if company.get("status") != "Active" or not company.get("isHiring"):
            continue
        regions = {r.lower() for r in company.get("regions") or []}
        if wanted_regions and not wanted_regions & regions:
            continue
        year = batch_year(company.get("batch"))
        if year is None or year < settings.min_batch_year:
            continue
        if (company.get("team_size") or 0) > settings.max_team_size:
            continue
        if company_excluded(company.get("name"), exclude):
            continue
        kept.append(company)
    return kept


def parse_page(page_html: str) -> dict[str, Any]:
    found = re.search(r'data-page="([^"]*)"', page_html)
    if not found:
        raise ValueError("no data-page JSON on the page; YC may have changed its site")
    return json.loads(html.unescape(found.group(1)))


async def fetch_hiring(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await http.get(HIRING_URL, timeout=60)
    response.raise_for_status()
    return response.json()


async def company_page(http: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    response = await http.get(f"{YC_URL}/companies/{slug}", headers=BROWSER_HEADERS, timeout=30)
    response.raise_for_status()
    return parse_page(response.text)["props"]


async def job_page(http: httpx.AsyncClient, path: str) -> dict[str, Any]:
    url = path if path.startswith("http") else YC_URL + path
    response = await http.get(url, headers=BROWSER_HEADERS, timeout=30)
    response.raise_for_status()
    return parse_page(response.text)["props"]


def founders(props: dict[str, Any]) -> list[dict[str, Any]]:
    """Active founders, technical ones first (they're the right audience for an engineer)."""
    people = [
        {"name": f.get("full_name", "").strip(), "title": f.get("title") or "Founder",
         "linkedin_url": (f.get("linkedin_url") or "").strip() or None, "bio": f.get("founder_bio") or ""}
        for f in (props.get("company") or {}).get("founders") or []
        if f.get("full_name") and f.get("is_active", True)
    ]

    def rank(person: dict[str, Any]) -> int:
        if TECH_TITLES.search(person["title"]):
            return 0
        return 1 if CEO_TITLES.search(person["title"]) else 2

    return sorted(people, key=rank)


def jobs(props: dict[str, Any]) -> list[dict[str, Any]]:
    return list(props.get("jobPostings") or [])


def job_summary(job: dict[str, Any]) -> str:
    facts = [("Role", job.get("title")), ("Location", job.get("location")), ("Type", job.get("type")),
             ("Experience", job.get("minExperience")), ("Salary", job.get("salaryRange")),
             ("Equity", job.get("equityRange")), ("Visa", job.get("visa")),
             ("Skills", ", ".join(s.get("name", str(s)) if isinstance(s, dict) else str(s)
                                  for s in job.get("skills") or []))]
    return "\n".join(f"{label}: {value}" for label, value in facts if value)


def company_summary(entry: dict[str, Any]) -> str:
    """Text for the fit score, from a yc-oss entry."""
    facts = [
        ("Name", f"{entry.get('name')} (YC {short_batch(entry.get('batch'))})"),
        ("One-liner", entry.get("one_liner")),
        ("What they do", entry.get("long_description")),
        ("Industries", ", ".join(entry.get("industries") or [])),
        ("Tags", ", ".join(entry.get("tags") or [])),
        ("Team size", entry.get("team_size")),
        ("Stage", entry.get("stage")),
        ("Locations", entry.get("all_locations")),
        ("Hires in", ", ".join(entry.get("regions") or [])),
    ]
    return "\n".join(f"{label}: {value}" for label, value in facts if value)
