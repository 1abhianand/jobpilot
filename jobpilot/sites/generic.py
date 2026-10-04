"""Instahyre, Hirist, Wellfound and Work at a Startup share one flow; only their patterns differ.

Open your saved search page, collect links to job pages, read each job, and apply from the job
page. Patterns can be overridden per site in config.yaml (`<site>.selectors`) when a
`jobpilot doctor <site>` run shows that the live page differs from these defaults. Patterns are
used both in Python and in the page's JavaScript, so keep them to syntax both understand.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from dataclasses import dataclass

from playwright.async_api import Locator, Page

from ..browser import (LoggedOut, close_dialogs, first_visible, goto, links, page_text, pause, scroll, text_matching,
                       top_dialog, type_text)
from ..forms import FormFiller
from .base import ApplyResult, JobPage, JobStub, Outcome

log = logging.getLogger(__name__)

DONE = (r"^(applied|already applied|application (sent|submitted)|applied successfully)$"
        r"|^applied (on|today)\b|your application (has been|was) (sent|submitted)")


@dataclass(frozen=True)
class Patterns:
    job_href: str  # matches the absolute URL of a job page linked from a search page
    job_id: str  # first group is the site's id for the job
    apply: str  # accessible name of the button or link that starts an application
    done: str = DONE  # short visible text that means you have applied
    submit: str = r"^\s*(submit|send|confirm|continue|yes)( application| note| message)?\s*$|^\s*apply\s*$"
    note_box: str = "textarea"  # CSS for the note field inside the application form
    description: str = "main, article, [class*='description' i]"  # CSS, tried in order, for the job text


@dataclass(frozen=True)
class Spec:
    name: str
    label: str
    login_url: str
    default_searches: tuple[str, ...]
    needs_note: bool  # the application includes a note to the team (approved on Telegram first)
    opens_form: bool  # the first Apply click opens a form instead of applying on the spot
    patterns: Patterns


SPECS = {
    "instahyre": Spec(
        name="instahyre", label="Instahyre", login_url="https://www.instahyre.com/login/",
        default_searches=("https://www.instahyre.com/candidate/opportunities/?matching=true",),
        needs_note=False, opens_form=False,
        patterns=Patterns(job_href=r"instahyre\.com/job[-/]\d+", job_id=r"/job[-/](\d+)",
                          apply=r"^\s*(apply|apply now|i'?m interested|interested)\s*$"),
    ),
    "hirist": Spec(
        name="hirist", label="Hirist", login_url="https://www.hirist.tech/",
        default_searches=("https://www.hirist.tech/",),  # your logged-in job feed
        needs_note=False, opens_form=False,
        patterns=Patterns(job_href=r"hirist\.tech/j/", job_id=r"-(\d{4,})(?:\.html)?(?:[/?]|$)",
                          apply=r"^\s*(apply|apply now|easy apply|quick apply)\s*$"),
    ),
    "wellfound": Spec(
        name="wellfound", label="Wellfound", login_url="https://wellfound.com/login",
        default_searches=("https://wellfound.com/jobs",),
        needs_note=True, opens_form=True,
        patterns=Patterns(job_href=r"wellfound\.com/jobs/\d+", job_id=r"/jobs/(\d+)",
                          apply=r"^\s*apply( now)?\s*$",
                          submit=r"^\s*(send application|submit application|apply)\s*$"),
    ),
    "waas": Spec(
        name="waas", label="Work at a Startup", login_url="https://www.workatastartup.com/",
        default_searches=(),  # jobs come in from YC company pages; add WaaS search URLs to widen it
        needs_note=True, opens_form=True,
        patterns=Patterns(job_href=r"workatastartup\.com/jobs/\d+", job_id=r"/jobs/(\d+)",
                          apply=r"^\s*apply\b",
                          submit=r"^\s*(send|submit|apply|send application|submit application)\s*$"),
    ),
}


class GenericSite:
    def __init__(self, spec: Spec, filler: FormFiller, overrides: dict[str, str] | None = None):
        known = {f.name for f in dataclasses.fields(Patterns)}
        unknown = set(overrides or {}) - known
        if unknown:
            raise ValueError(f"{spec.name}.selectors: unknown keys {sorted(unknown)}; allowed: {sorted(known)}")
        self.spec = spec
        self.p = dataclasses.replace(spec.patterns, **(overrides or {}))
        self.name = spec.name
        self.label = spec.label
        self.login_url = spec.login_url
        self.needs_note = spec.needs_note
        self.default_searches = list(spec.default_searches)
        self.filler = filler

    def job_id(self, url: str) -> str:
        found = re.search(self.p.job_id, url)
        return found.group(1) if found else hashlib.sha1(url.encode()).hexdigest()[:16]

    async def discover(self, page: Page, urls: list[str], limit: int) -> list[JobStub]:
        found: dict[str, JobStub] = {}
        for index, url in enumerate(urls):
            share = -(-(limit - len(found)) // (len(urls) - index))  # a fair share of what's left
            if share <= 0:
                break
            try:
                await goto(page, url)
            except LoggedOut:
                raise
            except Exception as exc:  # one slow search page mustn't cancel the jobs already waiting to be applied to
                log.warning("%s: couldn't load %s (%s); skipping it this run", self.name, url, type(exc).__name__)
                continue
            await scroll(page, rounds=6)
            taken = 0
            for link in await links(page, self.p.job_href):
                job_url = link["url"].split("?")[0]
                job_id = self.job_id(job_url)
                if job_id in found:
                    continue
                found[job_id] = JobStub(job_id, job_url, title=link["text"] or None, hint=link["card"])
                taken += 1
                if taken >= share:
                    break
        return list(found.values())[:limit]

    async def _apply_button(self, page: Page) -> Locator | None:
        name = re.compile(self.p.apply, re.IGNORECASE)
        # Work at a Startup's Apply is an <a> with no href, which has no button or link role.
        clickable = page.locator("a:not([href]), [onclick], [class*='cursor-pointer']").filter(has_text=name)
        return await first_visible(page.get_by_role("button", name=name), page.get_by_role("link", name=name), clickable)

    async def read(self, page: Page, url: str) -> JobPage:
        await goto(page, url)
        heading = await first_visible(page.locator("h1"))
        title = (await heading.inner_text()).strip() if heading else None
        description = await page_text(page, [s.strip() for s in self.p.description.split(",")])
        applied = await text_matching(page, self.p.done) is not None
        can_apply = not applied and await self._apply_button(page) is not None
        return JobPage(title=title, company=None, location=None, description=description,
                       can_apply=can_apply, applied=applied)

    async def _form(self, page: Page) -> Locator | None:
        dialog = await top_dialog(page)
        if dialog is not None:
            return dialog
        if self.needs_note and await first_visible(page.locator(self.p.note_box)) is not None:
            return page.locator("body")  # some sites put the application form inline
        return None

    async def apply(self, page: Page, url: str, note: str | None, dry_run: bool,
                    job: str | None = None) -> ApplyResult:
        info = await self.read(page, url)
        if info.applied:
            return ApplyResult(Outcome.ALREADY)
        button = await self._apply_button(page)
        if button is None:
            return ApplyResult(Outcome.UNAVAILABLE, "no Apply button on the job page")
        if dry_run and not self.spec.opens_form:
            return ApplyResult(Outcome.DRY_RUN, f"stopped before clicking {(await button.inner_text()).strip()!r}")
        await button.click()
        await pause(1.5, 3.0)
        noted = False
        for attempt in range(4):
            if await text_matching(page, self.p.done):
                await close_dialogs(page)
                return ApplyResult(Outcome.APPLIED)
            form = await self._form(page)
            if form is None:
                break
            if self.needs_note and not noted:
                box = await first_visible(form.locator(self.p.note_box))
                if box is None:
                    await close_dialogs(page)
                    return ApplyResult(Outcome.FAILED, "the application form has no note box")
                await type_text(box, note or "")
                noted = True
            missing = await self.filler.fill(form, job or info.title, include_optional=attempt > 0)
            if missing is not None:
                await close_dialogs(page)
                return ApplyResult(Outcome.NEEDS_INPUT, question=missing.label, options=missing.options)
            submit = await first_visible(form.get_by_role("button", name=re.compile(self.p.submit, re.IGNORECASE)))
            if submit is None:
                break
            if dry_run:
                await close_dialogs(page)
                return ApplyResult(Outcome.DRY_RUN, "filled the form; stopped before submitting")
            accepted: list[int] = []
            watch = lambda response: accepted.append(response.status) if (  # noqa: E731
                response.request.method == "POST" and "/apply" in response.url) else None
            page.on("response", watch)
            try:
                await submit.click()
                await pause(2.0, 4.0)
            finally:
                page.remove_listener("response", watch)
            # Work at a Startup keeps its form open after sending; the site's accepted POST is the confirmation.
            if any(200 <= status < 300 for status in accepted):
                await close_dialogs(page)
                return ApplyResult(Outcome.APPLIED)
        if await text_matching(page, self.p.done):
            await close_dialogs(page)
            return ApplyResult(Outcome.APPLIED)
        return ApplyResult(Outcome.FAILED, "clicked Apply but saw no confirmation")
