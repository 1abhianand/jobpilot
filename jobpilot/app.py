"""Wires settings, state and services together. Everything else takes an App.

Services are built on first use, so commands that don't need an API key or a resume (like
`jobpilot login`) work before those are set up.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic

from .approvals import ApprovalBot
from .browser import Browser
from .clock import Clock
from .config import ConfigError, load
from .db import DB
from .email_finder import EmailFinder
from .forms import AnswerBook, FormFiller, load_rules
from .llm import LLM
from .mail import Mailbox
from .profile import candidate_context, load_resume
from .sites import Site, build_sites
from .telegram import Telegram


class App:
    def __init__(self, home: Path):
        self.settings, self.secrets, self.paths = load(home)
        self.db = DB(self.paths.db)
        self.clock = Clock(self.settings.schedule)
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.browser = Browser(self.settings.browser, self.paths.browser_profile, self.paths.shots)

    @cached_property
    def resume(self) -> str:
        return load_resume(self.paths)

    @cached_property
    def llm(self) -> LLM:
        if not self.secrets.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is missing from .env")
        client = AsyncAnthropic(api_key=self.secrets.anthropic_api_key, max_retries=4)
        context = candidate_context(self.settings.profile, self.resume)
        return LLM(self.settings.llm, self.settings.profile, context, client)

    @cached_property
    def bot(self) -> ApprovalBot:
        if not self.secrets.telegram_bot_token or self.secrets.telegram_chat_id is None:
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are needed in .env "
                              "(run `jobpilot telegram-id` to find the chat id)")
        from . import pipeline  # the bot's Rewrite button redrafts through the pipeline

        async def rewrite(outreach, instruction):
            return await pipeline.rewrite(self, outreach, instruction)

        return ApprovalBot(self.db, Telegram(self.secrets.telegram_bot_token), self.secrets.telegram_chat_id,
                           self.settings, self.clock, rewrite)

    @cached_property
    def mailbox(self) -> Mailbox | None:
        if not (self.secrets.gmail_address and self.secrets.gmail_app_password):
            return None
        return Mailbox(self.secrets.gmail_address, self.secrets.gmail_app_password, self.settings.profile.name)

    @cached_property
    def finder(self) -> EmailFinder:
        return EmailFinder(self.http, self.secrets.hunter_api_key)

    @cached_property
    def filler(self) -> FormFiller:
        try:
            llm: LLM | None = self.llm
        except (ConfigError, FileNotFoundError, ValueError):
            llm = None  # forms then rely on your saved answers and profile only
        book = AnswerBook(self.db, self.settings.profile, llm, load_rules(self.paths.answers))
        return FormFiller(book, self.paths.resume_pdf if self.paths.resume_pdf.exists() else None)

    @cached_property
    def sites(self) -> dict[str, Site]:
        return build_sites(self.settings, self.filler)

    async def close(self) -> None:
        await self.http.aclose()
