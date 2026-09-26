"""Settings from config.yaml, secrets from .env, and where everything lives on disk."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]
SITES = ("linkedin", "instahyre", "hirist", "wellfound", "waas")


class ConfigError(RuntimeError):
    pass


class Profile(BaseModel):
    name: str
    email: str
    phone: str = ""
    city: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    website: str = ""
    years_experience: float = 0
    notice_period_days: int = 0
    current_ctc: str = ""
    expected_ctc: str = ""
    target_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    work_authorization: str = ""
    pitch: str = ""

    @property
    def first_name(self) -> str:
        parts = self.name.split()
        return parts[0] if parts else ""


class Filters(BaseModel):
    title_include: list[str] = Field(default_factory=list)  # regexes; a title must match one (empty = any)
    title_exclude: list[str] = Field(default_factory=list)  # regexes; a match rejects the job
    company_exclude: list[str] = Field(default_factory=list)  # names, matched as whole words


class LLMSettings(BaseModel):
    model: str = "claude-opus-5"
    scoring_model: str = ""  # scoring, form answers and people picks; empty = `model`
    scoring_effort: Effort = "low"
    writing_effort: Effort = "medium"
    research_effort: Effort = "medium"


class Schedule(BaseModel):
    timezone: str = "Asia/Kolkata"
    days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat"])
    start: str = "09:30"
    end: str = "20:00"


class BrowserSettings(BaseModel):
    channel: str = "chrome"  # the installed Google Chrome; "chromium" needs `playwright install chromium`
    headless: bool = False
    slow_mo_ms: int = 50


class Approvals(BaseModel):
    max_open_cards: int = 6
    expire_after_days: int = 7


class SiteSettings(BaseModel):
    enabled: bool = True
    search_urls: list[str] = Field(default_factory=list)
    every_hours: float = 6
    reads_per_run: int = 25  # job pages opened and scored per run
    apply_per_day: int = 15
    min_score: int = 72
    selectors: dict[str, str] = Field(default_factory=dict)  # overrides for the site's default patterns


class LinkedInSettings(SiteSettings):
    connect_per_day: int = 15
    connect_per_week: int = 80
    messages_per_day: int = 15
    send_notes: bool = True
    note_limit: int = 200  # 300 on Premium
    lookups_per_day: int = 8  # company "People" searches for scouted startups


class EmailSettings(BaseModel):
    enabled: bool = True
    per_day: int = 15
    min_gap_minutes: float = 4
    followup_after_days: int = 5
    max_followups: int = 1
    signature: str = ""
    attach_resume: bool = False


class YCSettings(BaseModel):
    enabled: bool = True
    regions: list[str] = Field(default_factory=lambda: ["India", "Fully Remote", "Remote"])
    min_batch_year: int = 2019
    max_team_size: int = 300
    score_per_day: int = 60
    companies_per_day: int = 6
    min_fit: int = 70
    job_locations: list[str] = Field(
        default_factory=lambda: [
            r"\b(IN|India|Bengaluru|Bangalore|Hyderabad|Pune|Mumbai|Delhi|Gurugram|Gurgaon|Noida|Chennai)\b",
            r"\bRemote\b(?!\s*\()",
            r"\bRemote \([^)]*\b(IN|India|Global|Worldwide|Anywhere)\b",
        ]
    )


class OutreachSettings(BaseModel):
    linkedin_per_company: int = 2  # founders/leaders per company who get a connection request
    email_per_company: int = 1  # ... and who get a cold email


class ScoutSettings(BaseModel):
    enabled: bool = True
    every_days: float = 7
    per_run: int = 12
    companies_per_day: int = 4
    brief: str = ""


def _site(**overrides) -> SiteSettings:
    return SiteSettings(**overrides)


class Settings(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    profile: Profile
    filters: Filters = Field(default_factory=Filters)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    schedule: Schedule = Field(default_factory=Schedule)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    approvals: Approvals = Field(default_factory=Approvals)
    email: EmailSettings = Field(default_factory=EmailSettings)
    linkedin: LinkedInSettings = Field(default_factory=LinkedInSettings)
    instahyre: SiteSettings = Field(default_factory=SiteSettings)
    hirist: SiteSettings = Field(default_factory=SiteSettings)
    wellfound: SiteSettings = Field(default_factory=lambda: _site(apply_per_day=8))
    waas: SiteSettings = Field(default_factory=lambda: _site(apply_per_day=8))
    outreach: OutreachSettings = Field(default_factory=OutreachSettings)
    yc: YCSettings = Field(default_factory=YCSettings)
    scout: ScoutSettings = Field(default_factory=ScoutSettings)

    @property
    def dry_run(self) -> bool:
        return self.mode != "live"

    def site(self, name: str) -> SiteSettings:
        if name not in SITES:
            raise KeyError(name)
        return getattr(self, name)


class Secrets(BaseModel):
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: int | None = None
    gmail_address: str = ""
    gmail_app_password: str = ""
    hunter_api_key: str = ""

    @classmethod
    def from_env(cls) -> Secrets:
        env = os.environ
        chat = env.get("TELEGRAM_CHAT_ID", "").strip()
        return cls(
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=int(chat) if chat.lstrip("-").isdigit() else None,
            gmail_address=env.get("GMAIL_ADDRESS", "").strip(),
            gmail_app_password=env.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
            hunter_api_key=env.get("HUNTER_API_KEY", "").strip(),
        )


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def env(self) -> Path:
        return self.home / ".env"

    @property
    def profile_dir(self) -> Path:
        return self.home / "profile"

    @property
    def resume_pdf(self) -> Path:
        return self.profile_dir / "resume.pdf"

    @property
    def resume_md(self) -> Path:
        return self.profile_dir / "resume.md"

    @property
    def answers(self) -> Path:
        return self.profile_dir / "answers.yaml"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def db(self) -> Path:
        return self.data / "jobpilot.db"

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser-profile"

    @property
    def shots(self) -> Path:
        return self.data / "screenshots"

    @property
    def doctor(self) -> Path:
        return self.data / "doctor"

    @property
    def log(self) -> Path:
        return self.data / "jobpilot.log"


def resolve_home(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("JOBPILOT_HOME") or Path.cwd()).expanduser().resolve()


def load(home: Path) -> tuple[Settings, Secrets, Paths]:
    paths = Paths(home)
    load_dotenv(paths.env, override=False)
    if not paths.config.exists():
        raise ConfigError(f"{paths.config} not found. Run `jobpilot init` first.")
    raw = yaml.safe_load(paths.config.read_text()) or {}
    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"config.yaml is invalid:\n{exc}") from exc
    return settings, Secrets.from_env(), paths
