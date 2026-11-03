"""The work: find and apply to jobs, prepare outreach, send what you approved, follow up.

Every task takes the App and returns a one-line summary for the log ("" when there was nothing
to do). Emails, LinkedIn notes and messages, and application notes are only ever sent after you
approve them on Telegram. One-click applications (LinkedIn Easy Apply, Instahyre, Hirist) go out
on their own, within the daily caps, because that is what you asked for.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING

import anthropic
import httpx
from playwright.async_api import Page

from . import yc
from .approvals import esc
from .browser import LoggedOut, pause
from .clock import iso, parse
from .db import meta
from .email_finder import domain_of
from .llm import Draft, LLMError
from .mail import as_text, build_message, scan_inbox, send
from .roles import role_searches
from .rules import below_salary_floor, company_excluded, location_ok, reject_reason, salary_max_lpa
from .sites.base import ApplyResult, Outcome, Site
from .sites.linkedin import LinkedIn, Reach, names_match, profile_url

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)
# The connection dropped or a page never loaded: the job is fine, so it stays queued for the next run.
NETWORK = re.compile(r"net::ERR_|Page\.goto: Timeout|name resolution|Request timed out|Connection error", re.I)

BROWSER_CHANNELS = ("li_connect", "li_message", "apply_note")
ACTION_GAPS = {"li_connect": (90, 240), "li_message": (60, 180), "apply_note": (45, 120)}  # seconds
# After each LinkedIn request or message, LinkedIn alone rests this long (seconds): a few spread-out actions a
# day, never a burst. Bursts of messages are what got the account restricted on 2026-10-12.
LINKEDIN_REST = (20 * 60, 45 * 60)
LINKEDIN_APPLY_PER_RUN = 4  # Easy Apply submissions per LinkedIn run
LINKEDIN_APPLY_GAP = (120, 300)  # seconds between them, about what reading and applying by hand takes
LINKEDIN_LOCK = "linkedin_locked"  # kv: LinkedIn showed a login/security check; nothing touches it until you log in
APPLY_BATCH = 8  # approved applications carried out back to back in one send run
INSTANT_GAP = (15, 40)  # seconds between items sent the moment you approve them
OPEN = ("draft", "pending_approval", "approved", "sending", "waiting")
FATAL_API_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)
PROFILE_URL = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#\s]+", re.IGNORECASE)
COMPANY_URL = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/company/[^/?#\s]+", re.IGNORECASE)


def _paused(app: App) -> bool:
    return app.db.get("paused") == "1"


def _linkedin(app: App) -> LinkedIn:
    return app.sites["linkedin"]  # type: ignore[return-value]


def linkedin_usable(app: App) -> bool:
    """LinkedIn is switched on and hasn't been locked by a security check you haven't cleared yet."""
    return app.settings.linkedin.enabled and not app.db.get(LINKEDIN_LOCK)


LINKEDIN_FAILS = "linkedin_failures_in_a_row"
MAX_LINKEDIN_FAILS = 3  # this many failed LinkedIn sends in a row means something changed: stop and look
SAME_PERSON_DAYS = 3  # never two LinkedIn messages to one person within this many days
TEAM_GREETING = re.compile(r"^\s*(?:hi|hello|hey|dear)\s+(.+?)\s+team\b", re.IGNORECASE)
GENERIC_WORDS = {"ai", "inc", "labs", "lab", "technologies", "technology", "tech", "the", "co", "io", "hq", "app"}
GREETING = re.compile(r"^\s*(?:hi|hello|hey|dear)\s+([^\s,!.:\n]+)", re.IGNORECASE)


def linkedin_stopped(why: str) -> str:
    return (f"🔒 {esc(why)}\n\nSo <b>all LinkedIn activity is stopped</b>. Check the account in your own browser, "
            "then run <code>jobpilot login linkedin</code> to let jobpilot use it again. Pushing on through a "
            "warning is what gets accounts restricted.")


def lock_linkedin(app: App, why: str) -> bool:
    """Stop everything on LinkedIn until you log in again. True the first time, so you're told once."""
    if app.db.get(LINKEDIN_LOCK):
        return False
    app.db.put(LINKEDIN_LOCK, app.clock.stamp())
    log.warning("LinkedIn locked: %s", why)
    return True


def linkedin_resting(app: App) -> bool:
    rest = app.db.get("linkedin_rest_until")
    return bool(rest) and rest > app.clock.stamp()


def wrong_greeting(body: str, name: str | None) -> str | None:
    """The name a draft greets when it isn't the recipient's ("Hi Prateek" in a message to Arushi), else None."""
    found = GREETING.match(body or "")
    if not found or not name or found.group(1).lower() in ("there", "team", "all", "folks", "everyone"):
        return None
    return None if names_match(name, found.group(1)) else found.group(1)


def _profile_or_none(url: str | None) -> str | None:
    found = PROFILE_URL.match((url or "").strip())
    return profile_url(found.group(0)) if found else None


def _company_page_or_none(url: str | None) -> str | None:
    found = COMPANY_URL.match((url or "").strip())
    return found.group(0) + "/" if found else None


# -- job sites ---------------------------------------------------------------------------------

async def run_site(app: App, name: str) -> str:
    """Find new jobs on one site, score them against your resume, then act on the best ones."""
    settings = app.settings.site(name)
    site = app.sites[name]
    counts: Counter[str] = Counter()
    urls = list(dict.fromkeys([*(settings.search_urls or site.default_searches),
                               *role_searches(name, app.settings.profile)]))
    fresh = app.db.jobs(name, ["new"], limit=settings.reads_per_run)
    one_click_ready = not site.needs_note and app.db.jobs(name, ["matched", "queued"], limit=1)
    if urls or any(not job["description"] for job in fresh) or one_click_ready:
        async with app.browser.session() as page:
            if urls:
                known = {r["external_id"] for r in app.db.rows("SELECT external_id FROM jobs WHERE site=?", name)}
                for stub in await site.discover(page, urls, limit=settings.reads_per_run * 4, known=known):
                    if app.db.add_job(name, stub.external_id, stub.url, title=stub.title,
                                      meta={"hint": stub.hint[:600]}):
                        counts["new"] += 1
            await _score_new(app, site, counts, page)
            if not site.needs_note:
                await _apply_matches(app, page, site, counts)
    else:
        await _score_new(app, site, counts, None)
    if site.needs_note:
        await _draft_notes(app, site, counts)
    return f"{site.label}: " + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()) or "nothing new")


async def _score_new(app: App, site: Site, counts: Counter[str], page: Page | None) -> None:
    for job in app.db.jobs(site.name, ["new"], limit=app.settings.site(site.name).reads_per_run):
        if _paused(app):
            return
        counts[await _read_and_score(app, site, job, page)] += 1


