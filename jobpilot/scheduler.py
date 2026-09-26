"""Runs each task on its own interval during working hours, while the Telegram bot listens.

One task runs at a time, so there is never more than one Chrome window. The bot keeps answering
your taps and commands while a long browser task is working.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from . import pipeline
from .approvals import esc
from .browser import LoggedOut
from .clock import WEEKDAYS, parse
from .config import SITES

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

TICK_SECONDS = 30
ALERT_EVERY = timedelta(hours=6)


@dataclass
class Task:
    name: str
    every: timedelta
    run: Callable[[App], Awaitable[str]]
    working_hours: bool = True


def build_tasks(app: App) -> list[Task]:
    s = app.settings
    tasks = [
        Task("cards", timedelta(minutes=1), pipeline.show_cards),
        Task("email", timedelta(minutes=1), pipeline.send_email_queue),
        Task("actions", timedelta(minutes=20), pipeline.send_browser_queue),
    ]
    if s.yc.enabled:
        tasks += [Task("yc-sync", timedelta(hours=24), pipeline.sync_yc),
                  Task("yc-prepare", timedelta(hours=3), pipeline.prepare_yc)]
    if s.scout.enabled:
        tasks += [Task("scout", timedelta(days=s.scout.every_days), pipeline.run_scout),
                  Task("scout-prepare", timedelta(hours=3), pipeline.prepare_scouted)]
    for name in SITES:
        if s.site(name).enabled:
            tasks.append(Task(f"site:{name}", timedelta(hours=s.site(name).every_hours),
                              partial(pipeline.run_site, name=name)))
    if s.linkedin.enabled:
        tasks.append(Task("connections", timedelta(hours=6), pipeline.check_connections))
    if app.mailbox is not None:
        tasks += [Task("inbox", timedelta(minutes=15), pipeline.check_inbox, working_hours=False),
                  Task("followups", timedelta(hours=12), pipeline.draft_followups)]
    tasks.append(Task("expire", timedelta(hours=6), pipeline.expire_cards, working_hours=False))
    return tasks


async def alert(app: App, key: str, text: str) -> None:
    """Tell you about a problem, at most once every few hours per kind of problem."""
    last = app.db.get(f"alert:{key}")
    if last and parse(last) + ALERT_EVERY > app.clock.now():
        return
    app.db.put(f"alert:{key}", app.clock.stamp())
    try:
        await app.bot.notify(esc(text))
    except Exception:
        log.exception("couldn't send an alert to Telegram")


async def run_task(app: App, task: Task) -> str:
    app.db.put(f"last:{task.name}", app.clock.stamp())  # stamped first, so a crashing task can't spin
    try:
        summary = await task.run(app)
    except LoggedOut as exc:
        log.warning("%s: %s", task.name, exc)
        await alert(app, f"login:{task.name}", f"🔒 {exc}")
        return ""
    except Exception as exc:
        log.exception("task %s failed", task.name)
        await alert(app, task.name, f"⚠️ {task.name} failed: {type(exc).__name__}: {exc}")
        return ""
    if summary:
        log.info("%s: %s", task.name, summary)
    return summary


def due(app: App, task: Task) -> bool:
    if task.working_hours and not app.clock.working():
        return False
    last = app.db.get(f"last:{task.name}")
    return last is None or parse(last) + task.every <= app.clock.now()


async def maybe_digest(app: App) -> None:
    here = app.clock.local()
    today = here.date().isoformat()
    if WEEKDAYS[here.weekday()] not in app.clock.days or here.time() < app.clock.end:
        return
    if app.db.get("digest_date") == today:
        return
    app.db.put("digest_date", today)
    await app.bot.notify(app.bot.digest())


async def tick(app: App, tasks: list[Task]) -> None:
    """One pass: run every task that is due, unless you've paused, then send the digest if it's time."""
    for task in tasks:
        if app.db.get("paused") == "1":
            return
        if due(app, task):
            await run_task(app, task)
    try:
        await maybe_digest(app)
    except Exception:
        log.exception("digest failed")


async def run_forever(app: App) -> None:
    tasks = build_tasks(app)
    await pipeline.recover(app)
    mode = "dry run: nothing is really sent" if app.settings.dry_run else "LIVE"
    await app.bot.notify(f"🚀 jobpilot started ({mode}). /status for today's numbers, /pause to stop everything.")
    listener = asyncio.create_task(app.bot.poll_forever())
    try:
        while True:
            await tick(app, tasks)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        listener.cancel()
