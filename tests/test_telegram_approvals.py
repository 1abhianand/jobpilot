from __future__ import annotations

import json
import re

import httpx
import pytest

from jobpilot.approvals import HELP, ApprovalBot
from jobpilot.db import DB
from jobpilot.llm import Draft
from jobpilot.telegram import Telegram, TelegramError

from .conftest import MovableClock, make_settings

CHAT = 4242


# -- the Bot API client ------------------------------------------------------------------------

async def test_client_sends_html_with_buttons_and_raises_on_errors():
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        seen.append((method, body))
        if method == "editMessageText":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"})
        if method == "getMe":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    tg = Telegram("TOKEN", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await tg.send(CHAT, "<b>hi</b>", [[("✅ Send", "ok:1")]]) == 77
    method, body = seen[0]
    assert method == "sendMessage" and body["parse_mode"] == "HTML"
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "✅ Send", "callback_data": "ok:1"}]]}
    await tg.send(CHAT, "reply please", force_reply=True, reply_to=5)
    assert seen[1][1]["reply_markup"]["force_reply"] is True
    assert seen[1][1]["reply_parameters"]["message_id"] == 5
    await tg.edit(CHAT, 77, "same text")  # "not modified" is ignored
    with pytest.raises(TelegramError, match="Unauthorized"):
        await tg.me()


# -- the approval bot -----------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []
        self.next_id = 100

    async def send(self, chat_id, text, buttons=None, *, force_reply=False, reply_to=None):
        self.next_id += 1
        self.sent.append({"id": self.next_id, "chat": chat_id, "text": text, "buttons": buttons,
                          "force_reply": force_reply})
        return self.next_id

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))

    async def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))


@pytest.fixture
def rewrites() -> list[str]:
    return []


@pytest.fixture
def bot(db: DB, rewrites: list[str]) -> ApprovalBot:
    settings = make_settings(approvals={"max_open_cards": 2})

    async def rewrite(o, instruction: str) -> Draft:
        rewrites.append(instruction)
        return Draft(subject=None, body=f"rewritten ({instruction or 'fresh'})")

    return ApprovalBot(db, FakeTelegram(), CHAT, settings, MovableClock(settings), rewrite)


def seed(db: DB, channel: str = "email", body: str = "Hi Dev,\nLet's talk.", **fields) -> int:
    company = db.upsert_company("acme.example", "Acme AI", "yc", yc_batch="W24")
    person = db.upsert_person(company, "Dev Patel", "https://www.linkedin.com/in/dev/", title="CTO",
                              email="dev@acme.example", email_source="guess")
    return db.add_outreach(channel, body, person_id=person, company_id=company, to_addr="dev@acme.example",
                           subject="Backend work" if channel == "email" else None, why="builds agents", **fields)


