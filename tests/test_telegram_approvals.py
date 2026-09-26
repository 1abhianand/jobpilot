from __future__ import annotations

import json

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
