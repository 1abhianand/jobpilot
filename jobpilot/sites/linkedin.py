"""LinkedIn: Easy Apply, connection requests, messages after someone accepts, and people lookup.

Buttons are found by their accessible names, which LinkedIn changes far less often than its
CSS classes. Before connecting or messaging, the name on the profile must match the person we
meant to reach. In dry-run mode nothing that could send is ever clicked.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Container
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from playwright.async_api import Locator, Page

from ..browser import LoggedOut, close_dialogs, first_visible, goto, page_text, pause, scroll, text_matching, top_dialog, type_text
from ..forms import FormFiller
from .base import ApplyResult, JobPage, JobStub, Outcome

log = logging.getLogger(__name__)

JOB_IDS_JS = r"""() => {
  const ids = new Set();
  document.querySelectorAll('[data-job-id], [data-occludable-job-id]').forEach(el => {
    const id = el.getAttribute('data-job-id') || el.getAttribute('data-occludable-job-id');
    if (/^\d+$/.test(id || '')) ids.add(id);
  });
  document.querySelectorAll('a[href*="/jobs/view/"]').forEach(a => {
    const m = a.href.match(/\/jobs\/view\/(\d+)/);
    if (m) ids.add(m[1]);
  });
  return [...ids];
}"""

SCROLL_LIST_JS = r"""() => {
  const card = document.querySelector('[data-occludable-job-id], [data-job-id]');
  let box = card && card.parentElement;
  while (box && box.scrollHeight <= box.clientHeight + 10) box = box.parentElement;
  (box || document.scrollingElement).scrollBy(0, 900);
}"""

PEOPLE_JS = r"""() => {
  const out = [];
  const seen = new Set();
  const buttons = /^(connect|message|follow|pending|view profile|\d+(st|nd|rd|th)\+?|· \d+(st|nd|rd|th))$/i;
  for (const a of document.querySelectorAll('main a[href*="/in/"]')) {
    const url = a.href.split('?')[0].replace(/\/$/, '');
    if (seen.has(url)) continue;
    const card = a.closest('li') || a.parentElement;
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(s => s && !buttons.test(s));
    if (!lines.length || /linkedin member/i.test(lines[0])) continue;
    seen.add(url);
    out.push({url, name: lines[0], headline: lines.slice(1, 4).join(' · ')});
  }
  return out;
}"""

COMPANIES_JS = r"""() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/company/"]')) {
    const url = a.href.split('?')[0].replace(/\/$/, '');
    const name = (a.innerText || '').trim().split('\n')[0];
    if (!name || seen.has(url)) continue;
    seen.add(url);
    out.push({url, name});
  }
  return out;
}"""

ERRORS_JS = r"""(root) => {
  const out = [];
  // 2026 date pickers have empty <label>s: name the field from the caption beside it ("From"), its placeholder
  // option ("Month") and the step's heading ("Work experience"), so the question you get says what's missing
  const nearby = field => {
    const step = ((root.querySelector('h3') || root.querySelector('h2') || {}).innerText || '').trim().split('\n')[0];
    for (let box = field.parentElement, i = 0; box && i < 6; box = box.parentElement, i++) {
      const caption = [...box.querySelectorAll('p, legend, h4, span')].map(e => (e.innerText || '').trim())
        .find(t => t && t.length < 80 && !/required|invalid|please|enter a|must be/i.test(t));
      if (!caption) continue;
      const hint = field.tagName === 'SELECT' && field.options.length ? ` (${field.options[0].text.trim()})` : '';
      return [step, caption + hint].filter(Boolean).join(' · ');
    }
    return '';
  };
  const errors = root.querySelectorAll('.artdeco-inline-feedback--error, [data-test-form-element-error-messages], [role=alert]');
  for (const err of errors) {
    const box = err.getBoundingClientRect();
    const message = (err.innerText || '').trim();
    if (!box.width || !box.height || !message) continue;
    const holder = err.closest('[data-test-form-element], .fb-dash-form-element, .jobs-easy-apply-form-element, fieldset') || err.parentElement;
    const label = holder && holder.querySelector('label, legend');
    out.push({label: label ? label.innerText.trim().split('\n')[0] : '', message});
  }
  // 2026 layout: the message sits in the element named by the field's aria-describedby ("Invalid input")
  for (const field of root.querySelectorAll('input, textarea, select')) {
    const ids = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    const said = ids.map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim();
    if (!/invalid|required|enter a|must be|please/i.test(said)) continue;
    const own = field.labels && field.labels.length ? field.labels[0].innerText.trim().split('\n')[0] : '';
    out.push({label: own || nearby(field), message: said.split('\n')[0], value: field.value || '',
              jp: field.getAttribute('data-jp-field')});
  }
  return out;
}"""

CHATS_JS = r"""() => {
  // Every visible chat box, with who its window is addressed to. Since 2026 LinkedIn draws its chats inside
  // a shadow root (#interop-outlet), so open shadow roots are searched too. Chats left open come back on every
  // page, so the box on screen is not necessarily the one with the person we came to message.
  const roots = [document];
  for (let i = 0; i < roots.length; i++)
    for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  for (const root of roots)
    root.querySelectorAll('[data-jp-chat], [data-jp-window]').forEach(el => {
      el.removeAttribute('data-jp-chat'); el.removeAttribute('data-jp-window'); });
  const visible = e => { const r = e.getBoundingClientRect(); return r.width && r.height; };
  const boxes = [...new Set(roots.flatMap(r => [...r.querySelectorAll(
    "div.msg-form__contenteditable[contenteditable='true'], [role='textbox'][contenteditable='true']")]))].filter(visible);
  const generic = /^(new message|messaging|compose message)$/i;
  const namesIn = win => {
    // an open conversation: "Close your conversation with Arushi Gandhi"
    const closers = [...win.querySelectorAll('button')]
      .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())
      .filter(t => /^close your conversation with\s/i.test(t))
      .map(t => t.replace(/^close your conversation with\s*/i, '').trim());
    // a new message: one "Remove Priyashaa Naayan" chip per recipient
    const chips = [...win.querySelectorAll("button[aria-label^='Remove ' i]")]
      .map(b => b.getAttribute('aria-label').replace(/^remove\s+/i, '').trim())
      .filter(n => n && !/attachment|file|image|draft/i.test(n));
    if (closers.length || chips.length) return [...closers, ...chips];
    const heading = [...win.querySelectorAll('h2, h3')].map(h => h.innerText.trim().split('\n')[0])
      .find(t => t && !generic.test(t));
    return heading ? [heading] : [];
  };
  return boxes.map((box, i) => {
    box.setAttribute('data-jp-chat', i);
    let win = box.closest('.msg-convo-wrapper, .msg-overlay-conversation-bubble'), names = [];
    if (win) names = namesIn(win);
    else for (win = box.parentElement; win && win !== document.body; win = win.parentElement) {
      names = namesIn(win);
      if (names.length) break;
    }
    if (!win || win === document.body) return {index: i, names: [], profiles: []};
    win.setAttribute('data-jp-window', i);
    const profiles = [...new Set([...win.querySelectorAll("a[href*='/in/']")]
      .map(a => ((a.getAttribute('href') || '').match(/\/in\/([^/?#]+)/) || [])[1])
      .filter(Boolean).map(slug => decodeURIComponent(slug).toLowerCase()))];
    return {index: i, names, profiles};
  });
}"""

NEXT_STEP = re.compile(r"^(continue to next step|next|review your application|review)$", re.IGNORECASE)
SUBMIT = re.compile(r"^submit application$", re.IGNORECASE)
EASY_APPLY = re.compile(r"^easy apply\b", re.IGNORECASE)
APPLIED_BEFORE = re.compile(r"\bApplied \d+ (second|minute|hour|day|week|month)s? ago\b|\bApplication submitted\b")
WEEKLY_LIMIT = re.compile(r"weekly invitation limit|reached the weekly limit|out of invitations", re.IGNORECASE)
NOTE_UPSELL = re.compile(r"personali[sz]ed invitations|personali[sz]e (your )?invit|premium", re.IGNORECASE)
# Pages LinkedIn shows instead of what was asked for when it is unhappy with the account. They don't live at a
# login URL, so they're spotted by their wording; any of them stops all LinkedIn activity.
WARNING_PAGE = re.compile(r"your account (has been |is )?(temporarily )?restricted|we('ve| have) restricted your account"
                          r"|unusual activity (on|from) your account|commercial use limit"
                          r"|monthly limit for (profile )?searches|let's do a quick security check", re.IGNORECASE)
FIRST_DEGREE = re.compile(r"(^|\s|·)1st\b|\b1st degree connection\b")


class Reach(StrEnum):
    SENT = "sent"
    DRY_RUN = "dry_run"
    PENDING = "already_pending"
    CONNECTED = "already_connected"
    NOTE_QUOTA = "note_quota"
    WEEKLY_LIMIT = "weekly_limit"
    EMAIL_REQUIRED = "email_required"
    NAME_MISMATCH = "name_mismatch"
    NOT_CONNECTED = "not_connected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass
class Member:
    name: str
    connected: bool
    pending: bool


def _plain(text: str) -> list[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z]+", ascii_text) if len(t) > 1 and t not in ("dr", "mr", "ms", "mrs")]


def names_match(expected: str, shown: str) -> bool:
    """Same first name (or one a prefix of the other, like Adit/Aditya) and a compatible last name."""
    want, got = _plain(expected), _plain(shown)
    if not want or not got:
        return False
    first_ok = want[0] == got[0] or (min(len(want[0]), len(got[0])) >= 3 and
                                     (want[0].startswith(got[0]) or got[0].startswith(want[0])))
    last_ok = len(want) == 1 or len(got) == 1 or want[-1] in got or got[-1] in want
    return first_ok and last_ok


def with_start(url: str, start: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["start"] = str(start)
    return urlunsplit(parts._replace(query=urlencode(query)))


def profile_url(url: str) -> str:
    """Canonical profile URL. Country hosts (in.linkedin.com) fail to load for a logged-in member, so use www."""
    url = re.sub(r"^https?://([a-z]{2,3}\.)?linkedin\.com", "https://www.linkedin.com", url.strip(), flags=re.IGNORECASE)
    return url.split("?")[0].rstrip("/") + "/"


def profile_slug(url: str) -> str:
    """'https://in.linkedin.com/in/Priyashaa-Naayan-690997175?x=1' -> 'priyashaa-naayan-690997175'"""
    found = re.search(r"/in/([^/?#]+)", url or "")
    return unquote(found.group(1)).lower() if found else ""


def title_and_company(page_title: str | None) -> tuple[str | None, str | None]:
    """The tab title, which LinkedIn keeps when it drops the h1 from the page.

    'Voice AI Engineer | Revspot | LinkedIn'                -> ('Voice AI Engineer', 'Revspot')
    'Revspot hiring Voice AI Engineer in Bengaluru | …'     -> ('Voice AI Engineer', 'Revspot')
    """
    parts = [p.strip() for p in (page_title or "").split("|")]
    parts = [p for p in parts if p and p.lower() != "linkedin"]
    if not parts:
        return None, None
    hiring = re.match(r"^(.+?)\s+hiring\s+(.+?)(?:\s+in\s+.+)?$", parts[0], re.IGNORECASE)
    if hiring:
        return hiring.group(2).strip(), hiring.group(1).strip()
    return parts[0], (parts[1] if len(parts) > 1 else None)


async def open_page(page: Page, url: str) -> None:
    """goto, then stop everything if LinkedIn shows a restriction or warning page instead."""
    await goto(page, url)
    top = f"{await page.title()}\n{(await page.locator('body').inner_text())[:2000]}"
    if found := WARNING_PAGE.search(top):
        raise LoggedOut(f"www.linkedin.com showed a warning on your account: \"{found.group(0)}\" ({page.url.split('?')[0]})")


class LinkedIn:
    name = "linkedin"
    label = "LinkedIn"
    login_url = "https://www.linkedin.com/login"
    needs_note = False

    def __init__(self, filler: FormFiller):
        self.filler = filler
        self.default_searches: list[str] = []  # LinkedIn needs your own search URLs

    # -- jobs -------------------------------------------------------------------------

    async def discover(self, page: Page, urls: list[str], limit: int,
                       known: Container[str] = ()) -> list[JobStub]:
        """New job ids from your searches. Jobs already in `known` are scrolled past, not counted, so each run
        reaches further down the results instead of re-reading the same top 25."""
        ids: list[str] = []
        for index, url in enumerate(urls):
            try:  # one hung search page once blocked everything for 5 hours; give each search 4 minutes
                await asyncio.wait_for(self._search(page, url, ids, limit, len(urls) - index, known), timeout=240)
            except asyncio.TimeoutError:
                log.warning("LinkedIn search took over 4 minutes, moving on: %s", url[:120])
            except LoggedOut:
                raise
            except Exception as exc:  # one page that won't load mustn't cancel the jobs already waiting to be applied to
                log.warning("LinkedIn search failed (%s), moving on: %s", type(exc).__name__, url[:120])
        return [JobStub(i, f"https://www.linkedin.com/jobs/view/{i}/") for i in ids[:limit]]

    async def _search(self, page: Page, url: str, ids: list[str], limit: int, urls_left: int,
                      known: Container[str] = ()) -> None:
        # Each search gets a fair share of what's left, so the first one can't crowd out the rest.
        share = -(-(limit - len(ids)) // urls_left)
        taken = 0
        for start_at in range(0, 250, 25):
            if taken >= share:
                break
            await open_page(page, with_start(url, start_at))
            for _ in range(6):  # scroll the results list the way a person would, so every card loads
                await page.evaluate(SCROLL_LIST_JS)
                await pause(0.8, 1.6)
            listed = await page.evaluate(JOB_IDS_JS)
            if not listed:
                break  # past the last page of results
            fresh = [i for i in listed if i not in ids and i not in known]
            if not fresh:
                await pause(1.0, 2.5)
                continue  # a page of jobs already seen: look one page further
            fresh = fresh[: share - taken]
            ids.extend(fresh)
            taken += len(fresh)

    async def _easy_apply(self, page: Page) -> Locator | None:
        return await first_visible(page.get_by_role("button", name=EASY_APPLY))

    async def read(self, page: Page, url: str) -> JobPage:
        await open_page(page, url)
        more = await first_visible(page.get_by_role("button", name=re.compile(r"^(see more|…\s*more|show more)",
                                                                                 re.IGNORECASE)))
        if more is not None:
            await more.click()
        heading = await first_visible(page.locator("h1"))
        title = (await heading.inner_text()).strip() if heading else None
        company_link = await first_visible(page.locator("main a[href*='/company/']"))
        company = (await company_link.inner_text()).strip() or None if company_link else None
        if not title:  # LinkedIn dropped the h1 from job pages; the tab title still carries the role
            title, from_tab = title_and_company(await page.title())
            company = company or from_tab
        top = (await page.locator("main").first.inner_text())[:2500] if await page.locator("main").count() else ""
        description = await page_text(page, ["#job-details", ".jobs-description__content", ".jobs-description",
                                             "article", "main"])
        applied = APPLIED_BEFORE.search(top) is not None
        can_apply = not applied and await self._easy_apply(page) is not None
        return JobPage(title=title, company=company, location=None, description=description,
                       can_apply=can_apply, applied=applied)

    async def _modal(self, page: Page) -> Locator | None:
        for _ in range(10):
            modal = await first_visible(page.locator(".jobs-easy-apply-modal"), page.locator("[role='dialog']"),
                                        page.locator("dialog[open]"), page.locator("dialog"))  # a <dialog> since 2026
            if modal is not None:
                return modal
            await pause(0.5, 0.8)
        return None

    async def _numbers_only(self, modal: Locator, errors: list[dict]) -> bool:
        """A field rejected "45 days" often wants just "45": retry with the number, and remember it."""
        fixed = False
        for error in errors:
            value = error.get("value") or ""
            number = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
            if error.get("jp") is None or not number or number.group() == value.strip():
                continue
            await modal.locator(f'[data-jp-field="{error["jp"]}"]').first.fill(number.group())
            if error.get("label"):
                self.filler.book.db.learn_answer(error["label"], number.group(), "you")
            fixed = True
        return fixed

    async def _discard(self, page: Page) -> None:
        modal = await top_dialog(page)
        if modal is None:
            return
        dismiss = await first_visible(modal.get_by_role("button", name=re.compile(r"^dismiss$", re.IGNORECASE)),
                                      modal.locator("button[aria-label*='Dismiss' i]"))
        if dismiss is None:
            await page.keyboard.press("Escape")
        else:
            await dismiss.click()
        await pause(0.6, 1.2)
        confirm = await first_visible(page.get_by_role("button", name=re.compile(r"^discard$", re.IGNORECASE)))
        if confirm is not None:
            await confirm.click()
            await pause(0.5, 1.0)

    async def apply(self, page: Page, url: str, note: str | None, dry_run: bool,
                    job: str | None = None) -> ApplyResult:
        info = await self.read(page, url)
        if info.applied:
            return ApplyResult(Outcome.ALREADY)
        button = await self._easy_apply(page)
        if button is None:
            return ApplyResult(Outcome.UNAVAILABLE, "no Easy Apply button (the job applies on the company site)")
        await button.click()  # opens the form; nothing is submitted until "Submit application"
        await pause(1.5, 2.5)
        modal = await self._modal(page)
        if modal is None:
            return ApplyResult(Outcome.FAILED, "the Easy Apply window didn't open")
        label = job or " at ".join(x for x in (info.title, info.company) if x) or None
        stuck = 0
        for _ in range(15):
            missing = await self.filler.fill(modal, label, include_optional=stuck > 0)
            if missing is not None:
                await self._discard(page)
                return ApplyResult(Outcome.NEEDS_INPUT, question=missing.label, options=missing.options)
            submit = await first_visible(modal.get_by_role("button", name=SUBMIT))
            if submit is not None:
                if dry_run:
                    await self._discard(page)
                    return ApplyResult(Outcome.DRY_RUN, "filled every step; stopped before Submit")
                await submit.click()
                await pause(2.0, 3.5)
                if await text_matching(page, r"application (was )?sent|application submitted|applied to"):
                    await close_dialogs(page)
                    return ApplyResult(Outcome.APPLIED)
                return ApplyResult(Outcome.FAILED, "clicked Submit but saw no confirmation")
            step = await first_visible(modal.get_by_role("button", name=NEXT_STEP))
            if step is None:
                await self._discard(page)
                return ApplyResult(Outcome.FAILED, "no Next, Review or Submit button in the form")
            before = (await modal.inner_text())[:300]
            await step.click()
            await pause(1.2, 2.2)
            errors = await modal.evaluate(ERRORS_JS)
            if errors and await self._numbers_only(modal, errors):
                step = await first_visible(modal.get_by_role("button", name=NEXT_STEP))
                if step is not None:
                    await step.click()
                    await pause(1.2, 2.2)
                errors = await modal.evaluate(ERRORS_JS)
            if not errors and (await modal.inner_text())[:300] == before:
                errors = [{"label": "", "message": "the page didn't change after Next"}]  # an error we can't read
            if errors:
                stuck += 1
                if stuck >= 2:
                    await self._discard(page)
                    first = errors[0]
                    return ApplyResult(Outcome.NEEDS_INPUT, detail=first["message"],
                                       question=first["label"] or first["message"])
            else:
                stuck = 0
        await self._discard(page)
        return ApplyResult(Outcome.FAILED, "the form had more steps than expected")

    # -- people ---------------------------------------------------------------------------

    @staticmethod
    def _missing(page: Page) -> str:
        if "/404" in page.url:
            return "this LinkedIn profile doesn't exist any more (the link we had is out of date)"
        return "profile didn't load"

    async def _top_card(self, page: Page) -> Locator:
        # The 2026 layout puts the whole profile in one "Primary content" region with the name as an h2.
        card = page.locator("main section, main [role='region']").filter(has=page.locator("h1, h2")).first
        return card if await card.count() else page.locator("main").first

    async def member(self, page: Page, url: str) -> Member | None:
        await open_page(page, profile_url(url))
        heading = None
        for _ in range(3):  # the profile renders after the page itself has loaded
            heading = await first_visible(page.locator("main h1"), page.locator("main a[href*='/in/'] h2"),
                                          page.locator("main h2"))
            if heading is not None:
                break
            await pause(1.5, 2.5)
        if heading is None:
            return None
        top = await self._top_card(page)
        text = (await top.inner_text())[:500]  # name, degree and headline come first; posts further down mention "1st" too
        pending_name = re.compile(r"^pending", re.IGNORECASE)  # a button before 2026, a link since
        pending = await first_visible(top.get_by_role("button", name=pending_name), top.get_by_role("link", name=pending_name))
        return Member(name=(await heading.inner_text()).strip(), connected=FIRST_DEGREE.search(text) is not None,
                      pending=pending is not None)

    async def connect(self, page: Page, url: str, expected: str, note: str, dry_run: bool) -> tuple[Reach, str]:
        who = await self.member(page, url)
        if who is None:
            return Reach.UNAVAILABLE, self._missing(page)
        if not names_match(expected, who.name):
            return Reach.NAME_MISMATCH, f"the profile belongs to {who.name!r}"
        if who.pending:
            return Reach.PENDING, ""
        if who.connected:
            return Reach.CONNECTED, ""
        top = await self._top_card(page)
        invite = re.compile(rf"^invite {re.escape(who.name)} to connect$", re.IGNORECASE)
        css_name = who.name.replace("\\", "\\\\").replace('"', '\\"')
        button = await first_visible(top.get_by_role("button", name=invite),
                                     top.get_by_role("button", name=re.compile(r"^connect$", re.IGNORECASE)))
        opened_menu = False
        if button is None:
            more = await first_visible(top.get_by_role("button", name=re.compile(r"^more( actions)?$", re.IGNORECASE)))
            if more is None:
                return Reach.UNAVAILABLE, "no Connect or More button on the profile"
            await more.click()
            opened_menu = True
            await pause(0.6, 1.2)
            button = await first_visible(page.get_by_role("button", name=invite),
                                         page.get_by_role("menuitem", name=invite),
                                         page.locator(f'[aria-label="Invite {css_name} to connect" i]'),
                                         page.get_by_role("menuitem", name=re.compile(r"^connect$", re.IGNORECASE)))
        if button is None:
            if opened_menu:
                await page.keyboard.press("Escape")
            return Reach.UNAVAILABLE, "LinkedIn doesn't offer Connect for this profile"
        if dry_run:
            if opened_menu:
                await page.keyboard.press("Escape")
            return Reach.DRY_RUN, "found the Connect button; stopped before clicking it"
        href = await button.get_attribute("href")
        if href and "invite" in href:
            # The 2026 Connect is a link to the invite window; open it rather than click through overlays.
            await open_page(page, urljoin("https://www.linkedin.com/", href))
        else:
            await button.click()
        await pause(1.0, 2.0)
        dialog = await top_dialog(page)
        if dialog is None:
            if (after := await self.member(page, url)) and after.pending:
                return Reach.SENT, "sent without a note (LinkedIn skipped the note step)"
            return Reach.FAILED, "the invitation window didn't open"
        text = await dialog.inner_text()
        if WEEKLY_LIMIT.search(text):
            await close_dialogs(page)
            return Reach.WEEKLY_LIMIT, ""
        if await first_visible(dialog.locator("input[type='email'], input[name='email']")) is not None:
            await close_dialogs(page)
            return Reach.EMAIL_REQUIRED, "LinkedIn wants their email address to connect"
        if note:
            add = await first_visible(dialog.get_by_role("button", name=re.compile(r"^add a (free )?note$", re.IGNORECASE)))
            if add is not None:
                await add.click()
                await pause(0.8, 1.5)
                dialog = await top_dialog(page) or dialog
            box = await first_visible(dialog.locator("textarea"))
            if box is None:
                await close_dialogs(page)
                return Reach.NOTE_QUOTA, "LinkedIn won't take a note right now (free-plan limit)"
            await type_text(box, note)
            send = await first_visible(dialog.get_by_role("button", name=re.compile(r"^send( invitation| now)?$",
                                                                                     re.IGNORECASE)))
        else:
            send = await first_visible(dialog.get_by_role(
                "button", name=re.compile(r"^send without a note$|^send( now| invitation)?$", re.IGNORECASE)))
        if send is None or await send.is_disabled():
            await close_dialogs(page)
            return Reach.FAILED, "no usable Send button in the invitation window"
        await send.click()
        await pause(1.5, 3.0)
        if (after_dialog := await top_dialog(page)) is not None:
            after_text = await after_dialog.inner_text()
            if WEEKLY_LIMIT.search(after_text):
                await close_dialogs(page)
                return Reach.WEEKLY_LIMIT, ""
            if note and NOTE_UPSELL.search(after_text):
                await close_dialogs(page)
                return Reach.NOTE_QUOTA, "LinkedIn won't take a note right now (free-plan limit)"
        if await text_matching(page, r"^invitation sent") or ((after := await self.member(page, url)) and after.pending):
            return Reach.SENT, ""
        return Reach.FAILED, "clicked Send but couldn't confirm; check the profile"

    async def message(self, page: Page, url: str, expected: str, text: str, dry_run: bool,
                      attachment: Path | None = None) -> tuple[Reach, str]:
        who = await self.member(page, url)
        if who is None:
            return Reach.UNAVAILABLE, self._missing(page)
        if not names_match(expected, who.name):
            return Reach.NAME_MISMATCH, f"the profile belongs to {who.name!r}"
        if not who.connected:
            return Reach.NOT_CONNECTED, "you aren't connected yet"
        top = await self._top_card(page)
        button = await first_visible(top.get_by_role("button", name=re.compile(r"^message\b", re.IGNORECASE)),
                                     top.get_by_role("link", name=re.compile(r"^message\b", re.IGNORECASE)))
        if button is None:
            return Reach.UNAVAILABLE, "no Message button on the profile"
        if dry_run:
            return Reach.DRY_RUN, "found the Message button; stopped before opening the chat"
        # Chats left open come back on every page. On 2026-10-12 one stayed open and took five messages
        # meant for other people, so close them all and only ever type into the chat named for this person.
        await self._close_chats(page)
        await button.click()
        await pause(1.5, 2.5)
        chat = await self._chat_with(page, expected, url)
        if isinstance(chat, str):
            await self._close_chats(page)
            return Reach.FAILED, chat
        box, window = chat
        snippet = " ".join(text.split())[:40]
        if snippet in " ".join((await window.inner_text()).split()):
            await self._close_chats(page)
            return Reach.SENT, "already in the conversation, so it wasn't sent again"
        await box.click()
        await box.fill(text)  # fill, not key presses: Enter may send mid-message
        await pause(0.8, 1.5)
        attached = ""
        if attachment is not None:
            chooser = window.locator("input[type='file']")
            if await chooser.count():
                await chooser.first.set_input_files(str(attachment))
                await pause(3.0, 5.0)  # the upload finishes before Send is usable again
                attached = f"attached {attachment.name}"
            else:
                attached = "couldn't find the attach button, so the message went without your resume"
        send = await first_visible(window.locator("button.msg-form__send-button"),
                                   window.get_by_role("button", name=re.compile(r"^send$", re.IGNORECASE)))
        if send is None or await send.is_disabled():
            await box.fill("")
            return Reach.FAILED, "the chat's Send button is unavailable"
        await send.click()
        await pause(1.5, 2.5)
        try:
            sent = await self._delivered(page, box, window, snippet, expected, url)
            await self._close_chats(page)
        except Exception:
            sent = False
        # Send was clicked: an unconfirmed message must never be retried, or the person gets it twice.
        return Reach.SENT, attached if sent else "Send was clicked but LinkedIn didn't confirm it; check the chat"

    async def _delivered(self, page: Page, box: Locator, window: Locator, snippet: str, expected: str,
                         url: str) -> bool:
        """The message shows in their conversation. A new-message draft turns into a conversation once sent,
        so when the window we typed into is gone, look for the conversation with them again."""
        try:
            if await window.count() and not (await box.inner_text()).strip() and \
                    snippet in " ".join((await window.inner_text()).split()):
                return True
        except Exception:
            pass
        await pause(1.0, 2.0)
        again = await self._chat_with(page, expected, url)
        return not isinstance(again, str) and snippet in " ".join((await again[1].inner_text()).split())

    @staticmethod
    async def _close_chats(page: Page) -> None:
        for _ in range(12):
            # by accessible name, which covers an aria-label or the button's own (visually hidden) text
            close = await first_visible(page.get_by_role("button", name=re.compile(r"^close your", re.IGNORECASE)))
            if close is None:
                return
            try:
                await close.click()
            except Exception:
                return
            await pause(0.3, 0.7)

    @staticmethod
    async def _chat_with(page: Page, expected: str, url: str) -> tuple[Locator, Locator] | str:
        """The one open chat box addressed to `expected` alone, or why there isn't exactly one.

        Its window must name only them (a group chat or someone else's chat is refused), and any profile links
        in it must include theirs, so a namesake's chat can't pass either."""
        chats: list[dict] = []
        for _ in range(3):  # the chat window renders a moment after the click
            chats = await page.evaluate(CHATS_JS)
            if chats:
                break
            await pause(1.0, 2.0)
        if not chats:
            return "the chat box didn't open"
        slug = profile_slug(url)
        ours = [c for c in chats if c["names"] and all(names_match(expected, n) for n in c["names"])
                and (not c["profiles"] or slug in c["profiles"])]
        if len(ours) != 1:
            seen = ", ".join(sorted({n for c in chats for n in c["names"]})) or "an unnamed chat"
            return f"couldn't find exactly one chat with {expected} (open: {seen}); nothing was typed"
        index = ours[0]["index"]
        return page.locator(f"[data-jp-chat='{index}']"), page.locator(f"[data-jp-window='{index}']")

    async def company_url(self, page: Page, company: str) -> str | None:
        await open_page(page, f"https://www.linkedin.com/search/results/companies/?keywords={quote(company)}")
        wanted = " ".join(_plain(company))
        for result in await page.evaluate(COMPANIES_JS):
            if " ".join(_plain(result["name"])) == wanted:
                return result["url"]
        return None

    async def people(self, page: Page, company_url: str,
                     keywords: tuple[str, ...] = ("founder", "cto", "engineering", "recruiter")) -> list[dict]:
        base = company_url.split("?")[0].rstrip("/")
        found: dict[str, dict] = {}
        for keyword in keywords:
            await open_page(page, f"{base}/people/?keywords={quote(keyword)}")
            await scroll(page, rounds=2)
            for person in await page.evaluate(PEOPLE_JS):
                found.setdefault(person["url"], person)
            if len(found) >= 15:
                break
        return list(found.values())
