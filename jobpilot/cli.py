"""Command line for jobpilot. Run `jobpilot --help` for the list of commands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Awaitable
from importlib import resources
from pathlib import Path

from . import dashboard, yc
from .app import App
from .approvals import summary
from .browser import LoggedOut
from .config import SITES, ConfigError, resolve_home
from .db import question_key
from .mail import check_login
from .scheduler import build_tasks, run_forever
from .sites import JobStub, login_urls
from .telegram import Telegram

TEMPLATES = {"config.example.yaml": "config.yaml", "env.example": ".env",
             "answers.example.yaml": "profile/answers.yaml"}

NEXT_STEPS = """
Next:
  1. Put your resume at profile/resume.pdf
  2. Fill in config.yaml (profile, searches, exclude list) and .env (keys)
  3. jobpilot telegram-id     finds your Telegram chat id
  4. jobpilot login           log in to each job site once, in the Chrome window it opens
  5. jobpilot check           verifies everything
  6. jobpilot run             starts it (mode: dry_run until you switch config.yaml to live)
"""


def setup_logging(home: Path, verbose: bool) -> None:
    (home / "data").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(home / "data" / "jobpilot.log"), logging.StreamHandler()],
    )
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init(home: Path) -> int:
    templates = resources.files("jobpilot") / "templates"
    for source, target in TEMPLATES.items():
        destination = home / target
        if destination.exists():
            print(f"  kept     {target} (already there)")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((templates / source).read_text())
        print(f"  created  {target}")
    os.chmod(home / ".env", 0o600)
    (home / "data").mkdir(exist_ok=True)
    print(NEXT_STEPS)
    return 0


async def check(app: App) -> int:
    problems = 0

    def report(ok: bool, text: str, fix: str = "") -> None:
        nonlocal problems
        print(f"  {'✓' if ok else '✗'} {text}" + (f"\n      → {fix}" if fix and not ok else ""))
        problems += not ok

    settings = app.settings
    report(True, f"config.yaml loaded (mode: {settings.mode})")
    placeholders = [field for field in ("name", "email", "phone")
                    if re.search(r"your|x{4}|example", getattr(settings.profile, field), re.IGNORECASE)]
    report(not placeholders, "profile filled in", f"replace the placeholder {', '.join(placeholders)} in config.yaml")
    try:
        report(True, f"resume read ({len(app.resume):,} characters)")
    except (FileNotFoundError, ValueError) as exc:
        report(False, "resume", str(exc))

    if not app.secrets.anthropic_api_key:
        report(False, "Claude API key", "set ANTHROPIC_API_KEY in .env")
    else:
        try:
            reply = await app.llm.answer(question="What is your first name?", kind="text", options=[], job=None)
            report(True, f"Claude API works ({settings.llm.model} answered {reply.answer!r})")
        except Exception as exc:
            report(False, "Claude API", f"{type(exc).__name__}: {exc}")

    try:
        me = await app.bot.tg.me()
        await app.bot.notify("✅ jobpilot check: Telegram works. Approval cards will arrive here.")
        report(True, f"Telegram bot @{me['username']} can message you")
    except Exception as exc:
        report(False, "Telegram", f"{exc}")

    if app.mailbox is None:
        report(not settings.email.enabled, "Gmail", "set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env, "
                                                    "or set email.enabled: false")
    else:
        try:
            await asyncio.to_thread(check_login, app.mailbox)
            report(True, f"Gmail login works ({app.mailbox.address})")
        except Exception as exc:
            report(False, "Gmail login", f"{exc} (it needs an App Password, not your normal password)")

    if app.secrets.hunter_api_key:
        try:
            response = await app.http.get("https://api.hunter.io/v2/account",
                                          params={"api_key": app.secrets.hunter_api_key})
            left = response.json()["data"]["requests"]["searches"]
            report(True, f"Hunter.io key works ({left['available'] - left['used']} lookups left this month)")
        except Exception as exc:
            report(False, "Hunter.io", f"{exc}")
    else:
        print("  - no Hunter.io key: emails will be guessed (first@company.com) and marked as guesses")

    try:
        hiring = await yc.fetch_hiring(app.http)
        report(True, f"YC data reachable ({len(hiring):,} hiring companies)")
    except Exception as exc:
        report(False, "YC data", f"{exc} (behind a company proxy? set SSL_CERT_FILE in .env)")

    try:
        async with app.browser.session(headless=True) as page:
            await page.goto("about:blank")
        report(True, f"Chrome starts (channel: {settings.browser.channel})")
    except Exception as exc:
        report(False, "Chrome", f"{exc}\n        install Google Chrome, or set browser.channel: chromium "
                                "and run `playwright install chromium`")

    for name in SITES:
        site_settings = settings.site(name)
        if site_settings.enabled and name != "waas" and not (site_settings.search_urls or app.sites[name].default_searches):
            report(False, f"{name}: no search URLs", f"add {name}.search_urls in config.yaml, or set enabled: false")

    print("\nAll good." if not problems else f"\n{problems} thing(s) to fix.")
    if not problems and settings.dry_run:
        print("mode is dry_run: everything runs except the final click or send. Set mode: live when you're happy.")
    return 1 if problems else 0


async def login(app: App, names: list[str]) -> int:
    urls = login_urls()
    chosen = names or [name for name in SITES if app.settings.site(name).enabled]
    unknown = sorted(set(chosen) - set(urls))
    if unknown:
        print(f"Unknown site(s): {', '.join(unknown)}. Choose from: {', '.join(SITES)}")
        return 2
    async with app.browser.session(headless=False) as page:
        await page.goto(urls[chosen[0]])
        for name in chosen[1:]:
            tab = await page.context.new_page()
            await tab.goto(urls[name])
        print("Chrome is open with one tab per site. Log in on each, ticking 'remember me' where offered.")
        print("Your passwords go only to the sites; jobpilot keeps just the login cookies in data/browser-profile.")
        await asyncio.to_thread(input, "Press Enter here when you're done... ")
    return 0


async def telegram_id(app: App) -> int:
    if not app.secrets.telegram_bot_token:
        print("Put TELEGRAM_BOT_TOKEN in .env first (create a bot by messaging @BotFather on Telegram).")
        return 2
    telegram = Telegram(app.secrets.telegram_bot_token)
    me = await telegram.me()
    await asyncio.to_thread(input, f"Send any message to @{me['username']} on Telegram, then press Enter here... ")
    chats = {}
    for update in await telegram.updates(None, timeout=0):
        chat = (update.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No messages yet. Send the bot a message and run this again.")
        return 1
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({name})")
    print("Put that line in .env.")
    return 0


async def _snapshot(page, folder: Path, label: str) -> None:
    await page.screenshot(path=str(folder / f"{label}.png"), full_page=True)
    (folder / f"{label}.html").write_text(await page.content())
    (folder / f"{label}.aria.yml").write_text(await page.locator("body").aria_snapshot())


async def doctor(app: App, name: str) -> int:
    """Open a site's search and first job page, report what jobpilot sees, and save snapshots."""
    site = app.sites[name]
    urls = app.settings.site(name).search_urls or site.default_searches
    folder = app.paths.doctor / name
    folder.mkdir(parents=True, exist_ok=True)
    async with app.browser.session(headless=False) as page:
        stubs: list[JobStub] = []
        if urls:
            stubs = await site.discover(page, urls[:1], limit=10)
            await _snapshot(page, folder, "search")
            print(f"Search page: {urls[0]}\n  job links found: {len(stubs)}")
            for stub in stubs[:5]:
                print(f"    {stub.url}  {stub.title or ''}".rstrip())
        else:
            known = app.db.jobs(name, ["new", "matched", "awaiting_approval", "low_score"], limit=1)
            if known:
                stubs = [JobStub(known[0]["external_id"], known[0]["url"], known[0]["title"])]
            print("No search URL for this site; jobs arrive from YC company pages.")
        if stubs:
            info = await site.read(page, stubs[0].url)
            await _snapshot(page, folder, "job")
            print(f"Job page: {stubs[0].url}\n  title: {info.title}\n  apply button found: {info.can_apply}\n"
                  f"  already applied: {info.applied}\n  job text: {len(info.description):,} characters")
    print(f"\nSnapshots (screenshot, HTML, accessibility tree) saved in {folder}")
    if not stubs:
        print("No job links matched. Send me the snapshots and I'll adjust the patterns, or set "
              f"{name}.selectors.job_href in config.yaml.")
    return 0