def _retry_later(app: App, job: sqlite3.Row, exc: Exception) -> str:
    tries = meta(job).get("tries", 0) + 1
    app.db.set_job(job["id"], status="failed" if tries >= 3 else job["status"],
                   error=f"{type(exc).__name__}: {exc}"[:300], meta={**meta(job), "tries": tries})
    log.warning("job %s: %s", job["id"], exc)
    return "errors"


async def _read_and_score(app: App, site: Site, job: sqlite3.Row, page: Page | None) -> str:
    title, company, location, description = job["title"], job["company"], job["location"], job["description"]
    try:
        if not description:
            if page is None:
                return "waiting"
            info = await site.read(page, job["url"])
            title, company = info.title or title, info.company or company
            if info.applied:
                app.db.set_job(job["id"], status="applied_before", title=title, company=company)
                return "applied_before"
            if not info.can_apply:
                app.db.set_job(job["id"], status="unavailable", title=title, company=company,
                               error="no apply button on the job page")
                return "unavailable"
            location, description = info.location or location, info.description
        if reason := reject_reason(app.settings.filters, title, company):
            app.db.set_job(job["id"], status="filtered", reason=reason, title=title, company=company,
                           description=description)
            return "filtered"
        card = meta(job).get("hint")  # listing cards often show the pay the job page leaves out
        score = await app.llm.score_job(site=site.label, url=job["url"], title=title, company=company,
                                        location=location,
                                        description=f"{description}\n\nListing card: {card}" if card else description)
    except (LoggedOut, *FATAL_API_ERRORS):
        raise
    except (LLMError, anthropic.BadRequestError) as exc:
        app.db.set_job(job["id"], status="failed", error=f"scoring: {exc}"[:300])
        return "errors"
    except Exception as exc:
        return _retry_later(app, job, exc)
    title, company, location = title or score.title, company or score.company, location or score.location
    reason = reject_reason(app.settings.filters, title, company)  # the company may only be known now
    floor = app.settings.filters.min_salary_lpa
    if not reason and below_salary_floor(score.salary_max_lpa, floor):
        reason = f"stated pay tops out at {score.salary_max_lpa:g} LPA, under your {floor:g} LPA minimum"
    status = "filtered" if reason else (
        "matched" if score.score >= app.settings.site(site.name).min_score else "low_score")
    app.db.set_job(job["id"], status=status, title=title, company=company, location=location,
                   description=description, score=score.score, reason=reason or score.reason, error=None)
    return status


async def _apply_matches(app: App, page: Page, site: Site, counts: Counter[str]) -> None:
    room = app.settings.site(site.name).apply_per_day - app.db.applied_since(site.name, app.clock.day_start())
    if site.name == "linkedin":  # a few per run, spread through the day, never a burst
        room = min(room, LINKEDIN_APPLY_PER_RUN)
    for job in app.db.jobs(site.name, ["queued", "matched"], limit=max(room, 0), best_first=True):
        if _paused(app):
            return
        result = await _apply(app, page, site, job, note=None)
        counts[result.outcome.value] += 1
        await pause(*(LINKEDIN_APPLY_GAP if site.name == "linkedin" else (20, 60)))


async def _apply(app: App, page: Page, site: Site, job: sqlite3.Row, note: str | None) -> ApplyResult:
    label = " at ".join(x for x in (job["title"], job["company"]) if x) or None
    try:
        result = await site.apply(page, job["url"], note, app.settings.dry_run, label)
    except LoggedOut:
        raise
    except Exception as exc:
        if NETWORK.search(str(exc)):
            log.warning("applying to %s hit a network problem; retrying next run: %s", job["id"], str(exc)[:160])
            return ApplyResult(Outcome.FAILED, "network problem; stays queued")
        log.exception("applying to %s failed", job["id"])
        result = ApplyResult(Outcome.FAILED, f"{type(exc).__name__}: {exc}"[:300])
    if result.outcome is Outcome.NEEDS_INPUT and not result.question:
        result = ApplyResult(Outcome.FAILED, "the form has a required field without a label")
    if result.outcome is Outcome.FAILED:
        result.detail += f" (screenshot: {await app.browser.screenshot(page, job['id'])})"
    fields: dict[str, object] = {"status": result.outcome.value, "error": result.detail or None}
    if result.outcome in (Outcome.APPLIED, Outcome.DRY_RUN):
        fields["applied_at"] = app.clock.stamp()
    if result.outcome is Outcome.NEEDS_INPUT:
        fields["question"] = result.question
    app.db.set_job(job["id"], **fields)
    app.db.log(f"apply:{result.outcome.value}", job["id"], result.detail)
    if result.outcome is Outcome.NEEDS_INPUT:
        await app.bot.ask_answer(app.db.job(job["id"]), result.question, result.options, result.detail)
    return result


async def _draft_notes(app: App, site: Site, counts: Counter[str]) -> None:
    room = app.settings.site(site.name).apply_per_day - app.db.notes_drafted_since(site.name, app.clock.day_start())
    for job in app.db.jobs(site.name, ["matched"], limit=max(room, 0), best_first=True):
        if not app.db.has_outreach("apply_note", job_id=job["id"]):
            try:
                await draft_application_note(app, job)
            except LLMError as exc:
                app.db.set_job(job["id"], status="failed", error=f"drafting: {exc}")
                continue
            counts["notes drafted"] += 1
        app.db.set_job(job["id"], status="awaiting_approval")


# -- drafting ---------------------------------------------------------------------------------

def _company_text(company: sqlite3.Row | None) -> str:
    if company is None:
        return ""
    extra = meta(company)
    facts = [
        ("Name", company["name"] + (f" (YC {company['yc_batch']})" if company["yc_batch"] else "")),
        ("Website", company["website"]),
        ("One-liner", company["one_liner"]),
        ("About", company["description"]),
        ("Location", company["location"]),
        ("Team size", company["team_size"]),
        ("Stage", extra.get("stage")),
        ("Tags", ", ".join(extra.get("tags") or [])),
        ("Why it fits", company["fit_reason"]),
        ("Open roles", "; ".join(extra.get("roles") or [])),
    ]
    return "\n".join(f"{label}: {value}" for label, value in facts if value)


def _recipient(person: sqlite3.Row | None) -> str | None:
    if person is None:
        return None
    text = person["name"] + (f", {person['title']}" if person["title"] else "")
    bio = meta(person).get("bio")
    return f"{text}\nAbout them: {bio}" if bio else text


def _job_text(job: sqlite3.Row | None) -> str | None:
    if job is None:
        return None
    header = "\n".join(f"{label}: {value}" for label, value in
                       (("Title", job["title"]), ("Company", job["company"]), ("Location", job["location"])) if value)
    return f"{header}\n\n{(job['description'] or '')[:8000]}".strip()


def _signed(app: App, body: str) -> str:
    signature = app.settings.email.signature.strip()
    return f"{body.rstrip()}\n\n{signature}" if signature else body.rstrip()


