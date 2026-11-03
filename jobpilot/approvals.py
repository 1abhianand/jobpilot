"""Telegram approval flow. Nothing reaches another person until you tap Send on its card.

Cards: every draft (email, LinkedIn note or message, application note) arrives with
Send / Edit / Rewrite / Skip. Edit and Rewrite ask for your reply text; so do form questions
the bot can't answer on its own. Only messages from your own chat are ever acted on.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .clock import Clock, parse
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
SITE_ALIASES = {"li": "linkedin", "insta": "instahyre", "wf": "wellfound", "yc": "waas"}
SENT_FILTERS = {"email": ("email",), "linkedin": ("li_connect", "li_message"), "li": ("li_connect", "li_message")}
DEFAULT_DAYS = 7
COMMANDS = [  # the / menu in Telegram; keep in step with HELP and _command
    ("status", "Today's numbers"),
    ("applied", "Applications with links. e.g. /applied linkedin 30"),
    ("sent", "Emails and LinkedIn requests sent. e.g. /sent linkedin"),
    ("pending", "Show open approval cards again"),
    ("queued", "What you approved that hasn't gone out yet"),
    ("questions", "Ask the open application-form questions again"),
    ("failed", "Sends that failed, with Retry buttons"),
    ("run", "Run a task now. e.g. /run linkedin"),
    ("answers", "Form answers I've saved for you"),
    ("pause", "Stop all automation"),
    ("resume", "Restart automation"),
    ("help", "What I do and how to use me"),
]

HELP = """I send you every email, LinkedIn note and application note before it goes out.

On each card: <b>Send</b> approves it, <b>Edit</b> lets you reply with your own text, <b>Rewrite</b> asks for an instruction ("shorter", "mention my Kafka work") and redrafts, <b>Skip</b> drops it.

