from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jobpilot.clock import Clock
from jobpilot.config import Paths, Settings
from jobpilot.db import DB
from jobpilot.llm import Draft, FieldAnswer, Fit, JobScore
from jobpilot.sites.base import ApplyResult, JobPage, Outcome
from jobpilot.sites.linkedin import Member, Reach

FIXTURES = Path(__file__).parent / "fixtures"
THURSDAY_11_30_IST = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)

BASE_SETTINGS: dict[str, Any] = {
    "profile": {
        "name": "Asha Rao", "email": "asha@example.org", "phone": "+91 9000000000", "city": "Bengaluru",
        "linkedin_url": "https://www.linkedin.com/in/asha-example/", "github_url": "https://github.com/asha",
        "years_experience": 5, "notice_period_days": 30, "current_ctc": "30 LPA", "expected_ctc": "40 LPA",
        "target_roles": ["Backend Engineer"], "preferred_locations": ["Bengaluru", "Remote"],
        "work_authorization": "Indian citizen",
    },
    "filters": {"company_exclude": ["Ola"], "title_exclude": [r"\bintern\b"]},
    "email": {"signature": "Asha Rao\nlinkedin.com/in/asha-example"},
}


def make_settings(**sections: dict[str, Any]) -> Settings:
    data = {key: dict(value) for key, value in BASE_SETTINGS.items()}
    for key, value in sections.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    return Settings.model_validate(data)


class MovableClock(Clock):
    def __init__(self, settings: Settings, moment: datetime = THURSDAY_11_30_IST):
        self.moment = moment
        super().__init__(settings.schedule, now=lambda: self.moment)


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.job_score = 80
        self.fit_score = 80
        self.answers: dict[str, tuple[str, bool]] = {}
        self.scouted: list = []
        self.picks: list[int] = [0]

    async def score_job(self, **kw: Any) -> JobScore:
        self.calls.append(("score_job", kw))
        return JobScore(score=self.job_score, reason="solid backend match", title=kw["title"] or "Backend Engineer",
                        company=kw["company"] or "Acme", location=kw["location"] or "Bengaluru")

    async def score_company(self, text: str) -> Fit:
        self.calls.append(("score_company", text))
        return Fit(score=self.fit_score, reason="builds LLM support agents")

    async def write(self, channel: str, **kw: Any) -> Draft:
        self.calls.append(("write", channel, kw))
        return Draft(subject="Backend work at Acme" if channel == "email" else None, body=f"{channel} draft")

    async def answer(self, *, question: str, kind: str, options: list[str], job: str | None) -> FieldAnswer:
        self.calls.append(("answer", question))
        text, confident = self.answers.get(question, ("", False))
        return FieldAnswer(answer=text, confident=confident)

    async def scout(self, **kw: Any) -> list:
        self.calls.append(("scout", kw))
        return self.scouted

    async def pick_people(self, **kw: Any) -> list[int]:
        self.calls.append(("pick_people", kw))
        return self.picks


class FakeBot:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def notify(self, text: str) -> None:
        self.events.append(("notify", text))

    async def mark(self, outreach_id: int, line: str) -> None:
        self.events.append(("mark", outreach_id, line))

    async def failed(self, outreach_id: int, reason: str, *, without_note: bool = False) -> None:
        self.events.append(("failed", outreach_id, reason, without_note))

    async def ask_answer(self, job, question: str, options: list[str], hint: str = "") -> None:
        self.events.append(("ask", job["id"], question))

    async def pump(self) -> int:
        return 0

    def digest(self) -> str:
        return "digest"

    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]


class FakeBrowser:
    def __init__(self) -> None:
        self.sessions = 0

    @asynccontextmanager
    async def session(self, headless: bool | None = None):
        self.sessions += 1
        yield object()

    async def screenshot(self, page, label: str) -> str:
        return f"/tmp/{label}.png"


class FakeSite:
    def __init__(self, name: str, needs_note: bool = False) -> None:
        self.name = name
        self.label = name.title()
        self.login_url = ""
        self.needs_note = needs_note
        self.default_searches = [f"https://{name}.example/search"]
        self.stubs: list = []
        self.pages: dict[str, JobPage] = {}
        self.results: dict[str, ApplyResult] = {}
        self.applied: list[tuple] = []

    async def discover(self, page, urls, limit):
        return self.stubs[:limit]

    async def read(self, page, url):
        return self.pages[url]

    async def apply(self, page, url, note, dry_run, job=None):
        self.applied.append((url, note, dry_run, job))
        return self.results.get(url) or ApplyResult(Outcome.DRY_RUN if dry_run else Outcome.APPLIED)


class FakeLinkedIn(FakeSite):
    def __init__(self) -> None:
        super().__init__("linkedin")
        self.reach, self.detail = Reach.SENT, ""
        self.connected = False
        self.connects: list[tuple] = []
        self.messages: list[tuple] = []

    async def connect(self, page, url, expected, note, dry_run):
        self.connects.append((url, expected, note, dry_run))
        return (Reach.DRY_RUN, "stopped") if dry_run else (self.reach, self.detail)

    async def message(self, page, url, expected, text, dry_run):
        self.messages.append((url, expected, text, dry_run))
        return (Reach.DRY_RUN, "stopped") if dry_run else (self.reach, self.detail)

    async def member(self, page, url):
        return Member(name="Someone", connected=self.connected, pending=not self.connected)

    async def company_url(self, page, company):
        return "https://www.linkedin.com/company/acme-example/"

    async def people(self, page, url):
        return [{"url": "https://www.linkedin.com/in/lead-example", "name": "Lee Lead", "headline": "CTO at Acme"},
                {"url": "https://www.linkedin.com/in/sam-example", "name": "Sam Sales", "headline": "Sales at Acme"}]


class FakeFinder:
    def __init__(self, result: tuple[str | None, str] = ("dev@acme-ai.example", "guess")) -> None:
        self.result = result
        self.lookups: list[tuple] = []

    async def find(self, name: str, domain: str | None):
        self.lookups.append((name, domain))
        return self.result


def mock_http(routes: dict[str, httpx.Response | str]) -> httpx.AsyncClient:
    """An httpx client that answers from `routes` (URL without query -> response or body text)."""
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        found = routes.get(key)
        if found is None:
            return httpx.Response(404, text=f"no fixture for {key}")
        return found if isinstance(found, httpx.Response) else httpx.Response(200, text=found)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The pipeline paces itself like a person; tests don't need to wait."""
    from jobpilot import pipeline

    async def instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "pause", instant)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def db() -> DB:
    return DB(":memory:")


@pytest.fixture
def clock(settings: Settings) -> MovableClock:
    return MovableClock(settings)


@pytest.fixture
def app(tmp_path: Path, settings: Settings, db: DB, clock: MovableClock) -> SimpleNamespace:
    """Just enough of App for the pipeline: real settings, DB and clock; fakes for everything external."""
    sites = {"linkedin": FakeLinkedIn(), "instahyre": FakeSite("instahyre"),
             "hirist": FakeSite("hirist"), "wellfound": FakeSite("wellfound", needs_note=True),
             "waas": FakeSite("waas", needs_note=True)}
    return SimpleNamespace(settings=settings, db=db, clock=clock, llm=FakeLLM(), bot=FakeBot(),
                           browser=FakeBrowser(), sites=sites, finder=FakeFinder(), mailbox=None,
                           http=mock_http({}), paths=Paths(tmp_path))