async def once(app: App, task_name: str) -> int:
    """Run one task now, outside the schedule, with errors shown here rather than on Telegram."""
    tasks = {task.name: task for task in build_tasks(app)}
    if task_name not in tasks:
        print(f"Unknown task. Choose from: {', '.join(tasks)}")
        return 2
    try:
        summary = await tasks[task_name].run(app)
    except LoggedOut as exc:
        print(f"🔒 {exc}")
        return 1
    app.db.put(f"last:{task_name}", app.clock.stamp())
    print(summary or "done (nothing to report)")
    return 0


def status(app: App) -> int:
    print(re.sub(r"<[^>]+>", "", summary(app.db, app.clock, app.settings)))
    return 0


def answers(app: App, forget: str | None) -> int:
    if forget:
        removed = app.db.conn.execute("DELETE FROM answers WHERE key=?", (question_key(forget),)).rowcount
        print("Forgotten." if removed else "No saved answer for that question.")
        return 0 if removed else 1
    for row in app.db.rows("SELECT * FROM answers ORDER BY created_at"):
        print(f"[{row['source']}] {row['question']}\n    -> {row['answer']}")
    return 0


def requeue(app: App) -> int:
    counts = app.db.requeue_dry_runs()
    print(f"Back in the queue: {counts['applications']} one-click applications, "
          f"{counts['notes']} noted applications.\n"
          f"Back to Telegram for a real decision: {counts['drafts']} drafts (emails, LinkedIn notes, application notes).")
    if app.settings.dry_run:
        print("Note: config.yaml still says mode: dry_run, so these will only be dry-run again. "
              "Set mode: live first.")
    return 0


