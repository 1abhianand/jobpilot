"""The browser: one Chrome window at a time, on a dedicated profile that remembers your site logins.

You log in yourself once (`jobpilot login`); the profile keeps the session cookies, so no
password is ever stored here. Every helper paces itself like a person would.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import BrowserSettings

log = logging.getLogger(__name__)

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]
CLOSE_BUTTONS = re.compile(r"^(dismiss|close|cancel|no thanks|not now|skip)$", re.IGNORECASE)
LOGIN_PATHS = re.compile(r"/(login|signin|sign-in|authwall|checkpoint|uas/login|challenge)\b", re.IGNORECASE)
LOGIN_HOSTS = ("account.ycombinator.com", "accounts.google.com")


class LoggedOut(RuntimeError):
    """A site sent the browser to its login or security-check page."""


def is_login_wall(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "") in LOGIN_HOSTS or bool(LOGIN_PATHS.search(parts.path))


LINKS_JS = """(pattern) => {
  const re = new RegExp(pattern, 'i');
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const url = a.href.split('#')[0];
    if (!re.test(url) || seen.has(url)) continue;
    seen.add(url);
    const card = a.closest('li, article, [class*="card" i], [class*="job" i]') || a;
    out.push({url, text: (a.innerText || '').trim().slice(0, 200), card: (card.innerText || '').trim().slice(0, 800)});
  }
  return out;
}"""

TEXT_JS = """(pattern) => {
  const re = new RegExp(pattern, 'i');
  const tags = 'button, a, span, div, p, li, h1, h2, h3, h4, [role=status], [role=alert]';
  for (const el of document.querySelectorAll(tags)) {
    if (el.childElementCount > 3) continue;
    // a status line is never a menu entry: skip navigation (Wellfound's sidebar has an "Applied" link)
    if (el.closest('nav, header, a[href]:not([href="#"])')) continue;
    const text = (el.innerText || '').trim();
    if (!text || text.length > 160 || !re.test(text)) continue;
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.height > 0) return text;
  }
  return null;
}"""


class Browser:
    def __init__(self, settings: BrowserSettings, profile_dir: Path, shots_dir: Path):
        self.settings = settings
        self.profile_dir = profile_dir
        self.shots_dir = shots_dir
        self._lock = asyncio.Lock()
        self._context: BrowserContext | None = None  # the open session, if any, so instant sends can borrow it
        self._borrowed = 0

    @asynccontextmanager
    async def session(self, headless: bool | None = None) -> AsyncIterator[Page]:
        headless = self.settings.headless if headless is None else headless
        size = {"viewport": {"width": 1440, "height": 900}} if headless else {"no_viewport": True}
        async with self._lock, async_playwright() as pw:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            for attempt in (1, 2):  # a busy machine can make Chrome slow to start
                try:
                    context = await pw.chromium.launch_persistent_context(
                        str(self.profile_dir),
                        channel=self.settings.channel or None,
                        headless=headless,
                        slow_mo=self.settings.slow_mo_ms,
                        args=LAUNCH_ARGS,
                        ignore_default_args=["--enable-automation"],
                        timeout=60_000,
                        **size,
                    )
                    break
                except PlaywrightTimeout:
                    if attempt == 2:
                        raise
                    log.warning("Chrome was slow to start; trying once more")
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(20_000)
                self._context = context
                yield page
            finally:
                self._context = None
                while self._borrowed:  # let an instant send in another tab finish first
                    await asyncio.sleep(0.5)
                await context.close()

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """A page right now: a new tab in the session that's already open (say, a site scan), or a session of
        its own when Chrome isn't running. Used to act the moment you approve, without waiting for the browser."""
        context = self._context
        if context is None:
            async with self.session() as page:
                yield page
            return
        self._borrowed += 1
        try:
            page = await context.new_page()
            page.set_default_timeout(20_000)
            try:
                yield page
            finally:
                await page.close()
        finally:
            self._borrowed -= 1

    async def screenshot(self, page: Page, label: str) -> str:
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60]
        path = self.shots_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.png"
        try:
            await page.screenshot(path=str(path))
        except Exception as exc:  # a screenshot must never hide the real failure
            log.warning("screenshot failed: %s", exc)
        return str(path)


async def pause(low: float = 0.8, high: float = 2.2) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeout:
        pass  # busy pages (LinkedIn) never go idle; the DOM is enough
    if is_login_wall(page.url) and not is_login_wall(url):
        raise LoggedOut(f"{urlsplit(url).hostname} asked you to log in again (landed on {page.url.split('?')[0]}). "
                        "Run `jobpilot login` and sign in.")
    await pause(1.2, 2.6)


async def scroll(page: Page, rounds: int = 5) -> None:
    for _ in range(rounds):
        await page.mouse.wheel(0, random.randint(700, 1200))
        await pause(0.6, 1.4)


async def type_text(box: Locator, text: str) -> None:
    """Replace a field's text; short texts are typed key by key, long ones pasted."""
    await box.click()
    await box.fill("")
    if len(text) > 400 or "\n" in text:
        await box.fill(text)
    else:
        await box.press_sequentially(text, delay=random.randint(18, 45))
    await pause(0.4, 1.0)


async def first_visible(*locators: Locator) -> Locator | None:
    for locator in locators:
        count = await locator.count()
        for index in range(min(count, 12)):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    return None


async def top_dialog(page: Page) -> Locator | None:
    dialogs = page.locator("[role='dialog'], [role='alertdialog'], [aria-modal='true'], dialog[open]")
    for index in reversed(range(await dialogs.count())):
        candidate = dialogs.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def close_dialogs(page: Page) -> None:
    for _ in range(3):
        dialog = await top_dialog(page)
        if dialog is None:
            return
        button = await first_visible(
            dialog.get_by_role("button", name=CLOSE_BUTTONS),
            dialog.locator("button[aria-label*='close' i], button[aria-label*='dismiss' i]"),
        )
        if button is not None:
            await button.click()
        else:
            await page.keyboard.press("Escape")
        await pause(0.5, 1.0)


async def links(page: Page, href_pattern: str) -> list[dict[str, Any]]:
    """Every distinct link whose absolute URL matches the pattern, with its link and card text."""
    return await page.evaluate(LINKS_JS, href_pattern)


async def text_matching(page: Page, pattern: str) -> str | None:
    """The first short visible text (a button, badge or status line) matching the pattern."""
    return await page.evaluate(TEXT_JS, pattern)


def tidy(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def page_text(page: Page, selectors: list[str], limit: int = 15_000) -> str:
    """Text of the first listed container that holds real content, else the whole page."""
    for selector in selectors:
        box = await first_visible(page.locator(selector))
        if box is not None:
            text = tidy(await box.inner_text())
            if len(text) > 200:
                return text[:limit]
    return tidy(await page.locator("body").inner_text())[:limit]
