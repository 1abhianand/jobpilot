"""Instahyre, Hirist, Wellfound and Work at a Startup share one flow; only their patterns differ.

Open your saved search page, collect links to job pages, read each job, and apply from the job
page. Patterns can be overridden per site in config.yaml (`<site>.selectors`) when a
`jobpilot doctor <site>` run shows that the live page differs from these defaults. Patterns are
used both in Python and in the page's JavaScript, so keep them to syntax both understand.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Container
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..browser import (LoggedOut, close_dialogs, first_visible, goto, links, page_text, pause, scroll, text_matching,
                       top_dialog, type_text)
from ..forms import FormFiller
from .base import ApplyResult, JobPage, JobStub, Outcome

log = logging.getLogger(__name__)

DONE = (r"^(applied|already applied|application (sent|submitted)|applied successfully)\s*[.!]?$"
        r"|^applied (on|today)\b|your application (has been|was) (sent|submitted)"
        r"|^application (sent|submitted) (to|successfully)\b")  # Instahyre: "Application sent to Acme!"
CONFIRM_WAIT = 15  # seconds a slow site (Hirist's spinning Apply button) gets to show its confirmation


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
    listing_js: str | None = None  # for search pages whose job cards aren't links: returns [{url, text, card}]
    # for search pages that don't run their search from the URL: (page, url, limit) -> [{url, text, card}] or None
    search: Callable[[Page, str, int], Awaitable[list[dict[str, str]] | None]] | None = None
    form_url: str | None = None  # a page the Apply click can lead to that is itself the application form


# Instahyre's opportunity cards are AngularJS buttons ("View job »") with no href; the job's own page
# URL sits on each card's scope.
INSTAHYRE_LISTING_JS = """() => {
  if (!window.angular) return [];
  const out = [];
  for (const el of document.querySelectorAll('[ng-click="openApplyModal(opp)"]')) {
    const opp = angular.element(el).scope()?.opp;
    const job = opp?.job;
    if (!job?.opportunity_url) continue;
    const company = opp.employer?.company_name || job.hiring_company_name || '';
    out.push({url: new URL(job.opportunity_url, location.origin).href,
              text: `${company} - ${job.title}`,
              card: [`${company} - ${job.title}`, job.locations, (job.keywords || []).join(', '),
                     opp.employer?.instahyre_note || ''].join('\\n').slice(0, 800)});
  }
  return out;
}"""

INSTAHYRE_API = "https://www.instahyre.com/api/v1/job_search"
INSTAHYRE_PAGE = 35  # the API's page size


async def instahyre_search(page: Page, url: str, limit: int) -> list[dict[str, str]] | None:
    """Instahyre's search page ignores its own URL on load, so run the search through the API it calls.

    Takes the URL the search page shows after a search, e.g.
    /candidate/opportunities/?search=true&skills=Python,LLM&location=Bangalore,Work From Home&years=1
    (skills are OR-ed). Returns None for a URL that isn't a search, such as the recommendations page.
    """
    query = parse_qs(urlsplit(url).query)
    if query.get("search") != ["true"]:
        return None
    split = lambda key: [v.strip() for value in query.get(key, []) for v in value.split(",") if v.strip()]  # noqa: E731
    params = [("company_size", "0"), ("job_type", "0"), ("status", "0")]
    params += [("skills", s) for s in split("skills")] + [("jobLocations", c) for c in split("location")]
    params += [("years", y) for y in query.get("years", [])[:1]]
    out: list[dict[str, str]] = []
    offset = 0
    while len(out) < limit:
        response = await page.request.get(f"{INSTAHYRE_API}?{urlencode(params + [('offset', str(offset))])}")
        if not response.ok:
            log.warning("instahyre: search API returned HTTP %s for %s", response.status, url)
            break
        data = await response.json()
        for item in data.get("objects", []):
            company = (item.get("employer") or {}).get("company_name", "")
            text = f"{company} - {item.get('title', '')}"
            out.append({"url": item.get("public_url", ""), "text": text,
                        "card": "\n".join([text, item.get("locations", ""), ", ".join(item.get("keywords") or []),
                                           (item.get("employer") or {}).get("instahyre_note") or ""])[:800]})
        offset += INSTAHYRE_PAGE
        if not (data.get("meta") or {}).get("next"):
            break
        await pause()
    return out


SPECS = {
    "instahyre": Spec(
        name="instahyre", label="Instahyre", login_url="https://www.instahyre.com/login/",
        default_searches=("https://www.instahyre.com/candidate/opportunities/?matching=true",),
        needs_note=False, opens_form=False,
        patterns=Patterns(job_href=r"instahyre\.com/job[-/]\d+", job_id=r"/job[-/](\d+)",
                          apply=r"^\s*(apply|apply now|i'?m interested|interested)\s*$"),
        listing_js=INSTAHYRE_LISTING_JS, search=instahyre_search,
    ),
    "hirist": Spec(
        name="hirist", label="Hirist", login_url="https://www.hirist.tech/",
        default_searches=("https://www.hirist.tech/",),  # your logged-in job feed
        needs_note=False, opens_form=False,
        patterns=Patterns(job_href=r"hirist\.tech/j/", job_id=r"-(\d{4,})(?:\.html)?(?:[/?]|$)",
                          apply=r"^\s*(apply|apply now|easy apply|quick apply)\s*$",
                          submit=r"^\s*(next|submit|submit application)\s*$"),
        # Jobs with screening questions send the Apply click to their own page; Hirist pre-fills most answers.
        form_url=r"/job/\d+/screening",
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

    async def discover(self, page: Page, urls: list[str], limit: int,
                       known: Container[str] = ()) -> list[JobStub]:
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
            searched = await self.spec.search(page, url, share) if self.spec.search else None
            if searched is None:
                await scroll(page, rounds=6)
            taken = 0
            listed = searched if searched is not None else await links(page, self.p.job_href)
            if searched is None and self.spec.listing_js:
                listed += [item for item in await page.evaluate(self.spec.listing_js)
                           if re.search(self.p.job_href, item["url"], re.IGNORECASE)]
            for link in listed:
                job_url = link["url"].split("?")[0]
                job_id = self.job_id(job_url)
                if job_id in found or job_id in known:
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
        if self.spec.form_url and re.search(self.spec.form_url, page.url):
            return page.locator("body")
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
        try:
            await button.click(timeout=8_000)
        except PlaywrightTimeout:
            # Instahyre lays an empty link over its Apply button, so the pointer can't reach it; the
            # button's own click handler still works.
            await button.evaluate("el => el.click()")
        if self.spec.form_url:
            try:
                await page.wait_for_url(re.compile(self.spec.form_url), timeout=10_000)
            except PlaywrightTimeout:
                pass  # applied on the spot, or the confirmation is on its way
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
        if await self._confirmed(page):
            await close_dialogs(page)
            return ApplyResult(Outcome.APPLIED)
        # Hirist often never renders the confirmation even though the application did go through.
        # Reloading the job page asks the site itself, which is the reliable answer.
        await close_dialogs(page)
        await goto(page, url)
        if await text_matching(page, self.p.done):
            return ApplyResult(Outcome.APPLIED)
        return ApplyResult(Outcome.FAILED, "clicked Apply, and the reloaded job page still doesn't show it as applied")

    async def _confirmed(self, page: Page) -> bool:
        """Wait a little for the site to say the application went through; some take a few seconds."""
        for _ in range(CONFIRM_WAIT * 2):
            if await text_matching(page, self.p.done):
                return True
            await asyncio.sleep(0.5)
        return False
