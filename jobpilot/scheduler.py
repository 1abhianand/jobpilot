"""Runs each task on its own interval during working hours, while the Telegram bot listens.

One task runs at a time, so there is never more than one Chrome window. The bot keeps answering
your taps and commands while a long browser task is working. When nothing is due, the sites take
turns (the one run longest ago goes next), so the browser never sits idle during working hours.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from . import dashboard, pipeline
from .approvals import esc
from .browser import LoggedOut
from .clock import WEEKDAYS, parse
from .config import SITES

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

TICK_SECONDS = 30
# Quick tasks never open Chrome, so they run in their own loop and are never stuck behind a long site run.
QUICK = ("cards", "email", "update", "inbox", "expire", "dashboard")
ALERT_EVERY = timedelta(hours=6)
TASK_LIMIT = timedelta(minutes=60)  # no single task may hold the browser longer than this
SITE_REST = timedelta(minutes=30)  # taking turns never runs the same site again sooner than this
RESTART = "restart"  # kv flag: finish the task in hand, then exit so systemd starts the new code


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
        Task("actions", timedelta(minutes=1), pipeline.send_browser_queue),
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
    tasks.append(Task("update", timedelta(hours=2), pipeline.send_update))
    if dashboard.ready(app):
        tasks.append(Task("dashboard", timedelta(minutes=30), dashboard.publish))
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
        summary = await asyncio.wait_for(task.run(app), timeout=TASK_LIMIT.total_seconds())
    except asyncio.TimeoutError:
        log.warning("%s ran past %s and was stopped; it runs again next time", task.name, TASK_LIMIT)
        return ""
    except LoggedOut as exc:
        log.warning("%s: %s", task.name, exc)
        if "linkedin.com" not in str(exc):
            await alert(app, f"login:{task.name}", f"🔒 {exc}")
        elif pipeline.lock_linkedin(app, str(exc)):
            await app.bot.notify(pipeline.linkedin_stopped(str(exc)))
        return ""
    except Exception as exc:
        log.exception("task %s failed", task.name)
        await alert(app, task.name, f"⚠️ {task.name} failed: {type(exc).__name__}: {exc}")
        return ""
    if summary:
        log.info("%s: %s", task.name, summary)
    return summary


def linkedin_resting(app: App, task: Task) -> bool:
    """Right after a connection request or message, LinkedIn gets a break; other sites use the time.
    After a security check it stays untouched until you log in again."""
    if task.name not in ("site:linkedin", "connections"):
        return False
    return (pipeline.linkedin_resting(app) or bool(app.db.get(pipeline.LINKEDIN_LOCK))
            or not app.clock.send_hours())  # LinkedIn is only used in the daytime, like a person would


def due(app: App, task: Task) -> bool:
    if task.working_hours and not app.clock.working():
        return False
    if linkedin_resting(app, task):
        return False
    last = app.db.get(f"last:{task.name}")
    return last is None or parse(last) + task.every <= app.clock.now()


def next_site(app: App, tasks: list[Task]) -> Task | None:
    """The site run longest ago that has rested a while, to fill time when nothing is due."""
    if not app.clock.working():
        return None
    # LinkedIn only runs on its own schedule: extra turns would mean many more page loads than a person makes
    rested = [t for t in tasks if t.name.startswith("site:") and t.name != "site:linkedin"
              and (not (last := app.db.get(f"last:{t.name}")) or parse(last) + SITE_REST <= app.clock.now())]
    return min(rested, key=lambda t: app.db.get(f"last:{t.name}") or "", default=None)


def stopping(app: App) -> bool:
    return app.db.get("paused") == "1" or app.db.get(RESTART) == "1"


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


async def work_pass(app: App, tasks: list[Task]) -> bool:
    """Run every due browser task; if none was due, give the next site its turn. True if anything ran."""
    ran = busy = False
    for task in tasks:
        if stopping(app):
            return ran
        if due(app, task):
            busy = bool(await run_task(app, task)) or busy  # "" = nothing to do yet, e.g. a send gap
            ran = True
    if not busy and not stopping(app) and (task := next_site(app, tasks)):
        await run_task(app, task)
        ran = True
    return ran


async def _quick_loop(app: App, tasks: list[Task]) -> None:
    while True:
        await tick(app, tasks)
        await asyncio.sleep(TICK_SECONDS)


async def _work_loop(app: App, tasks: list[Task]) -> None:
    while app.db.get(RESTART) != "1":  # checked only between tasks, so a restart never cuts one off
        if not await work_pass(app, tasks):
            await asyncio.sleep(TICK_SECONDS)
    log.info("restarting between tasks, as asked")


async def run_forever(app: App) -> None:
    tasks = build_tasks(app)
    quick = [t for t in tasks if t.name in QUICK]
    slow = [t for t in tasks if t.name not in QUICK]
    app.bot.tasks = [t.name for t in tasks]
    app.db.put(RESTART, "")
    await pipeline.recover(app)
    mode = "dry run: nothing is really sent" if app.settings.dry_run else "LIVE"
    app.db.put("menu_commands", "")  # re-send the / menu on every start, in case it was changed in Telegram
    await app.bot.register_commands()
    await app.bot.notify(f"🚀 jobpilot started ({mode}). /status for today's numbers, /applied and /sent for "
                         "what went out, /pause to stop everything. Type / for every command.")
    helpers = [asyncio.create_task(app.bot.poll_forever()), asyncio.create_task(_quick_loop(app, quick))]
    try:
        await _work_loop(app, slow)
    finally:
        for helper in helpers:
            helper.cancel()