def tap(data: str, chat: int = CHAT) -> dict:
    return {"update_id": 1, "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat}}}}


def text(message: str, reply_to: int | None = None, chat: int = CHAT) -> dict:
    body = {"chat": {"id": chat}, "text": message}
    if reply_to is not None:
        body["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 2, "message": body}


async def test_pump_limits_open_cards(bot: ApprovalBot, db: DB):
    ids = [seed(db) for _ in range(3)]
    assert await bot.pump() == 2
    assert await bot.pump() == 0
    assert [db.outreach(i)["status"] for i in ids] == ["pending_approval", "pending_approval", "draft"]
    card = bot.tg.sent[0]
    assert "Dev Patel" in card["text"] and "(YC W24)" in card["text"] and "guessed" in card["text"]
    assert "Dry run" in card["text"]
    assert card["buttons"][0][0] == ("✅ Send", f"ok:{ids[0]}")


async def test_cards_escape_html(bot: ApprovalBot, db: DB):
    oid = seed(db, body="<script>alert(1)</script> & more")
    assert "&lt;script&gt;" in bot.render(db.outreach(oid))


async def test_approve_skip_and_double_tap(bot: ApprovalBot, db: DB):
    first, second = seed(db), seed(db)
    await bot.pump()
    await bot.handle(tap(f"ok:{first}"))
    assert db.outreach(first)["status"] == "approved" and db.outreach(first)["approved_at"]
    assert "Approved" in bot.tg.edits[-1][1] and bot.tg.edits[-1][2] is None
    await bot.handle(tap(f"ok:{first}"))
    assert bot.tg.answers[-1] == ("cb", "Already handled.")
    await bot.handle(tap(f"no:{second}", chat=999))  # someone else's chat: ignored
    assert db.outreach(second)["status"] == "pending_approval"
    await bot.handle(tap(f"no:{second}"))
    assert db.outreach(second)["status"] == "rejected"


async def test_skipping_an_application_skips_the_job(bot: ApprovalBot, db: DB):
    db.add_job("waas", "1", "https://www.workatastartup.com/jobs/1", title="Backend")
    oid = seed(db, channel="apply_note", body="note", job_id="waas:1")
    await bot.pump()
    await bot.handle(tap(f"no:{oid}"))
    assert db.job("waas:1")["status"] == "skipped"


async def test_edit_replaces_subject_and_body(bot: ApprovalBot, db: DB):
    oid = seed(db)
    await bot.pump()
    old_card = db.outreach(oid)["tg_message_id"]
    await bot.handle(tap(f"ed:{oid}"))
    prompt = bot.tg.sent[-1]
    assert prompt["force_reply"]
    await bot.handle(text("Subject: Quick one\nHi Dev, shorter now.", reply_to=prompt["id"]))
    o = db.outreach(oid)
    assert (o["subject"], o["body"], o["status"]) == ("Quick one", "Hi Dev, shorter now.", "pending_approval")
    assert o["tg_message_id"] != old_card
    assert any(edit[0] == old_card and "Replaced" in edit[1] for edit in bot.tg.edits)


async def test_edit_rejects_notes_over_linkedin_limit(bot: ApprovalBot, db: DB):
    oid = seed(db, channel="li_connect", body="short note")
    await bot.pump()
    await bot.handle(tap(f"ed:{oid}"))
    await bot.handle(text("x" * 250, reply_to=bot.tg.sent[-1]["id"]))
    assert db.outreach(oid)["body"] == "short note"
    assert "LinkedIn allows 200" in bot.tg.sent[-1]["text"]
    await bot.handle(text("A better, shorter note.", reply_to=bot.tg.sent[-1]["id"]))
    assert db.outreach(oid)["body"] == "A better, shorter note."


async def test_rewrite_passes_your_instruction(bot: ApprovalBot, db: DB, rewrites: list[str]):
    oid = seed(db, channel="li_message", body="old")
    await bot.pump()
    await bot.handle(tap(f"rw:{oid}"))
    await bot.handle(text("mention Kafka", reply_to=bot.tg.sent[-1]["id"]))
    await bot.handle(tap(f"rw:{oid}"))
    await bot.handle(text("go"))  # a plain message answers the prompt sent moments ago
    assert rewrites == ["mention Kafka", ""]
    assert db.outreach(oid)["body"] == "rewritten (fresh)"


async def test_form_question_answers_are_learned(bot: ApprovalBot, db: DB):
    db.add_job("linkedin", "9", "https://www.linkedin.com/jobs/view/9/", title="Backend", company="Acme")
    db.set_job("linkedin:9", status="needs_input", question="Years of Go?")
    await bot.ask_answer(db.job("linkedin:9"), "Years of Go?", ["0-2", "3+"], hint="Enter a whole number")
    assert "Years of Go?" in bot.tg.sent[-1]["text"] and "whole number" in bot.tg.sent[-1]["text"]
    await bot.handle(text("3+", reply_to=bot.tg.sent[-1]["id"]))
    assert db.learned_answer("Years of Go?") == "3+"
    assert db.job("linkedin:9")["status"] == "queued"


async def test_stray_messages_get_help(bot: ApprovalBot, db: DB):
    await bot.handle(text("hello"))
    assert bot.tg.sent[-1]["text"] == HELP
    await bot.handle(text("hello", chat=999))
    assert len(bot.tg.sent) == 1


async def test_commands(bot: ApprovalBot, db: DB):
    await bot.handle(text("/pause"))
    assert db.get("paused") == "1"
    await bot.handle(text("/status"))
    assert "Paused" in bot.tg.sent[-1]["text"]
    await bot.handle(text("/resume"))
    assert db.get("paused") == "0"
    oid = seed(db)
    await bot.pump()
    await bot.handle(text("/pending"))
    assert bot.tg.sent[-1]["buttons"][0][0][1] == f"ok:{oid}"


async def test_queued_lists_approved_not_yet_sent(bot: ApprovalBot, db: DB):
    await bot.handle(text("/queued"))
    assert "Nothing queued" in bot.tg.sent[-1]["text"]
    db.add_job("wellfound", "7", "https://wellfound.example/7", title="SDE-1", company="Zenskar")
    note = seed(db, channel="apply_note", job_id="wellfound:7")
    request = seed(db, channel="li_connect")
    done = seed(db)
    for oid in (note, request, done):
        db.set_outreach(oid, status="approved", approved_at=bot.clock.stamp())
    db.set_outreach(done, status="sent", sent_at=bot.clock.stamp())
    await bot.handle(text("/queued"))
    report = bot.tg.sent[-1]["text"]
    assert "2 approved" in report
    assert f"#{note} " in report and "SDE-1</a> · Zenskar" in report and f"#{request} " in report
    assert f"#{done} " not in report


async def test_questions_failed_run_and_answers_commands(bot: ApprovalBot, db: DB):
    await bot.handle(text("/questions"))
    assert bot.tg.sent[-1]["text"] == "No open form questions."
    db.add_job("linkedin", "9", "https://www.linkedin.com/jobs/view/9/", title="Backend", company="Acme")
    db.set_job("linkedin:9", status="needs_input", question="Current Salary (LPA)")
    await bot.handle(text("/questions"))
    assert "Current Salary (LPA)" in bot.tg.sent[-1]["text"]
    await bot.handle(text("12", reply_to=bot.tg.sent[-1]["id"]))
    assert db.learned_answer("Current Salary (LPA)") == "12"

    oid = seed(db, channel="li_connect", body="note")
    await bot.pump()
    db.set_outreach(oid, status="failed", error="note_quota: free plan")
    await bot.handle(text("/failed"))
    assert bot.tg.sent[-1]["buttons"][0][0] == ("🤝 Send without the note", f"nn:{oid}")

    db.put("last:site:linkedin", bot.clock.stamp())
    await bot.handle(text("/run"))
    assert "site:linkedin" in bot.tg.sent[-1]["text"]
    await bot.handle(text("/run site:linkedin"))
    assert db.get("last:site:linkedin") is None and "runs within a minute" in bot.tg.sent[-1]["text"]
    await bot.handle(text("/run site:linkedin"))  # no last: record any more, still a known task
    assert "runs within a minute" not in bot.tg.sent[-1]["text"]  # without a schedule, only past runs are known

    bot.tasks = ["site:linkedin", "site:instahyre", "inbox"]
    await bot.handle(text("/run linkedin"))
    assert "▶️ site:linkedin runs within a minute" in bot.tg.sent[-1]["text"]
    await bot.handle(text("/run insta"))
    assert "▶️ site:instahyre runs" in bot.tg.sent[-1]["text"]
    await bot.handle(text("/run monster"))
    assert "I don't know the task" in bot.tg.sent[-1]["text"]

    await bot.handle(text("/answers"))
    assert "Current Salary (LPA) → <b>12</b>" in bot.tg.sent[-1]["text"]


def test_menu_and_help_list_every_command():
    """Typing / in Telegram should offer every command the bot handles, and /help should explain each."""
    import inspect
    from jobpilot import approvals
    handled = set(re.findall(r'command == "/(\w+)"', inspect.getsource(approvals.ApprovalBot._command)))
    menu = {name for name, _ in approvals.COMMANDS}
    assert handled | {"help"} == menu
    assert all(f"/{name}" in HELP for name in handled)


async def test_menu_is_retried_until_telegram_takes_it(bot: ApprovalBot, db: DB):
    calls = []

    async def set_commands(commands):
        calls.append(commands)
        if len(calls) == 1:
            raise httpx.ConnectError("offline")

    bot.tg.set_commands = set_commands
    await bot.register_commands()  # network down: nothing saved, so the next call tries again
    await bot.register_commands()
    await bot.register_commands()  # already up to date: no call
    assert len(calls) == 2


async def test_send_goes_out_right_away(bot: ApprovalBot, db: DB):
    import asyncio
    sent_now = []

    async def send_now(oid):
        sent_now.append(oid)

    bot.send_now = send_now
    oid = seed(db)
    await bot.pump()
    await bot.handle(tap(f"ok:{oid}"))
    await asyncio.sleep(0)
    assert sent_now == [oid] and "Sending now" in bot.tg.edits[-1][1]


async def test_resume_says_what_goes_out_and_what_waits(bot: ApprovalBot, db: DB):
    bot.outlook = lambda: [("apply_note", 1, ""), ("li_connect", 8, "Today's limit is reached: it goes out tomorrow."),
                           ("email", 2, "No emails go out on Sundays: it's queued and goes out the next day.")]
    db.put("paused", "1")
    db.put("last:actions", bot.clock.stamp())
    await bot.handle(text("/resume"))
    report = bot.tg.sent[-1]["text"]
    assert db.get("paused") == "0" and db.get("last:actions") is None  # the send run starts on the next tick
    assert "📝 1 application" in report and "🤝 8 LinkedIn connection requests" in report
    assert "Sundays" in report and "📧 2 emails" in report


async def test_failed_sends_offer_retry_and_no_note(bot: ApprovalBot, db: DB):
    oid = seed(db, channel="li_connect", body="note")
    await bot.pump()
    db.set_outreach(oid, status="failed")
    await bot.failed(oid, "free-plan note limit", without_note=True)
    buttons = bot.tg.sent[-1]["buttons"]
    assert buttons[0][0] == ("🤝 Send without the note", f"nn:{oid}")
    await bot.handle(tap(f"nn:{oid}"))
    o = db.outreach(oid)
    assert (o["status"], o["body"]) == ("approved", "")


async def test_digest_lists_todays_applications(bot: ApprovalBot, db: DB):
    db.add_job("instahyre", "1", "u1", title="Backend <Engineer>", company="Acme")
    db.set_job("instahyre:1", status="dry_run", applied_at=bot.clock.stamp())
    db.add_job("hirist", "2", "u2", title="Old one", company="Beta")
    db.set_job("hirist:2", status="applied", applied_at=bot.clock.ago(days=2))
    digest = bot.digest()
    assert "Applied: Instahyre 1" in digest
    assert "Backend &lt;Engineer&gt; · Acme (Instahyre)" in digest and "Old one" not in digest


async def test_applied_lists_applications_by_site_and_period(bot: ApprovalBot, db: DB):
    db.add_job("instahyre", "1", "https://instahyre.example/1", title="Backend <Engineer>", company="Acme")
    db.set_job("instahyre:1", status="applied", applied_at=bot.clock.ago(days=2))
    db.add_job("wellfound", "2", "https://wellfound.example/2", title="Platform", company="Beta")
    db.set_job("wellfound:2", status="dry_run", applied_at=bot.clock.stamp())
    db.add_job("linkedin", "3", "https://linkedin.example/3", title="Old one", company="Gamma")
    db.set_job("linkedin:3", status="applied", applied_at=bot.clock.ago(days=20))
    db.add_job("linkedin", "4", "https://linkedin.example/4", title="Not applied", company="Delta")

    await bot.handle(text("/applied"))
    report = bot.tg.sent[-1]["text"]
    assert "2 applications" in report and "last 7 days" in report
    assert '<a href="https://instahyre.example/1">Backend &lt;Engineer&gt;</a> · Acme' in report
    assert "\n1. " in report and "\n2. " in report and "<b>Instahyre</b> (1)" in report
    assert "Platform" in report and "🧪" in report
    assert "Old one" not in report and "Not applied" not in report

    await bot.handle(text("/applied li 30"))
    report = bot.tg.sent[-1]["text"]
    assert "1 application on LinkedIn" in report and "Old one" in report and "Acme" not in report

    await bot.handle(text("/applied insta all"))
    assert "on Instahyre" in bot.tg.sent[-1]["text"] and "all time" in bot.tg.sent[-1]["text"]

    await bot.handle(text("/applied monster"))
    assert "don't know the site" in bot.tg.sent[-1]["text"]


async def test_applied_splits_long_lists(bot: ApprovalBot, db: DB):
    for i in range(120):
        db.add_job("linkedin", str(i), f"https://linkedin.example/{i}", title="Backend Engineer " * 3, company="Acme")
        db.set_job(f"linkedin:{i}", status="applied", applied_at=bot.clock.stamp())
    await bot.handle(text("/applied"))
    assert len(bot.tg.sent) > 1 and all(len(m["text"]) <= 4096 for m in bot.tg.sent)


async def test_sent_lists_outreach(bot: ApprovalBot, db: DB):
    email = seed(db)
    db.set_outreach(email, status="sent", sent_at=bot.clock.stamp())
    note = seed(db, channel="li_connect", body="hi")
    db.set_outreach(note, status="sent", sent_at=bot.clock.stamp(), to_addr="https://www.linkedin.com/in/dev/")
    await bot.handle(text("/sent"))
    report = bot.tg.sent[-1]["text"]
    assert "2 sent" in report and "Email" in report and "LinkedIn connection request" in report
    assert f"#{email} · " in report and f"#{note} · " in report
    await bot.handle(text("/sent linkedin"))
    assert "1 sent" in bot.tg.sent[-1]["text"] and "Email" not in bot.tg.sent[-1]["text"]


async def test_command_menu_matches_help():
    from jobpilot.approvals import COMMANDS
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    await Telegram("T", httpx.AsyncClient(transport=httpx.MockTransport(handler))).set_commands(COMMANDS)
    assert [c["command"] for c in seen[0]["commands"]] == [name for name, _ in COMMANDS]
    assert all(f"/{name}" in HELP for name, _ in COMMANDS if name != "help")