def _unsigned(app: App, body: str) -> str:
    signature = app.settings.email.signature.strip()
    if signature and body.rstrip().endswith(signature):
        return body.rstrip()[: -len(signature)].rstrip()
    return body


def note_from_template(app: App, job: sqlite3.Row, company: sqlite3.Row | None) -> str:
    name = (company["name"] if company else None) or job["company"] or "your"
    role = (job["title"] or app.settings.email.default_role).strip()
    return app.settings.outreach.apply_note_template.format_map({"company": name, "role": role}).strip()


async def draft_application_note(app: App, job: sqlite3.Row) -> int:
    company = app.db.company(meta(job).get("company_id"))
    if app.settings.outreach.apply_note_template:
        draft = Draft(subject=None, body=note_from_template(app, job, company))
    else:
        draft = await app.llm.write("apply_note", company=_company_text(company) or None, job=_job_text(job))
    auto = {"status": "approved", "approved_at": app.clock.stamp(), "why": "sent without asking (auto_send_notes)"} \
        if app.settings.site(job["site"]).auto_send_notes else {}
    return app.db.add_outreach("apply_note", draft.body, job_id=job["id"], to_addr=job["url"],
                               company_id=company["id"] if company else None, **auto)


async def draft_connect(app: App, person: sqlite3.Row, company: sqlite3.Row) -> int | None:
    if not person["linkedin_url"] or app.db.has_outreach("li_connect", person_id=person["id"]):
        return None
    note = ""
    if app.settings.linkedin.send_notes and app.settings.outreach.connect_template:
        note = connect_note(app, person, company)
    elif app.settings.linkedin.send_notes:
        draft = await app.llm.write("li_connect", recipient=_recipient(person), company=_company_text(company),
                                    limit=app.settings.linkedin.note_limit)
        note = draft.body
    return app.db.add_outreach("li_connect", note, person_id=person["id"], company_id=company["id"],
                               to_addr=person["linkedin_url"], why=company["fit_reason"])


ROLE_WORDS = re.compile(r"\b(backend|back-end|ai|ml|machine learning|llm|software|sde|fde|forward[- ]deployed|full[- ]?stack|platform|engineer)\b",
                        re.IGNORECASE)


def pick_role(app: App, company: sqlite3.Row) -> str:
    """The company's open role that suits you best, for the email template; a default when none is listed."""
    for listed in meta(company).get("roles") or []:
        title = listed.split(" (", 1)[0].strip()  # "Backend Engineer (Bengaluru, KA / Remote (IN))" -> title
        if title and ROLE_WORDS.search(title) and not reject_reason(app.settings.filters, title, None):
            return title
    return app.settings.email.default_role


def from_template(app: App, person: sqlite3.Row, company: sqlite3.Row) -> Draft:
    fields = {"first_name": (person["name"].split() or ["there"])[0], "company": company["name"],
              "role": pick_role(app, company)}
    settings = app.settings.email
    return Draft(subject=settings.template_subject.format_map(fields).strip(),
                 body=settings.template_body.format_map(fields).strip())


def connect_note(app: App, person: sqlite3.Row, company: sqlite3.Row) -> str:
    fields = {"first_name": (person["name"].split() or ["there"])[0], "company": company["name"],
              "role": pick_role(app, company)}
    note = app.settings.outreach.connect_template.format_map(fields).strip()
    limit = app.settings.linkedin.note_limit
    if len(note) > limit:  # a long role title could push it over; fall back to the generic role
        note = app.settings.outreach.connect_template.format_map({**fields, "role": "engineering"}).strip()
    return note[:limit]


async def draft_email(app: App, person: sqlite3.Row, company: sqlite3.Row) -> int | None:
    if app.db.has_outreach("email", person_id=person["id"]):
        return None
    address = person["email"]
    if not address:
        address, source = await app.finder.find(person["name"], company["domain"])
        app.db.set_person(person["id"], email=address, email_source=source)
        if not address:
            return None
    if app.settings.email.template_body:
        draft = from_template(app, person, company)
    else:
        draft = await app.llm.write("email", recipient=_recipient(person), company=_company_text(company))
    return app.db.add_outreach("email", _signed(app, draft.body), subject=draft.subject or f"Engineering at {company['name']}",
                               person_id=person["id"], company_id=company["id"], to_addr=address,
                               why=company["fit_reason"])


async def draft_message(app: App, person: sqlite3.Row) -> int | None:
    if not person["linkedin_url"] or app.db.has_outreach("li_message", person_id=person["id"]):
        return None
    company = app.db.company(person["company_id"])
    draft = await app.llm.write("li_message", recipient=_recipient(person), company=_company_text(company) or None)
    return app.db.add_outreach("li_message", draft.body, person_id=person["id"], company_id=person["company_id"],
                               to_addr=person["linkedin_url"], why="Accepted your connection request")


async def draft_followup(app: App, original: sqlite3.Row) -> int:
    person, company = app.db.person(original["person_id"]), app.db.company(original["company_id"])
    earlier = f"Subject: {original['subject']}\n\n{_unsigned(app, original['body'])}"
    draft = await app.llm.write("followup", recipient=_recipient(person), company=_company_text(company) or None,
                                previous=earlier)
    subject = original["subject"] or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    sent_on = parse(original["sent_at"]).astimezone(app.clock.tz).strftime("%d %b")
    return app.db.add_outreach("email", _signed(app, draft.body), subject=subject, parent_id=original["id"],
                               person_id=original["person_id"], company_id=original["company_id"],
                               to_addr=original["to_addr"], why=f"No reply to your email of {sent_on}")


async def rewrite(app: App, o: sqlite3.Row, instruction: str) -> Draft:
    """Redraft a card for the Telegram Rewrite button, keeping its recipient and context."""
    person, company = app.db.person(o["person_id"]), app.db.company(o["company_id"])
    job = app.db.job(o["job_id"]) if o["job_id"] else None
    channel, previous = o["channel"], None
    if o["parent_id"]:
        parent = app.db.outreach(o["parent_id"])
        channel, previous = "followup", f"Subject: {parent['subject']}\n\n{_unsigned(app, parent['body'])}"
    draft = await app.llm.write(
        channel, recipient=_recipient(person), company=_company_text(company) or None, job=_job_text(job),
        previous=previous, draft=_unsigned(app, o["body"]) if o["channel"] == "email" else o["body"],
        instruction=instruction or "Write a fresh version from a different angle.",
        limit=app.settings.linkedin.note_limit if channel == "li_connect" else None,
    )
    if o["channel"] != "email":
        return Draft(subject=None, body=draft.body)
    subject = o["subject"] if o["parent_id"] else (draft.subject or o["subject"])
    return Draft(subject=subject, body=_signed(app, draft.body))


