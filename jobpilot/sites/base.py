"""What every job-site adapter provides."""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from playwright.async_api import Page


class Outcome(StrEnum):
    APPLIED = "applied"
    DRY_RUN = "dry_run"
    ALREADY = "applied_before"
    NEEDS_INPUT = "needs_input"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass
class ApplyResult:
    outcome: Outcome
    detail: str = ""
    question: str | None = None
    options: list[str] = field(default_factory=list)


@dataclass
class JobStub:
    external_id: str
    url: str
    title: str | None = None
    hint: str = ""  # text of the listing card


@dataclass
class JobPage:
    title: str | None
    company: str | None
    location: str | None
    description: str
    can_apply: bool
    applied: bool


class Site(Protocol):
    name: str
    label: str
    login_url: str
    needs_note: bool  # the application carries a note to the team, so it goes through approval
    default_searches: list[str]

    async def discover(self, page: Page, urls: list[str], limit: int,
                       known: Container[str] = ()) -> list[JobStub]: ...

    async def read(self, page: Page, url: str) -> JobPage: ...

    async def apply(self, page: Page, url: str, note: str | None, dry_run: bool,
                    job: str | None = None) -> ApplyResult: ...