async def publish_dashboard(app: App) -> int:
    path = dashboard.export(app)
    if not dashboard.ready(app):
        print(f"Wrote {path}. To publish it: install the Vercel CLI (npm i -g vercel), then in dashboard/ run "
              "`vercel login` and `vercel link` (see dashboard/README.md).")
        return 0
    print(await dashboard.publish(app))
    return 0


async def _run(app: App, work: Awaitable[int]) -> int:
    try:
        return await work
    finally:
        await app.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobpilot", description="Job-search autopilot with Telegram approvals.")
    parser.add_argument("--home", help="folder holding config.yaml and .env (default: $JOBPILOT_HOME or here)")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create config.yaml, .env and profile/answers.yaml from templates")
    commands.add_parser("check", help="verify config, resume, keys, Telegram, Gmail and Chrome")
    login_cmd = commands.add_parser("login", help="open Chrome to log in to the job sites once")
    login_cmd.add_argument("sites", nargs="*", help=f"any of {', '.join(SITES)} (default: all enabled)")
    commands.add_parser("telegram-id", help="find your Telegram chat id")
    doctor_cmd = commands.add_parser("doctor", help="check what jobpilot sees on a site, without applying")
    doctor_cmd.add_argument("site", choices=SITES)
    commands.add_parser("run", help="run everything on schedule until Ctrl+C")
    once_cmd = commands.add_parser("once", help="run one task now, e.g. site:linkedin, yc-sync, inbox")
    once_cmd.add_argument("task")
    commands.add_parser("status", help="today's numbers")
    answers_cmd = commands.add_parser("answers", help="list saved form answers, or forget one")
    answers_cmd.add_argument("--forget", metavar="QUESTION")
    commands.add_parser("dashboard", help="publish the Vercel dashboard now (it also updates every 30 minutes)")
    commands.add_parser("requeue", help="after switching to live: redo what the dry run only rehearsed")
    args = parser.parse_args(argv)

    home = resolve_home(args.home)
    if args.command == "init":
        return init(home)
    setup_logging(home, args.verbose)
    try:
        app = App(home)
        if args.command == "status":
            return status(app)
        if args.command == "answers":
            return answers(app, args.forget)
        if args.command == "requeue":
            return requeue(app)
        work = {
            "check": lambda: check(app),
            "login": lambda: login(app, args.sites),
            "telegram-id": lambda: telegram_id(app),
            "doctor": lambda: doctor(app, args.site),
            "run": lambda: _forever(app),
            "once": lambda: once(app, args.task),
            "dashboard": lambda: publish_dashboard(app),
        }[args.command]()
        return asyncio.run(_run(app, work))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


async def _forever(app: App) -> int:
    await run_forever(app)
    return 0
