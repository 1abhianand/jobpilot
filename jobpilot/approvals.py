"""Telegram approval flow. Nothing reaches another person until you tap Send on its card.

Cards: every draft (email, LinkedIn note or message, application note) arrives with
Send / Edit / Rewrite / Skip. Edit and Rewrite ask for your reply text; so do form questions
the bot can't answer on its own. Only messages from your own chat are ever acted on.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .clock import Clock
from .config import Settings
from .db import DB
from .llm import Draft
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

Rewriter = Callable[[sqlite3.Row, str], Awaitable[Draft]]

ICONS = {"email": "📧", "li_connect": "🤝", "li_message": "💬", "apply_note": "📝"}
LABELS = {"email": "Email", "li_connect": "LinkedIn connection request", "li_message": "LinkedIn message",
          "apply_note": "Application"}
SEND_LABELS = {"apply_note": "✅ Apply", "li_connect": "✅ Send request"}
SITE_NAMES = {"linkedin": "LinkedIn", "instahyre": "Instahyre", "hirist": "Hirist", "wellfound": "Wellfound",
              "waas": "Work at a Startup"}
MAX_CARD = 3900  # Telegram's limit is 4096; leave room for the status line

HELP = """I send you every email, LinkedIn note and application note before it goes out.

On each card: <b>Send</b> approves it, <b>Edit</b> lets you reply with your own text, <b>Rewrite</b> asks for an instruction ("shorter", "mention my Kafka work") and redrafts, <b>Skip</b> drops it.

/status: today's numbers
/pending: show open cards again
/pause and /resume: stop and restart all automation"""


def esc(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=False)


def attr(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _email_source(source: str | None) -> str:
    if not source:
        return ""
    if source.startswith("hunter:"):
        return f" (found by Hunter, confidence {source.split(':', 1)[1]}%)"
    if source == "guess":
        return " (guessed from the usual first-name pattern; unverified)"
    return f" ({esc(source)})"


def summary(db: DB, clock: Clock, settings: Settings) -> str:
    """Today's numbers, as Telegram HTML."""
    day = clock.day_start()
    applied = db.applied_by_site(day)
    sent = db.sent_by_channel(day)
    lines = ["📊 <b>Today</b>" + (" (dry run, nothing really sent)" if settings.dry_run else "")]
    if applied:
        lines.append("Applied: " + ", ".join(f"{SITE_NAMES.get(s, s)} {n}" for s, n in sorted(applied.items())))
    if sent:
        lines.append("Sent: " + ", ".join(f"{LABELS.get(c, c)} {n}" for c, n in sorted(sent.items())))
    if not applied and not sent:
        lines.append("Nothing applied or sent yet.")
    lines.append(f"Replies: {db.replies_since(day)}")
    lines.append(f"Waiting on you: {db.count_outreach('pending_approval')} cards, "
                 f"{db.count_jobs('needs_input')} form questions")
    lines.append(f"Approved, queued to send: {db.count_outreach('approved')}")
    spent = float(db.get(f"cost:{clock.local().date()}", "0") or 0)
    lines.append(f"Claude API cost today: ${spent:.2f}")
    if db.get("paused") == "1":
        lines.append("⏸ Paused. /resume to continue.")
    return "\n".join(lines)