async def _draft_for(app: App, company: sqlite3.Row, person_ids: list[int]) -> None:
    """Connection requests and cold emails for the best-ranked people at a company."""
    people = [app.db.person(pid) for pid in person_ids]
    wanted = app.settings.outreach
    try:
        if app.settings.linkedin.enabled:
            for person in [p for p in people if p["linkedin_url"]][: wanted.linkedin_per_company]:
                await draft_connect(app, person, company)
        if app.settings.email.enabled and app.mailbox is not None:
            for person in people[: wanted.email_per_company]:
                await draft_email(app, person, company)
    except LLMError as exc:
        log.warning("drafting for %s failed: %s", company["name"], exc)


# -- YC ------------------------------------------------------------------------------------

async def sync_yc(app: App) -> str:
    """Refresh the hiring YC companies that match your filters and score a batch of new ones."""
    settings = app.settings.yc
    entries = yc.prefilter(await yc.fetch_hiring(app.http), settings, app.settings.filters.company_exclude)
    new = 0
    for entry in entries:
        domain = domain_of(entry.get("website"))
        key = domain or f"yc:{entry['slug']}"
        new += app.db.row("SELECT 1 FROM companies WHERE key=?", key) is None
        app.db.upsert_company(
            key, entry["name"], "yc", website=entry.get("website"), domain=domain, yc_slug=entry.get("slug"),
            yc_batch=yc.short_batch(entry.get("batch")), one_liner=entry.get("one_liner"),
            description=entry.get("long_description"), location=entry.get("all_locations"),
            team_size=entry.get("team_size"),
            meta={"stage": entry.get("stage"), "tags": entry.get("tags") or [], "summary": yc.company_summary(entry),
                  "regions": entry.get("regions") or []},
        )
    scored = 0
    for company in app.db.companies("new", source="yc", limit=settings.score_per_day, newest_batch_first=True):
        if _paused(app):
            break
        try:
            fit = await app.llm.score_company(meta(company).get("summary") or _company_text(company))
        except LLMError as exc:
            app.db.set_company(company["id"], status="pass", fit_reason=f"scoring failed: {exc}")
            continue
        app.db.set_company(company["id"], fit=fit.score, fit_reason=fit.reason,
                           status="target" if fit.score >= settings.min_fit else "pass")
        scored += 1
    return f"YC: {len(entries)} hiring companies match your filters ({new} new); scored {scored}."


async def _add_waas_jobs(app: App, company: sqlite3.Row, postings: list[dict]) -> list[str]:
    """Record the company's open roles; engineering roles you can take become Work at a Startup jobs."""
    roles = []
    for posting in postings:
        title, where = posting.get("title") or "", posting.get("location") or ""
        roles.append(f"{title} ({where})" if where else title)
        if not app.settings.waas.enabled or posting.get("role") != "eng":
            continue
        if not location_ok(where, app.settings.yc.job_locations) or reject_reason(app.settings.filters, title,
                                                                                    company["name"]):
            continue
        if below_salary_floor(salary_max_lpa(posting.get("salaryRange")), app.settings.filters.min_salary_lpa):
            continue
        details: dict = {}
        try:
            details = await yc.job_page(app.http, posting["url"])
        except (httpx.HTTPError, ValueError) as exc:
            log.info("YC job page %s: %s", posting.get("url"), exc)
        job = details.get("job") or {}
        process = f"Interview process:\n{job['interview_process']}" if job.get("interview_process") else ""
        description = "\n\n".join(part for part in (yc.job_summary(posting), job.get("description"), process) if part)
        questions = [q["label"] for q in details.get("customQuestions") or [] if q.get("label")]
        app.db.add_job("waas", str(posting["id"]), yc.WAAS_JOB_URL.format(id=posting["id"]), title=title,
                       company=company["name"], location=where, description=description,
                       meta={"company_id": company["id"], "yc_url": yc.YC_URL + posting["url"],
                             "questions": questions})
    return roles


def _pays_below_floor(app: App, postings: list[dict]) -> str:
    """A YC company is skipped when every engineering role you could take that states its pay pays under your
    minimum (a well-paid US-only role doesn't count)."""
    floor = app.settings.filters.min_salary_lpa
    stated = [pay for p in postings
              if p.get("role") == "eng" and location_ok(p.get("location") or "", app.settings.yc.job_locations)
              if (pay := salary_max_lpa(p.get("salaryRange"))) is not None]
    if stated and all(below_salary_floor(pay, floor) for pay in stated):
        return f"engineering pay tops out at {max(stated):g} LPA, under your {floor:g} LPA minimum"
    return ""


async def prepare_yc(app: App) -> str:
    """For today's best YC targets: record their jobs, find the founders, draft outreach."""
    room = app.settings.yc.companies_per_day - app.db.drafted_since("yc", app.clock.day_start())
    done = 0
    priority = {r.lower() for r in app.settings.yc.priority_regions}
    targets = app.db.companies("target", source="yc", limit=5000, best_first=True)
    targets.sort(key=lambda c: not priority & {r.lower() for r in meta(c).get("regions") or []})  # stable: fit order kept
    for company in targets[: max(room, 0)]:
        if _paused(app):
            break
        try:
            props = await yc.company_page(app.http, company["yc_slug"])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("YC page for %s failed: %s", company["name"], exc)
            continue
        postings = yc.jobs(props)
        roles = await _add_waas_jobs(app, company, postings)
        app.db.set_company(company["id"], meta={**meta(company), "roles": roles})
        if reason := _pays_below_floor(app, postings):
            app.db.set_company(company["id"], status="pass", fit_reason=reason)
            continue
        company = app.db.company(company["id"])
        person_ids = [app.db.upsert_person(company["id"], f["name"], _profile_or_none(f["linkedin_url"]),
                                           title=f["title"], meta={"bio": f["bio"]})
                      for f in yc.founders(props)]
        await _draft_for(app, company, person_ids)
        app.db.set_company(company["id"], status="drafted", drafted_at=app.clock.stamp())
        done += 1
    return f"YC: prepared outreach for {done} companies." if done else ""


# -- other startups ----------------------------------------------------------------------

async def run_scout(app: App) -> str:
    """Ask Claude, with web search, for recently funded startups that fit you."""
    settings = app.settings.scout
    found = await app.llm.scout(brief=settings.brief, n=settings.per_run, exclude=app.db.company_names()[:300])
    added = 0
    for startup in found:
        domain = domain_of(startup.website)
        if not domain or company_excluded(startup.name, app.settings.filters.company_exclude):
            continue
        if app.db.row("SELECT 1 FROM companies WHERE key=? OR lower(name)=lower(?)", domain, startup.name):
            continue
        company_id = app.db.upsert_company(
            domain, startup.name, "scout", status="target", website=startup.website, domain=domain,
            linkedin_url=_company_page_or_none(startup.linkedin_url), location=startup.location,
            description=startup.why, fit_reason=startup.why, meta={"stage": startup.stage},
        )
        for person in startup.people:
            app.db.upsert_person(company_id, person.name, _profile_or_none(person.linkedin_url), title=person.title,
                                 meta={"source": "web search"})
        added += 1
    return f"Scout: found {added} new startups."


