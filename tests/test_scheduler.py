from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobpilot.browser import LoggedOut
from jobpilot.mail import Mailbox
from jobpilot.scheduler import RESTART, Task, _work_loop, build_tasks, due, maybe_digest, run_task, tick, work_pass


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
    app.clock.moment = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)  # Sunday: a working day by default
    assert due(app, task)
    app.clock.days = {"mon", "tue", "wed", "thu", "fri", "sat"}  # a day you take off
    assert not due(app, task) and due(app, anytime)
    app.clock.days.add("sun")
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


async def test_sites_take_turns_when_nothing_is_due(app):
    ran = []

    def site(name):
        async def work(app) -> str:
            ran.append(name)
            return ""
        return Task(f"site:{name}", timedelta(hours=6), work)

    tasks = [site("a"), site("b")]
    app.db.put("last:site:a", app.clock.ago(hours=2))
    app.db.put("last:site:b", app.clock.ago(hours=1))
    assert await work_pass(app, tasks)
    assert ran == ["a"]  # nothing due, so the one run longest ago goes
    assert await work_pass(app, tasks)
    assert ran == ["a", "b"]
    assert not await work_pass(app, tasks)  # both ran just now: they rest a while first
    app.clock.moment += timedelta(minutes=31)
    assert await work_pass(app, tasks)
    assert ran == ["a", "b", "a"]


async def test_restart_waits_for_the_task_in_hand(app):
    ran = []

    async def work(app) -> str:
        ran.append(1)
        app.db.put(RESTART, "1")  # asked for while this task is working
        return ""

    await _work_loop(app, [Task("site:a", timedelta(hours=6), work)])  # returns instead of looping forever
    assert ran == [1]


async def test_linkedin_is_only_used_in_the_daytime(app):
    task = Task("site:linkedin", timedelta(hours=6), lambda app: None)
    other = Task("site:instahyre", timedelta(hours=6), lambda app: None)
    app.clock.moment = app.clock.moment.replace(hour=14, minute=0)  # 19:30 IST: working hours, but past 19:00
    assert not due(app, task) and due(app, other)


async def test_linkedin_site_rests_after_a_linkedin_send(app):
    task = Task("site:linkedin", timedelta(hours=6), lambda app: None)
    other = Task("site:instahyre", timedelta(hours=6), lambda app: None)
    app.db.put("linkedin_rest_until", app.clock.stamp())
    app.clock.moment -= timedelta(minutes=2)  # two minutes before the rest ends
    assert not due(app, task) and due(app, other)
    app.clock.moment += timedelta(minutes=3)
    assert due(app, task)


async def test_linkedin_never_gets_extra_idle_turns(app):
    from jobpilot.scheduler import next_site
    linkedin = Task("site:linkedin", timedelta(hours=3), lambda app: None)
    other = Task("site:instahyre", timedelta(hours=6), lambda app: None)
    assert next_site(app, [linkedin]) is None
    assert next_site(app, [linkedin, other]) is other
