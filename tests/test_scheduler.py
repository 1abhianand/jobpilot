from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobpilot.browser import LoggedOut
from jobpilot.mail import Mailbox
from jobpilot.scheduler import Task, build_tasks, due, maybe_digest, run_task, tick


async def nothing(app) -> str:
    return ""


def test_task_list_follows_settings(app):
    app.settings.hirist.enabled = False
    names = [t.name for t in build_tasks(app)]
    assert "site:linkedin" in names and "site:hirist" not in names
    assert "inbox" not in names  # no Gmail configured
    app.mailbox = Mailbox("a@b.c", "pw", "A")
    assert {"inbox", "followups"} <= {t.name for t in build_tasks(app)}


def test_due_respects_working_hours_and_intervals(app):
    task = Task("site:x", timedelta(hours=6), nothing)
    anytime = Task("inbox", timedelta(minutes=15), nothing, working_hours=False)
    assert due(app, task)
    app.db.put("last:site:x", app.clock.ago(hours=2))
    assert not due(app, task)
    app.clock.moment += timedelta(hours=5)
    assert due(app, task)
    app.clock.moment = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)  # Sunday
    assert not due(app, task) and due(app, anytime)
    app.clock.moment = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)  # 22:30 IST
    assert not due(app, task)


async def test_failures_alert_once_per_window(app):
    async def boom(app) -> str:
        raise RuntimeError("selector broke")

    task = Task("site:x", timedelta(hours=6), boom)
    await run_task(app, task)
    await run_task(app, task)
    alerts = [e for e in app.bot.events if e[0] == "notify"]
    assert len(alerts) == 1 and "selector broke" in alerts[0][1]
    assert app.db.get("last:site:x") == app.clock.stamp()
    app.clock.moment += timedelta(hours=7)
    await run_task(app, task)
    assert len([e for e in app.bot.events if e[0] == "notify"]) == 2


async def test_logged_out_sites_ask_you_to_log_in(app):
    async def logged_out(app) -> str:
        raise LoggedOut("www.linkedin.com asked you to log in again")

    await run_task(app, Task("site:linkedin", timedelta(hours=6), logged_out))
    assert app.bot.events[-1][1].startswith("🔒")


async def test_digest_once_after_working_hours(app, monkeypatch):
    await maybe_digest(app)
    assert app.bot.events == []
    app.clock.moment = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)  # 20:30 IST
    await maybe_digest(app)
    await maybe_digest(app)
    assert app.bot.events == [("notify", "digest")]


async def test_tick_runs_due_tasks_unless_paused(app):
    ran = []

    async def work(app) -> str:
        ran.append(1)
        return "did work"

    tasks = [Task("x", timedelta(minutes=1), work)]
    app.db.put("paused", "1")
    await tick(app, tasks)
    assert ran == []
    app.db.put("paused", "0")
    await tick(app, tasks)
    await tick(app, tasks)  # not due again within the minute
    assert ran == [1]