async def _look_up_people(app: App, companies: list[sqlite3.Row]) -> None:
    """Find founders and engineering leaders on each company's LinkedIn People page."""
    key = f"lookups:{app.clock.local().date()}"
    used = int(app.db.get(key, "0") or 0)
    budget = app.settings.linkedin.lookups_per_day - used
    if budget <= 0 or not companies or not linkedin_usable(app):
        return
    li = _linkedin(app)
    wanted = max(app.settings.outreach.linkedin_per_company, app.settings.outreach.email_per_company)
    async with app.browser.session() as page:
        for company in companies[:budget]:
            used += 1
            app.db.put(key, str(used))
            url = _company_page_or_none(company["linkedin_url"]) or await li.company_url(page, company["name"])
            app.db.set_company(company["id"], linkedin_url=url, meta={**meta(company), "looked_up": True})
            if not url:
                continue
            candidates = await li.people(page, url)
            listing = [f"{c['name']}: {c['headline']}" for c in candidates]
            for index in await app.llm.pick_people(company=company["name"], candidates=listing, k=wanted):
                chosen = candidates[index]
                app.db.upsert_person(company["id"], chosen["name"], profile_url(chosen["url"]),
                                     title=chosen["headline"][:150], meta={"source": "linkedin"})
            await pause(10, 30)


async def prepare_scouted(app: App) -> str:
    room = app.settings.scout.companies_per_day - app.db.drafted_since("scout", app.clock.day_start())
    targets = app.db.companies("target", source="scout", limit=max(room, 0))
    if not targets:
        return ""
    if app.settings.linkedin.enabled:
        await _look_up_people(app, [c for c in targets if not meta(c).get("looked_up") and
                                    not any(p["linkedin_url"] for p in app.db.people_of(c["id"]))])
    done = 0
    for company in targets:
        company = app.db.company(company["id"])
        people = app.db.people_of(company["id"])
        if not people:
            if meta(company).get("looked_up") or not app.settings.linkedin.enabled:
                app.db.set_company(company["id"], status="pass",
                                   fit_reason=f"{company['fit_reason'] or ''} (nobody to contact found)".strip())
            continue  # otherwise wait for tomorrow's lookup budget
        await _draft_for(app, company, [p["id"] for p in people])
        app.db.set_company(company["id"], status="drafted", drafted_at=app.clock.stamp())
        done += 1
    return f"Scout: prepared outreach for {done} startups." if done else ""


# -- sending what you approved -------------------------------------------------------------

def _room(app: App, channel: str) -> bool:
    """Whether today's (and this week's) cap leaves room for one more on this channel."""
    day = app.clock.day_start()
    linkedin = app.settings.linkedin
    if channel.startswith("li_") and (not linkedin_usable(app) or linkedin_resting(app)):
        return False
    if channel == "email":
        return app.db.sent_since("email", day) < app.settings.email.per_day
    if channel == "li_connect":
        blocked = app.db.get("li_connect_blocked_until")
        if blocked and blocked > app.clock.stamp():
            return False
        return (app.db.sent_since("li_connect", day) < linkedin.connect_per_day and
                app.db.sent_since("li_connect", app.clock.ago(days=7)) < linkedin.connect_per_week)
    if channel == "li_message":
        return app.db.sent_since("li_message", day) < linkedin.messages_per_day
    return True


def _apply_room(app: App, o: sqlite3.Row) -> bool:
    if o["channel"] != "apply_note":
        return True
    job = app.db.job(o["job_id"])
    if not app.settings.site(job["site"]).enabled:
        return False
    return app.db.applied_since(job["site"], app.clock.day_start()) < app.settings.site(job["site"]).apply_per_day


async def send_email_queue(app: App) -> str:
    """Send the oldest approved email, at most one every few minutes."""
    if app.mailbox is None:
        return ""
    next_at = app.db.get("email_next_at")
    if (next_at and next_at > app.clock.stamp()) or not _room(app, "email") or app.clock.holding("email"):
        return ""
    queue = app.db.outreach_in("approved", ["email"], limit=1)
    if not queue:
        return ""
    await _send_email(app, queue[0])
    gap = app.settings.email.min_gap_minutes * random.uniform(1.0, 1.6)
    app.db.put("email_next_at", iso(app.clock.now() + timedelta(minutes=gap)))
    return f"email #{queue[0]['id']} handled"


async def _send_email(app: App, o: sqlite3.Row) -> None:
    if refused := _refusal(app, o):
        app.db.set_outreach(o["id"], status="failed", error=refused)
        await app.bot.failed(o["id"], refused)
        return
    parent = app.db.outreach(o["parent_id"]) if o["parent_id"] else None
    if parent is not None and parent["replied_at"]:
        app.db.set_outreach(o["id"], status="cancelled", error="they already replied")
        await app.bot.mark(o["id"], "🛑 Not sent: they already replied.")
        return
    stamp = app.clock.stamp()
    if app.settings.dry_run:
        app.db.set_outreach(o["id"], status="dry_run", sent_at=stamp)
        await app.bot.mark(o["id"], "🧪 Dry run: this is when it would have been sent.")
        return
    attach = app.settings.email.attach_resume and parent is None and app.paths.resume_pdf.exists()
    message = build_message(app.mailbox, o["to_addr"], o["subject"] or "", o["body"],
                            in_reply_to=parent["message_id"] if parent else None,
                            attachment=app.paths.resume_pdf if attach else None)
    app.db.set_outreach(o["id"], status="sending")
    try:
        await asyncio.to_thread(send, app.mailbox, message)
    except Exception as exc:
        log.exception("email #%s failed", o["id"])
        app.db.set_outreach(o["id"], status="failed", error=str(exc)[:300])
        await app.bot.failed(o["id"], str(exc)[:200])
        return
    app.db.set_outreach(o["id"], status="sent", sent_at=stamp, message_id=message["Message-ID"], error=None)
    app.db.log("email:sent", o["id"], o["to_addr"])
    await app.bot.mark(o["id"], f"📤 Sent at {app.clock.local():%H:%M}.")