class ApprovalBot:
    def __init__(self, db: DB, tg: Telegram, chat_id: int, settings: Settings, clock: Clock,
                 rewrite: Rewriter):
        self.db = db
        self.tg = tg
        self.chat_id = chat_id
        self.settings = settings
        self.clock = clock
        self.rewrite = rewrite

    # -- cards ------------------------------------------------------------------

    def render(self, o: sqlite3.Row) -> str:
        db = self.db
        person, company = db.person(o["person_id"]), db.company(o["company_id"])
        job = db.job(o["job_id"]) if o["job_id"] else None
        label = LABELS[o["channel"]] + (" (follow-up)" if o["parent_id"] else "")
        lines = [f"{ICONS[o['channel']]} <b>{label}</b> · #{o['id']}"]
        company_name = ""
        if company:
            company_name = esc(company["name"]) + (f" (YC {esc(company['yc_batch'])})" if company["yc_batch"] else "")
        if person:
            who = f"<b>{esc(person['name'])}</b>"
            if person["title"]:
                who += f", {esc(person['title'])}"
            lines.append(who + (f" · {company_name}" if company_name else ""))
        elif company_name:
            lines.append(f"<b>{company_name}</b>")
        if job:
            where = " · ".join(esc(x) for x in (job["location"], SITE_NAMES.get(job["site"], job["site"])) if x)
            lines.append(f'<a href="{attr(job["url"])}">{esc(job["title"] or "Job posting")}</a> · {where}')
            if job["score"] is not None:
                lines.append(f"Fit {job['score']}/100: {esc(job['reason'])}")
        if o["channel"] == "email":
            lines.append(f"To: <code>{esc(o['to_addr'])}</code>{_email_source(person['email_source'] if person else None)}")
        elif o["channel"] in ("li_connect", "li_message") and o["to_addr"]:
            lines.append(f'<a href="{attr(o["to_addr"])}">LinkedIn profile</a>')
        if o["why"]:
            lines.append(f"<i>{esc(o['why'])}</i>")
        lines.append("")
        if o["subject"]:
            lines.append(f"<b>Subject:</b> {esc(o['subject'])}")
        head = "\n".join(lines)
        tail = []
        if o["channel"] == "li_connect" and o["body"]:
            tail.append(f"{len(o['body'])}/{self.settings.linkedin.note_limit} characters")
        if self.settings.dry_run:
            tail.append("🧪 Dry run: approving won't send anything yet.")
        body = o["body"] or "(no note, just the connection request)"
        room = MAX_CARD - len(head) - sum(len(t) + 1 for t in tail) - 40
        if len(esc(body)) > room:
            body = body[: max(room, 200)] + " …"
            tail.insert(0, "(Card shortened to fit Telegram. The full text is what gets sent.)")
        return "\n".join([head, f"<blockquote>{esc(body)}</blockquote>", *tail])

    @staticmethod
    def buttons(o: sqlite3.Row) -> Buttons:
        oid = o["id"]
        return [[(SEND_LABELS.get(o["channel"], "✅ Send"), f"ok:{oid}"), ("✏️ Edit", f"ed:{oid}")],
                [("🔁 Rewrite", f"rw:{oid}"), ("❌ Skip", f"no:{oid}")]]

    async def _show(self, o: sqlite3.Row) -> None:
        message_id = await self.tg.send(self.chat_id, self.render(o), self.buttons(o))
        self.db.set_outreach(o["id"], status="pending_approval", tg_message_id=message_id)

    async def mark(self, outreach_id: int, status_line: str) -> None:
        """Rewrite a card with a closing status line and no buttons."""
        o = self.db.outreach(outreach_id)
        if o and o["tg_message_id"]:
            await self.tg.edit(self.chat_id, o["tg_message_id"], f"{self.render(o)}\n\n{status_line}")

    async def pump(self) -> int:
        """Send cards for new drafts while fewer than max_open_cards are waiting on you."""
        room = self.settings.approvals.max_open_cards - self.db.count_outreach("pending_approval")
        sent = 0
        for o in self.db.outreach_in("draft", limit=max(room, 0)):
            await self._show(o)
            sent += 1
        return sent

    # -- other messages -----------------------------------------------------------

    async def notify(self, text: str) -> None:
        await self.tg.send(self.chat_id, text)

    async def ask_answer(self, job: sqlite3.Row, question: str, options: list[str], hint: str = "") -> None:
        site = SITE_NAMES.get(job["site"], job["site"])
        choices = ("\nOptions: " + " / ".join(esc(o) for o in options)) if options else ""
        said = f"\n({esc(site)} said: {esc(hint)})" if hint else ""
        text = (f"❓ <b>{esc(site)}</b> application for <b>{esc(job['title'] or 'a job')}</b>"
                f"{' at ' + esc(job['company']) if job['company'] else ''} asks:\n"
                f"“{esc(question)}”{choices}{said}\n\nReply with your answer. I'll remember it and retry the application.")
        message_id = await self.tg.send(self.chat_id, text, force_reply=True)
        self.db.add_prompt(message_id, "answer", job["id"])

    async def failed(self, outreach_id: int, reason: str, *, without_note: bool = False) -> None:
        o = self.db.outreach(outreach_id)
        rows: Buttons = [[("🔁 Retry", f"rt:{outreach_id}"), ("❌ Drop", f"no:{outreach_id}")]]
        if without_note:
            rows.insert(0, [("🤝 Send without the note", f"nn:{outreach_id}")])
        await self.mark(outreach_id, f"⚠️ Not sent: {esc(reason)}")
        await self.tg.send(self.chat_id, f"⚠️ Couldn't send #{outreach_id} ({LABELS[o['channel']]}): {esc(reason)}",
                           rows, reply_to=o["tg_message_id"])

    def digest(self) -> str:
        """The end-of-day message: today's numbers plus every application made."""
        applied = self.db.applied_list(self.clock.day_start())
        lines = [self.summary()]
        if applied:
            lines += ["", "<b>Applications today</b>"]
            for job in applied[:40]:
                where = SITE_NAMES.get(job["site"], job["site"])
                lines.append(f"• {esc(job['title'] or 'Job')} · {esc(job['company'] or '?')} ({esc(where)})")
            if len(applied) > 40:
                lines.append(f"…and {len(applied) - 40} more")
        return "\n".join(lines)

    def summary(self) -> str:
        return summary(self.db, self.clock, self.settings)

    # -- incoming updates ---------------------------------------------------------

    async def poll_forever(self) -> None:
        offset = int(self.db.get("tg_offset", "0") or 0) or None
        while True:
            try:
                updates = await self.tg.updates(offset)
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("Telegram poll failed: %s", exc)
                await asyncio.sleep(10)
                continue
            except Exception:  # never let the listener die: approvals depend on it
                log.exception("Telegram poll failed unexpectedly")
                await asyncio.sleep(30)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.db.put("tg_offset", str(offset))
                try:
                    await self.handle(update)
                except Exception:
                    log.exception("Telegram update %s failed", update.get("update_id"))

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            if (query.get("message") or {}).get("chat", {}).get("id") != self.chat_id:
                return
            await self._on_button(query)
        elif "message" in update:
            message = update["message"]
            if message.get("chat", {}).get("id") != self.chat_id or not message.get("text"):
                return
            await self._on_text(message)

    async def _on_button(self, query: dict[str, Any]) -> None:
        action, _, ref = (query.get("data") or "").partition(":")
        o = self.db.outreach(ref) if ref.isdigit() else None
        allowed = {"ok": ("pending_approval",), "ed": ("pending_approval",), "rw": ("pending_approval",),
                   "no": ("pending_approval", "failed"), "rt": ("failed",), "nn": ("failed",)}
        if o is None or action not in allowed or o["status"] not in allowed[action]:
            await self.tg.answer_callback(query["id"], "Already handled.")
            return
        oid = o["id"]
        stamp = self.clock.stamp()
        if action == "ok":
            self.db.set_outreach(oid, status="approved", approved_at=stamp, error=None)
            await self.tg.answer_callback(query["id"], "Approved")
            await self.mark(oid, "✅ Approved. It goes out with the next send run, in working hours.")
        elif action == "no":
            self.db.set_outreach(oid, status="rejected")
            if o["channel"] == "apply_note" and o["job_id"]:
                self.db.set_job(o["job_id"], status="skipped")
            await self.tg.answer_callback(query["id"], "Skipped")
            await self.mark(oid, "❌ Skipped.")
        elif action in ("rt", "nn"):
            fields: dict[str, Any] = {"status": "approved", "approved_at": stamp, "error": None}
            if action == "nn":
                fields["body"] = ""
            self.db.set_outreach(oid, **fields)
            await self.tg.answer_callback(query["id"], "Queued again")
            await self.mark(oid, "🔁 Queued again.")
        else:
            await self.tg.answer_callback(query["id"])
            if action == "ed":
                hint = " Put <code>Subject: …</code> on the first line to change the subject." if o["channel"] == "email" else ""
                if o["channel"] == "li_connect":
                    hint = f" Keep it under {self.settings.linkedin.note_limit} characters."
                text = f"✏️ Reply to this message with the new text for #{oid}.{hint}"
            else:
                text = (f"🔁 What should change in #{oid}? Reply with an instruction, "
                        "for example “shorter” or “mention my payments work”. Reply “go” to just redraft.")
            message_id = await self.tg.send(self.chat_id, text, force_reply=True, reply_to=o["tg_message_id"])
            self.db.add_prompt(message_id, "edit" if action == "ed" else "rewrite", oid)

    async def _on_text(self, message: dict[str, Any]) -> None:
        text = message["text"].strip()
        if text.startswith("/"):
            await self._command(text.split()[0].split("@")[0].lower())
            return
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        prompt = self.db.take_prompt(reply_to) if reply_to else None
        if prompt is None:  # a plain message counts only for a prompt from the last half hour
            prompt = self.db.take_prompt(newer_than=self.clock.ago(minutes=30))
        if prompt is None:
            await self.notify(HELP)
            return
        if prompt["kind"] == "answer":
            await self._save_answer(prompt["ref"], text)
        else:
            await self._revise(prompt, text)

    async def _save_answer(self, job_id: str, text: str) -> None:
        job = self.db.job(job_id)
        if job is None or not job["question"]:
            await self.notify("That question isn't open any more.")
            return
        self.db.learn_answer(job["question"], text, "you")
        self.db.set_job(job_id, status="queued", question=None, error=None)
        await self.notify(f"Saved. I'll use “{esc(text)}” for that question from now on and retry the application.")

    async def _revise(self, prompt: sqlite3.Row, text: str) -> None:
        o = self.db.outreach(prompt["ref"])
        if o is None or o["status"] != "pending_approval":
            await self.notify("That draft isn't open any more.")
            return
        if prompt["kind"] == "edit":
            subject, body = o["subject"], text
            first, _, rest = text.partition("\n")
            if o["channel"] == "email" and first.lower().startswith("subject:"):
                subject, body = first.split(":", 1)[1].strip(), rest.strip()
        else:
            try:
                draft = await self.rewrite(o, "" if text.lower() in ("go", "again", "redo") else text)
            except Exception as exc:
                log.exception("rewrite failed")
                await self.notify(f"Rewrite failed: {esc(exc)}")
                return
            subject, body = draft.subject or o["subject"], draft.body
        limit = self.settings.linkedin.note_limit
        if o["channel"] == "li_connect" and len(body) > limit:
            message_id = await self.tg.send(
                self.chat_id, f"That's {len(body)} characters; LinkedIn allows {limit}. Reply with a shorter one.",
                force_reply=True)
            self.db.add_prompt(message_id, prompt["kind"], o["id"])
            return
        self.db.set_outreach(o["id"], subject=subject, body=body)
        if o["tg_message_id"]:
            await self.tg.edit(self.chat_id, o["tg_message_id"],
                               f"{self.render(o)}\n\n✏️ Replaced by the new version below.")
        await self._show(self.db.outreach(o["id"]))

    async def _command(self, command: str) -> None:
        if command == "/status":
            await self.notify(self.summary())
        elif command == "/pause":
            self.db.put("paused", "1")
            await self.notify("⏸ Paused. Nothing will be applied to or sent until /resume.")
        elif command == "/resume":
            self.db.put("paused", "0")
            await self.notify("▶️ Resumed.")
        elif command == "/pending":
            open_cards = self.db.outreach_in("pending_approval")
            if not open_cards:
                await self.notify("No open cards.")
            for o in open_cards:
                await self._show(o)
        else:
            await self.notify(HELP)