/status: today's numbers
/applied: applications from the last 7 days, with links. Add a site and/or days: <code>/applied linkedin</code>, <code>/applied insta 30</code>, <code>/applied wellfound all</code>
/sent: emails and LinkedIn requests/messages that went out, same options: <code>/sent linkedin 14</code>
/pending: show open cards again
/queued: what you approved that hasn't gone out yet
/questions: ask the open application-form questions again
/failed: sends that failed, with Retry buttons
/run: run a task now instead of waiting, e.g. <code>/run linkedin</code> or <code>/run insta</code>; on its own it lists the tasks
/answers: form answers I've saved for you
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
                 rewrite: Rewriter, send_now: Callable[[int], Awaitable[None]] | None = None,
                 outlook: Callable[[], list[tuple[str, int, str]]] | None = None):
        self.db = db
        self.tg = tg
        self.chat_id = chat_id
        self.settings = settings
        self.clock = clock
        self.rewrite = rewrite
        self.tasks: list[str] = []  # scheduled task names, filled in by the scheduler for /run
        self.send_now = send_now
        self.outlook = outlook
        self._sending: set[asyncio.Task[None]] = set()

    def _send_right_away(self, outreach_id: int) -> None:
        """Hand an approved item to the sender in the background, so the bot keeps answering meanwhile."""
        if self.send_now is None:
            return
        task = asyncio.create_task(self.send_now(outreach_id))
        self._sending.add(task)
        task.add_done_callback(self._sent)

    def _sent(self, task: asyncio.Task[None]) -> None:
        self._sending.discard(task)
        if not task.cancelled() and task.exception():
            log.error("sending right away failed", exc_info=task.exception())

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

    async def register_commands(self) -> None:
        """Make the / menu match COMMANDS. Cheap to call often: it only talks to Telegram when the list changed
        or the last attempt failed (e.g. the network was down at startup)."""
        wanted = json.dumps(COMMANDS)
        if self.db.get("menu_commands") == wanted:
            return
        try:
            await self.tg.set_commands(COMMANDS)
        except (httpx.HTTPError, TelegramError) as exc:  # only the menu is lost; typed commands still work
            log.warning("couldn't register the Telegram command menu: %s", exc)
            return
        self.db.put("menu_commands", wanted)

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
            if self.send_now is None:
                await self.mark(oid, "✅ Approved. It goes out with the next send run, in working hours.")
            else:
                await self.mark(oid, "✅ Approved. Sending now…")
                self._send_right_away(oid)
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
            await self.mark(oid, "🔁 Sending again now…" if self.send_now else "🔁 Queued again.")
            self._send_right_away(oid)
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
            await self._command(text)
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

    # -- history -------------------------------------------------------------------

    async def notify_long(self, lines: list[str]) -> None:
        """Send a list that may be longer than one Telegram message, split on line boundaries."""
        chunk: list[str] = []
        for line in lines:
            if chunk and sum(len(x) + 1 for x in chunk) + len(line) > MAX_CARD:
                await self.notify("\n".join(chunk))
                chunk = []
            chunk.append(line)
        if chunk:
            await self.notify("\n".join(chunk))

    def _period(self, args: list[str]) -> tuple[str, str, list[str]]:
        """Pull a day count ("30") or "all" out of command arguments: (since stamp, label, other args)."""
        days: int | None = DEFAULT_DAYS
        rest = []
        for arg in args:
            if arg.isdigit() and int(arg) > 0:
                days = int(arg)
            elif arg in ("all", "ever"):
                days = None
            else:
                rest.append(arg)
        if days is None:
            return "", "all time", rest
        if days == 1:
            return self.clock.day_start(), "today", rest
        return self.clock.ago(days=days), f"last {days} days", rest

    def _day(self, stamp: str | None) -> str:
        return parse(stamp).astimezone(self.clock.tz).strftime("%d %b %H:%M") if stamp else "?"

    @staticmethod
    def _site(arg: str) -> str | None:
        arg = SITE_ALIASES.get(arg, arg)
        for key, name in SITE_NAMES.items():
            if key.startswith(arg) or name.lower().replace(" ", "").startswith(arg):
                return key
        return None

    def applied_report(self, args: list[str]) -> list[str]:
        since, period, rest = self._period(args)
        site = None
        if rest:
            site = self._site(rest[0])
            if site is None:
                return [f"I don't know the site “{esc(rest[0])}”. Try one of: "
                        + ", ".join(SITE_NAMES) + "."]
        jobs = self.db.applications(since, site)
        where = f" on {SITE_NAMES[site]}" if site else ""
        if not jobs:
            return [f"No applications{where} ({period})."]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["site"]] = counts.get(job["site"], 0) + 1
        lines = [f"📝 <b>{len(jobs)} application{'s' if len(jobs) != 1 else ''}{where}</b> ({period})"]
        if not site:
            lines.append(", ".join(f"{SITE_NAMES.get(s, s)} {n}" for s, n in sorted(counts.items())))
        number = 0
        for key in sorted(counts):
            if not site:
                lines += ["", f"<b>{esc(SITE_NAMES.get(key, key))}</b> ({counts[key]})"]
            for job in (j for j in jobs if j["site"] == key):
                number += 1
                title = f'<a href="{attr(job["url"])}">{esc(job["title"] or "Job")}</a>'
                extra = " · ".join(esc(x) for x in (job["company"], job["location"]) if x)
                dry = " 🧪" if job["status"] == "dry_run" else ""
                lines.append(f"{number}. {self._day(job['applied_at'])}: {title}" + (f" · {extra}" if extra else "") + dry)
        if any(j["status"] == "dry_run" for j in jobs):
            lines += ["", "🧪 = dry run, not really submitted."]
        return lines

    def sent_report(self, args: list[str]) -> list[str]:
        since, period, rest = self._period(args)
        channels = ("email", "li_connect", "li_message")
        if rest:
            if rest[0] not in SENT_FILTERS:
                return [f"I don't know “{esc(rest[0])}”. Try <code>/sent email</code> or <code>/sent linkedin</code>."]
            channels = SENT_FILTERS[rest[0]]
        sent = self.db.sent_list(since, channels)
        if not sent:
            return [f"Nothing sent ({period})."]
        lines = [f"📤 <b>{len(sent)} sent</b> ({period})"]
        for o in sent:
            who = esc(o["person_name"] or o["to_addr"] or "?")
            if o["company_name"]:
                who += f" · {esc(o['company_name'])}"
            if o["channel"] in ("li_connect", "li_message") and o["to_addr"]:
                who = f'<a href="{attr(o["to_addr"])}">{who}</a>'
            label = LABELS[o["channel"]] + (" (follow-up)" if o["parent_id"] else "")
            state = " ↩️ replied" if o["replied_at"] else (" 🧪" if o["status"] == "dry_run" else "")
            lines.append(f"{ICONS[o['channel']]} #{o['id']} · {self._day(o['sent_at'])}: {who} ({label}){state}")
        return lines

    def queued_report(self) -> list[str]:
        queued = self.db.queued_list()
        if not queued:
            return ["Nothing queued. Everything you approved has gone out."]
        lines = [f"⏳ <b>{len(queued)} approved, waiting to go out</b>"]
        for o in queued:
            if o["channel"] == "apply_note":
                who = esc(o["job_title"] or "Job")
                if o["job_url"]:
                    who = f'<a href="{attr(o["job_url"])}">{who}</a>'
                if o["job_company"] and o["job_company"] not in (o["job_title"] or ""):
                    who += f" · {esc(o['job_company'])}"
            else:
                who = esc(o["person_name"] or o["to_addr"] or "?")
                if o["company_name"]:
                    who += f" · {esc(o['company_name'])}"
                if o["channel"] in ("li_connect", "li_message") and o["to_addr"]:
                    who = f'<a href="{attr(o["to_addr"])}">{who}</a>'
            state = " (sending now)" if o["status"] == "sending" else ""
            lines.append(f"{ICONS[o['channel']]} #{o['id']} {who} ({LABELS[o['channel']]}){state}")
        return lines

    async def ask_open_questions(self) -> None:
        jobs = self.db.rows("SELECT * FROM jobs WHERE status='needs_input' ORDER BY updated_at")
        if not jobs:
            await self.notify("No open form questions.")
        for job in jobs:
            await self.ask_answer(job, job["question"] or "?", [])

    async def show_failed(self) -> None:
        failed = self.db.outreach_in("failed")
        if not failed:
            await self.notify("Nothing failed.")
        for o in failed:
            reason = o["error"] or "unknown error"
            await self.failed(o["id"], reason, without_note=o["channel"] == "li_connect" and bool(o["body"])
                              and reason.startswith("note_quota"))

    def task_names(self) -> list[str]:
        """The scheduled tasks (set when the scheduler starts), else every task that has run before."""
        if self.tasks:
            return sorted(self.tasks)
        return sorted(row["key"][len("last:"):] for row in self.db.rows("SELECT key FROM kv WHERE key LIKE 'last:%'"))

    def resolve_task(self, arg: str) -> str | None:
        """A task name from what you typed: exact ("site:linkedin"), or a site's short name ("linkedin", "insta")."""
        names = self.task_names()
        if arg in names:
            return arg
        site = self._site(arg.removeprefix("site:"))
        if site and f"site:{site}" in names:
            return f"site:{site}"
        matches = [n for n in names if n.startswith(arg)]
        return matches[0] if len(matches) == 1 else None

    async def run_now(self, args: list[str]) -> None:
        names = self.task_names()
        task = self.resolve_task(args[0]) if args else None
        if task is None:
            unknown = f"I don't know the task “{esc(args[0])}”. " if args else ""
            await self.notify(f"{unknown}Tasks: " + ", ".join(f"<code>{esc(n)}</code>" for n in names)
                              + "\nFor example <code>/run linkedin</code> or <code>/run site:instahyre</code>.")
            return
        self.db.conn.execute("DELETE FROM kv WHERE key=?", (f"last:{task}",))
        paused = " You're paused, so it waits for /resume." if self.db.get("paused") == "1" else ""
        await self.notify(f"▶️ {esc(task)} runs within a minute, once anything already running finishes.{paused}")

    def answers_report(self) -> list[str]:
        saved = self.db.rows("SELECT question, answer FROM answers ORDER BY created_at")
        if not saved:
            return ["No saved form answers yet."]
        return [f"💾 <b>{len(saved)} saved answers</b>"] + [f"• {esc(a['question'])} → <b>{esc(a['answer'])}</b>"
                                                            for a in saved]

    def resume_report(self) -> str:
        """Unpause, start the held-back sends on the next tick, and say what happens now."""
        self.db.conn.execute("DELETE FROM kv WHERE key IN ('last:actions', 'email_next_at', 'actions_next_at')")
        lines = ["▶️ <b>Resumed.</b> Job search and applying pick up again within a minute."]

        def phrase(channel: str, n: int) -> str:
            label = LABELS[channel] if LABELS[channel].startswith("LinkedIn") else LABELS[channel].lower()
            return f"{ICONS[channel]} {n} {label}{'s' if n != 1 else ''}"

        outlook = self.outlook() if self.outlook else []
        going = [phrase(c, n) for c, n, why in outlook if not why]
        if going:
            lines += ["", "<b>Going out now</b> (one at a time, a little apart):", *going]
        waiting: dict[str, list[str]] = {}
        for c, n, why in outlook:
            if why:
                waiting.setdefault(why, []).append(phrase(c, n))
        for why, items in waiting.items():
            lines += ["", f"<b>Still waiting</b>: {esc(why)}", *items]
        open_cards = self.db.count_outreach("pending_approval")
        questions = self.db.count_jobs("needs_input")
        if open_cards or questions:
            lines += ["", f"Waiting on you: {open_cards} cards (/pending), {questions} form questions (/questions)."]
        if not outlook:
            lines += ["", "Nothing approved is queued. /queued shows the queue any time."]
        return "\n".join(lines)

    async def _command(self, text: str) -> None:
        command, *args = text.split()
        command = command.split("@")[0].lower()
        args = [a.lower() for a in args]
        if command == "/status":
            await self.notify(self.summary())
        elif command == "/applied":
            await self.notify_long(self.applied_report(args))
        elif command == "/sent":
            await self.notify_long(self.sent_report(args))
        elif command == "/pause":
            self.db.put("paused", "1")
            await self.notify("⏸ Paused. Nothing will be applied to or sent until /resume.")
        elif command == "/resume":
            self.db.put("paused", "0")
            await self.notify(self.resume_report())
        elif command == "/questions":
            await self.ask_open_questions()
        elif command == "/failed":
            await self.show_failed()
        elif command == "/run":
            await self.run_now(args)
        elif command == "/answers":
            await self.notify_long(self.answers_report())
        elif command == "/queued":
            await self.notify_long(self.queued_report())
        elif command == "/pending":
            open_cards = self.db.outreach_in("pending_approval")
            if not open_cards:
                await self.notify("No open cards.")
            for o in open_cards:
                await self._show(o)
        else:
            await self.notify(HELP)