async def send_browser_queue(app: App) -> str:
    """Carry out the oldest approved LinkedIn request, message or noted application, then free the browser:
    the gap before the next one is spent applying on the job sites instead of waiting."""
    app.db.release_waiting()
    next_at = app.db.get("actions_next_at")
    if next_at and next_at > app.clock.stamp():
        return ""
    queue = [o for o in app.db.outreach_in("approved", BROWSER_CHANNELS, limit=50)
             if _room(app, o["channel"]) and _apply_room(app, o) and not app.clock.holding(o["channel"])]
    if not queue:
        return ""
    o = queue[0]
    done: Counter[str] = Counter()
    async with app.browser.session() as page:
        if app.db.outreach(o["id"])["status"] != "approved":
            return ""  # sent the moment it was approved, while Chrome was starting
        await _browser_action(app, page, o)
        # Applications go in a batch, a short gap apart, instead of one per pass of the whole schedule.
        # LinkedIn items never do: each one is followed by LinkedIn's long rest.
        while o["channel"] == "apply_note" and sum(done.values()) < APPLY_BATCH - 1 and not _paused(app):
            done[app.db.outreach(o["id"])["status"]] += 1
            more = [n for n in app.db.outreach_in("approved", ["apply_note"], limit=5)
                    if _apply_room(app, n) and not app.clock.holding("apply_note")]
            if not more:
                break
            await pause(*ACTION_GAPS["apply_note"])
            o = more[0]
            await _browser_action(app, page, o)
    if done:
        done[app.db.outreach(o["id"])["status"]] += 1
        if done["sent"]:
            gap = random.uniform(*ACTION_GAPS["apply_note"])
            app.db.put("actions_next_at", iso(app.clock.now() + timedelta(seconds=gap)))
        return "applications: " + ", ".join(f"{n} {status}" for status, n in done.items())
    if app.db.outreach(o["id"])["status"] == "sent":
        gap = random.uniform(*ACTION_GAPS[o["channel"]])
        app.db.put("actions_next_at", iso(app.clock.now() + timedelta(seconds=gap)))
    if o["channel"].startswith("li_"):  # failed or not, LinkedIn rests: a run of quick failures looks like a bot
        rest_linkedin(app)
    return f"{o['channel'].replace('_', ' ')} #{o['id']}: {app.db.outreach(o['id'])['status']}"


def rest_linkedin(app: App) -> None:
    app.db.put("linkedin_rest_until", iso(app.clock.now() + timedelta(seconds=random.uniform(*LINKEDIN_REST))))


def _held_back(app: App, o: sqlite3.Row) -> str:
    """Why an approved item can't go out right now (it stays queued for the scheduled run), or ""."""
    if _paused(app):
        return "Paused: it goes out after /resume."
    if not app.clock.working():
        return "Outside working hours: it goes out when they start."
    if app.clock.holding(o["channel"]):
        what = {"email": "emails", "li_message": "LinkedIn messages", "li_connect": "connection requests",
                "apply_note": "applications"}.get(o["channel"], o["channel"])
        if app.clock.held_day():
            return f"No {what} go out on {app.clock.local():%A}s: it's queued and goes out the next day."
        return (f"{what.capitalize()} go out between {app.clock.send_start:%H:%M} and {app.clock.send_end:%H:%M}: "
                "it's queued until then.")
    if o["channel"].startswith("li_") and not app.settings.linkedin.enabled:
        return "LinkedIn is switched off: it stays queued until it's back on."
    if o["channel"].startswith("li_") and app.db.get(LINKEDIN_LOCK):
        return "LinkedIn asked for a security check: it stays queued until you run `jobpilot login linkedin`."
    if o["channel"].startswith("li_") and linkedin_resting(app):
        return "LinkedIn is resting between sends: it goes out in the next slot."
    if not (_room(app, o["channel"]) and _apply_room(app, o)):
        return "Today's limit is reached: it goes out tomorrow."
    if o["channel"] == "email" and app.mailbox is None:
        return "Gmail isn't set up, so it can't be sent."
    return ""


def queue_outlook(app: App) -> list[tuple[str, int, str]]:
    """What the approved queue will do from here: (channel, how many, why it waits or "" if it goes now)."""
    groups: Counter[tuple[str, str]] = Counter()
    for o in app.db.outreach_in("approved", limit=1000):
        groups[(o["channel"], _held_back(app, o))] += 1
    return [(channel, n, why) for (channel, why), n in groups.items()]


async def send_now(app: App, outreach_id: int) -> None:
    """Send, apply or connect the moment you approve, instead of waiting for the next scheduled run.
    One at a time, in the order you tapped; anything held back stays approved for the scheduled queue."""
    lock = getattr(app, "send_now_lock", None)
    if lock is None:
        lock = app.send_now_lock = asyncio.Lock()
    async with lock:
        o = app.db.outreach(outreach_id)
        if o is None or o["status"] != "approved":
            return
        held = _held_back(app, o)
        if held:
            await app.bot.mark(o["id"], f"✅ Approved. {held}")
            return
        if o["channel"] == "email":
            await _send_email(app, o)
            return
        app.db.set_outreach(o["id"], status="sending")  # claimed: the scheduled queue now skips it
        try:
            async with app.browser.page() as page:
                await _browser_action(app, page, o)
        except LoggedOut as exc:
            app.db.set_outreach(o["id"], status="approved")
            if "linkedin.com" not in str(exc):
                await app.bot.notify(f"🔒 {esc(str(exc))}")
            elif lock_linkedin(app, str(exc)):
                await app.bot.notify(linkedin_stopped(str(exc)))
        except Exception as exc:  # e.g. Chrome wouldn't start: leave it for the scheduled queue
            log.exception("sending #%s right away failed", o["id"])
            if app.db.outreach(o["id"])["status"] == "sending":
                app.db.set_outreach(o["id"], status="approved")
            await app.bot.mark(o["id"], f"✅ Approved. Couldn't send right away ({esc(type(exc).__name__)}); "
                                        "it goes with the next send run.")
        if o["channel"].startswith("li_") and app.db.outreach(o["id"])["status"] != "approved":
            rest_linkedin(app)  # what you tap next for LinkedIn waits for its slot instead of going in a burst
        await pause(*INSTANT_GAP)  # a breather before the next one you tapped, so LinkedIn doesn't see a burst


def same_company(greeted: str, company: str) -> bool:
    """"Vested" and "Vested Finance", "cardboard.ai" and "Cardboard" are the same; "Finn" and "Huvo AI" aren't."""
    words = lambda name: {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w and w not in GENERIC_WORDS}  # noqa: E731
    a, b = words(greeted), words(company)
    squash = lambda name: re.sub(r"[^a-z0-9]", "", name.lower())  # noqa: E731
    return bool(a & b) or not a or squash(greeted) in squash(company) or squash(company) in squash(greeted)


def _refusal(app: App, o: sqlite3.Row) -> str | None:
    """Why this must not go out at all, checked before anything is opened or typed."""
    if o["channel"] == "apply_note" and o["job_id"]:
        job = app.db.job(o["job_id"])
        found = TEAM_GREETING.match(o["body"] or "")
        if job and job["company"] and found and not same_company(found.group(1), job["company"]):
            return f"Not sent: the note greets the {found.group(1)} team, but the job is at {job['company']}"
    person = app.db.person(o["person_id"]) if o["person_id"] else None
    if person is None:
        return None
    if other := wrong_greeting(o["body"], person["name"]):
        return f"Not sent: the text greets {other!r}, but it's going to {person['name']}"
    if o["channel"] == "li_message" and app.db.scalar(
            "SELECT COUNT(*) FROM outreach WHERE channel='li_message' AND person_id=? AND status='sent' "
            "AND sent_at>=? AND id!=?", person["id"], app.clock.ago(days=SAME_PERSON_DAYS), o["id"]):
        return f"Not sent: {person['name']} already got a LinkedIn message in the last {SAME_PERSON_DAYS} days"
    return None


async def _tally_linkedin(app: App, outreach_id: int) -> None:
    """Count LinkedIn sends that fail in a row; past MAX_LINKEDIN_FAILS, stop LinkedIn and tell you."""
    o = app.db.outreach(outreach_id)
    if o["status"] in ("sent", "skipped", "dry_run"):
        app.db.put(LINKEDIN_FAILS, "0")
    elif o["status"] == "failed":
        fails = int(app.db.get(LINKEDIN_FAILS, "0") or 0) + 1
        app.db.put(LINKEDIN_FAILS, str(fails))
        why = f"{fails} LinkedIn sends failed in a row (the last: {o['error']})"
        if fails >= MAX_LINKEDIN_FAILS and lock_linkedin(app, why):
            await app.bot.notify(linkedin_stopped(why))


async def _browser_action(app: App, page: Page, o: sqlite3.Row) -> None:
    if refused := _refusal(app, o):
        app.db.set_outreach(o["id"], status="failed", error=refused)
        await app.bot.failed(o["id"], refused)
        return
    app.db.set_outreach(o["id"], status="sending")
    try:
        if o["channel"] == "apply_note":
            await _send_application(app, page, o)
        elif o["channel"] == "li_connect":
            await _send_connect(app, page, o)
        else:
            await _send_message(app, page, o)
    except LoggedOut:
        app.db.set_outreach(o["id"], status="approved")
        raise
    except Exception as exc:
        log.exception("outreach #%s failed", o["id"])
        # if it went out, only the Telegram update after it failed, and a retry would send it twice
        if app.db.outreach(o["id"])["status"] != "sent":
            shot = await app.browser.screenshot(page, f"outreach-{o['id']}")
            reason = f"{type(exc).__name__}: {exc}"[:250]
            app.db.set_outreach(o["id"], status="failed", error=reason)
            await app.bot.failed(o["id"], f"{reason} (screenshot: {shot})")
    if o["channel"].startswith("li_"):
        await _tally_linkedin(app, o["id"])


async def _send_application(app: App, page: Page, o: sqlite3.Row) -> None:
    job = app.db.job(o["job_id"])
    result = await _apply(app, page, app.sites[job["site"]], job, note=o["body"])
    stamp = app.clock.stamp()
    if result.outcome in (Outcome.APPLIED, Outcome.ALREADY):
        app.db.set_outreach(o["id"], status="sent", sent_at=stamp, error=None)
        await app.bot.mark(o["id"], "📤 Applied." if result.outcome is Outcome.APPLIED else "Already applied earlier.")
    elif result.outcome is Outcome.DRY_RUN:
        app.db.set_outreach(o["id"], status="dry_run", sent_at=stamp)
        await app.bot.mark(o["id"], f"🧪 Dry run: {result.detail}.")
    elif result.outcome is Outcome.NEEDS_INPUT:
        app.db.set_outreach(o["id"], status="waiting", error=f"waiting for your answer to: {result.question}")
    else:
        app.db.set_outreach(o["id"], status="failed", error=result.detail)
        await app.bot.failed(o["id"], result.detail or result.outcome.value)


async def _send_connect(app: App, page: Page, o: sqlite3.Row) -> None:
    person = app.db.person(o["person_id"])
    linkedin = _linkedin(app)
    note = as_text(o["body"]).strip()  # LinkedIn shows HTML as raw tags, so a draft that carries some is flattened
    reach, detail = await linkedin.connect(page, o["to_addr"], person["name"], note, app.settings.dry_run)
    without_note = reach is Reach.NOTE_QUOTA and bool(note)
    if without_note:  # free plan: notes run out, but a plain request still goes through
        await pause(2.0, 4.0)
        reach, detail = await linkedin.connect(page, o["to_addr"], person["name"], "", app.settings.dry_run)
        detail = detail or "sent without the note: LinkedIn's free plan won't take one right now"
    stamp = app.clock.stamp()
    if reach in (Reach.SENT, Reach.PENDING):
        app.db.set_outreach(o["id"], status="sent", sent_at=stamp, error=detail or None)
        app.db.set_person(person["id"], li_state="requested", li_requested_at=stamp)
        if reach is Reach.PENDING:
            await app.bot.mark(o["id"], "Already pending on LinkedIn.")
        elif without_note:
            await app.bot.mark(o["id"], "📤 Request sent <b>without the note</b>: LinkedIn's free plan "
                                        "won't take one right now.")
        else:
            await app.bot.mark(o["id"], "📤 Request sent.")
    elif reach is Reach.CONNECTED:
        app.db.set_outreach(o["id"], status="skipped", error="already connected")
        app.db.set_person(person["id"], li_state="connected")
        await app.bot.mark(o["id"], "🤝 You're already connected, so I'll draft a message instead.")
        await draft_message(app, app.db.person(person["id"]))
    elif reach is Reach.DRY_RUN:
        app.db.set_outreach(o["id"], status="dry_run", sent_at=stamp)
        await app.bot.mark(o["id"], f"🧪 Dry run: {detail}.")
    elif reach is Reach.WEEKLY_LIMIT:
        app.db.set_outreach(o["id"], status="approved")
        app.db.put("li_connect_blocked_until", app.clock.later(days=3))
        await app.bot.notify("⏸ LinkedIn says you've reached this week's invitation limit. Connection requests are "
                             "paused for 3 days; the approved ones stay queued.")
    else:
        app.db.set_outreach(o["id"], status="failed", error=f"{reach.value}: {detail}")
        await app.bot.failed(o["id"], detail or reach.value.replace("_", " "), without_note=reach is Reach.NOTE_QUOTA and not without_note)


async def _send_message(app: App, page: Page, o: sqlite3.Row) -> None:
    person = app.db.person(o["person_id"])
    resume = app.paths.resume_pdf if app.settings.linkedin.attach_resume and app.paths.resume_pdf.exists() else None
    reach, detail = await _linkedin(app).message(page, o["to_addr"], person["name"], as_text(o["body"]).strip(),
                                                 app.settings.dry_run,
                                                 attachment=resume)
    stamp = app.clock.stamp()
    if reach is Reach.SENT:
        app.db.set_outreach(o["id"], status="sent", sent_at=stamp, error=None)
        app.db.set_person(person["id"], li_state="messaged")
        await app.bot.mark(o["id"], "📤 Message sent." + (f" ({detail})" if detail else ""))
    elif reach is Reach.DRY_RUN:
        app.db.set_outreach(o["id"], status="dry_run", sent_at=stamp)
        await app.bot.mark(o["id"], f"🧪 Dry run: {detail}.")
    else:
        app.db.set_outreach(o["id"], status="failed", error=f"{reach.value}: {detail}")
        await app.bot.failed(o["id"], detail or reach.value.replace("_", " "))


# -- follow-through ------------------------------------------------------------------------

async def check_connections(app: App) -> str:
    """Look in on pending connection requests; when someone accepts, draft a message to them."""
    people = app.db.pending_connections(checked_before=app.clock.ago(hours=20), limit=12)
    if not people:
        return ""
    accepted = 0
    li = _linkedin(app)
    async with app.browser.session() as page:
        for person in people:
            if _paused(app):
                break
            member = await li.member(page, person["linkedin_url"])
            app.db.set_person(person["id"], li_checked_at=app.clock.stamp())
            if member is not None and member.connected:
                app.db.set_person(person["id"], li_state="connected")
                accepted += 1
                await draft_message(app, app.db.person(person["id"]))
            await pause(15, 40)
    return f"LinkedIn: {accepted} of {len(people)} pending requests were accepted."


async def check_inbox(app: App) -> str:
    """Spot replies (and bounces) to the emails you sent."""
    if app.mailbox is None:
        return ""
    sent = app.db.emails_awaiting_reply(app.clock.ago(days=45))
    if not sent:
        return ""
    since = (min(parse(o["sent_at"]) for o in sent) - timedelta(days=1)).date()
    report = await asyncio.to_thread(scan_inbox, app.mailbox, since,
                                     {o["message_id"] for o in sent if o["message_id"]},
                                     {o["to_addr"].lower() for o in sent})
    replies = bounces = 0
    stamp = app.clock.stamp()
    answered: set[int] = set()  # threads already handled, so one reply means one notification
    for o in sent:
        address = o["to_addr"].lower()
        person = app.db.person(o["person_id"])
        name = person["name"] if person else address
        root = o["parent_id"] or o["id"]
        if o["message_id"] in report.replied_ids or address in report.replied_from:
            app.db.set_outreach(o["id"], replied_at=stamp)
            if root in answered:
                continue
            answered.add(root)
            app.db.set_outreach(root, replied_at=stamp)
            for pending in app.db.thread(root):
                if pending["status"] in OPEN:
                    app.db.set_outreach(pending["id"], status="cancelled", error="they replied")
                    await app.bot.mark(pending["id"], "🛑 Cancelled: they replied.")
            replies += 1
            await app.bot.notify(f"🎉 {esc(name)} replied to “{esc(o['subject'])}”. It's in your Gmail.")
        elif address in report.bounced:
            app.db.set_outreach(o["id"], status="bounced")
            if person:
                app.db.set_person(person["id"], email_source="bounced")
            bounces += 1
            await app.bot.notify(f"📭 The email to {esc(address)} ({esc(name)}) bounced, so that address doesn't work.")
    return f"Inbox: {replies} replies, {bounces} bounces." if replies or bounces else ""


async def draft_followups(app: App) -> str:
    """One polite follow-up (by default) for emails that got no reply."""
    settings = app.settings.email
    if app.mailbox is None or not settings.enabled or settings.max_followups < 1:
        return ""
    cutoff = app.clock.ago(days=settings.followup_after_days)
    drafted = 0
    for original in app.db.first_emails_sent():
        thread = app.db.thread(original["id"])
        if any(t["status"] in OPEN for t in thread) or any(t["replied_at"] for t in thread):
            continue
        followups = [t for t in thread if t["status"] == "sent"]
        if len(followups) >= settings.max_followups:
            continue
        if max([original["sent_at"], *(t["sent_at"] for t in followups)]) > cutoff:
            continue
        try:
            await draft_followup(app, original)
        except LLMError as exc:
            log.warning("follow-up for #%s failed: %s", original["id"], exc)
            continue
        drafted += 1
    return f"Drafted {drafted} follow-ups." if drafted else ""


async def show_cards(app: App) -> str:
    await app.bot.register_commands()
    shown = await app.bot.pump()
    return f"showed {shown} cards" if shown else ""


async def expire_cards(app: App) -> str:
    days = app.settings.approvals.expire_after_days
    stale = app.db.stale_cards(app.clock.ago(days=days))
    for o in stale:
        app.db.set_outreach(o["id"], status="expired")
        if o["job_id"]:
            app.db.set_job(o["job_id"], status="expired")
        await app.bot.mark(o["id"], f"⌛ Expired: no decision in {days} days.")
    return f"expired {len(stale)} cards" if stale else ""


async def recover(app: App) -> None:
    """After a crash, anything caught mid-send is marked failed rather than silently retried."""
    for o in app.db.outreach_in("sending"):
        app.db.set_outreach(o["id"], status="failed", error="interrupted while sending")
        await app.bot.failed(o["id"], "interrupted while sending; check whether it went out before retrying")


async def send_update(app: App) -> str:
    """Every couple of hours: what happened since the last update, on Telegram."""
    since = app.db.get("update_since") or app.clock.ago(hours=2)
    now = app.clock.stamp()
    db = app.db
    found = db.scalar("SELECT COUNT(*) FROM jobs WHERE discovered_at>?", since)
    applied = db.rows("SELECT site, title, company FROM jobs WHERE status='applied' AND applied_at>? ORDER BY applied_at", since)
    sent = dict(db.rows("SELECT channel, COUNT(*) FROM outreach WHERE status='sent' AND sent_at>? GROUP BY channel", since))
    replies = db.scalar("SELECT COUNT(*) FROM outreach WHERE replied_at>?", since)
    queued = db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('matched','queued')")
    waiting = db.count_outreach("pending_approval") + db.count_jobs("needs_input")
    app.db.put("update_since", now)
    names = {"linkedin": "LinkedIn", "hirist": "Hirist", "wellfound": "Wellfound", "waas": "Work at a Startup",
             "instahyre": "Instahyre"}
    lines = [f"🕑 <b>Update since {parse(since).astimezone(app.clock.tz):%H:%M}</b>",
             f"🔎 New jobs found: {found}",
             f"✅ Applied: {len(applied)}"]
    lines += [f"   • {esc(j['title'] or 'Job')} · {esc(j['company'] or '?')} ({names.get(j['site'], j['site'])})"
              for j in applied[:10]]
    if len(applied) > 10:
        lines.append(f"   …and {len(applied) - 10} more")
    lines.append(f"📧 Emails sent: {sent.get('email', 0)} · 🤝 LinkedIn requests: {sent.get('li_connect', 0)}"
                 f" · 💬 LinkedIn messages: {sent.get('li_message', 0)}")
    lines.append(f"🎉 Replies: {replies}")
    lines.append(f"⏳ Good matches queued to apply: {queued} · Waiting on you: {waiting}")
    spent = float(app.db.get(f"cost:{app.clock.local().date()}", "0") or 0)
    lines.append(f"💰 Claude API cost today: ${spent:.2f}")
    await app.bot.notify("\n".join(lines))
    return "update sent"
