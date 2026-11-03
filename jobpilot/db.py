"""SQLite state: jobs, companies, people, outreach drafts, learned form answers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .clock import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,              -- "<site>:<the site's job id>"
    site          TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    score         INTEGER,
    reason        TEXT,
    question      TEXT,                          -- form question waiting for your answer
    error         TEXT,
    meta          TEXT NOT NULL DEFAULT '{}',
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    applied_at    TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(site, status);

CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,           -- domain, or "yc:<slug>" when there is none
    name         TEXT NOT NULL,
    source       TEXT NOT NULL,                  -- yc | scout
    status       TEXT NOT NULL DEFAULT 'new',    -- new | target | pass | drafted
    website      TEXT,
    domain       TEXT,
    linkedin_url TEXT,
    yc_slug      TEXT,
    yc_batch     TEXT,
    one_liner    TEXT,
    description  TEXT,
    location     TEXT,
    team_size    INTEGER,
    fit          INTEGER,
    fit_reason   TEXT,
    meta         TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    drafted_at   TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER REFERENCES companies(id),
    name            TEXT NOT NULL,
    title           TEXT,
    linkedin_url    TEXT UNIQUE,
    email           TEXT,
    email_source    TEXT,                        -- hunter:<score> | guess | ...
    li_state        TEXT NOT NULL DEFAULT 'none',  -- none | requested | connected | messaged | unavailable
    li_requested_at TEXT,
    li_checked_at   TEXT,
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach (
    id            INTEGER PRIMARY KEY,
    channel       TEXT NOT NULL,                 -- email | li_connect | li_message | apply_note
    status        TEXT NOT NULL DEFAULT 'draft',
    person_id     INTEGER REFERENCES people(id),
    company_id    INTEGER REFERENCES companies(id),
    job_id        TEXT REFERENCES jobs(id),
    parent_id     INTEGER REFERENCES outreach(id),
    to_addr       TEXT,                          -- email address, profile URL or job URL
    subject       TEXT,
    body          TEXT NOT NULL DEFAULT '',
    why           TEXT,                          -- one line for the approval card
    tg_message_id INTEGER,
    message_id    TEXT,                          -- Message-ID header of a sent email
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    approved_at   TEXT,
    sent_at       TEXT,
    replied_at    TEXT
);
CREATE INDEX IF NOT EXISTS outreach_by_status ON outreach(status, channel);

CREATE TABLE IF NOT EXISTS answers (
    key        TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    source     TEXT NOT NULL,                    -- you | llm
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (             -- Telegram messages waiting for a typed reply
    tg_message_id INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,                 -- edit | rewrite | answer
    ref           TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    ref    TEXT,
    detail TEXT
);
"""

SENT_LIKE = ("sent", "dry_run")  # both count toward caps so a dry run behaves like the real thing
APPLIED_LIKE = ("applied", "dry_run")


def question_key(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


def meta(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None or not row["meta"]:
        return {}
    return json.loads(row["meta"])


def _marks(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._columns: dict[str, set[str]] = {}

    # -- plumbing ---------------------------------------------------------------

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def row(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, *args: Any) -> Any:
        found = self.conn.execute(sql, args).fetchone()
        return found[0] if found else None

    def _cols(self, table: str) -> set[str]:
        if table not in self._columns:
            self._columns[table] = {r["name"] for r in self.rows(f"PRAGMA table_info({table})")}
        return self._columns[table]

    def _clean(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        unknown = set(fields) - self._cols(table)
        if unknown:
            raise KeyError(f"{table} has no column(s) {sorted(unknown)}")
        return {k: json.dumps(v) if k == "meta" and not isinstance(v, str) else v for k, v in fields.items()}

    def insert(self, table: str, **fields: Any) -> int:
        fields = self._clean(table, fields)
        sql = f"INSERT INTO {table} ({','.join(fields)}) VALUES ({_marks(fields)})"
        return self.conn.execute(sql, tuple(fields.values())).lastrowid

    def update(self, table: str, key: Any, **fields: Any) -> None:
        if not fields:
            return
        if "updated_at" in self._cols(table):
            fields.setdefault("updated_at", iso(utcnow()))
        fields = self._clean(table, fields)
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), key))

    # -- jobs -------------------------------------------------------------------

    def add_job(self, site: str, external_id: str, url: str, **fields: Any) -> bool:
        """Insert a job we have not seen before. Returns False when it already exists."""
        job_id = f"{site}:{external_id}"
        if self.row("SELECT 1 FROM jobs WHERE id=?", job_id):
            return False
        stamp = iso(utcnow())
        self.insert("jobs", id=job_id, site=site, external_id=external_id, url=url,
                    discovered_at=stamp, updated_at=stamp, **fields)
        return True

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM jobs WHERE id=?", job_id)

    def set_job(self, job_id: str, **fields: Any) -> None:
        self.update("jobs", job_id, **fields)

    def jobs(self, site: str, statuses: Iterable[str], limit: int, best_first: bool = False) -> list[sqlite3.Row]:
        statuses = tuple(statuses)
        order = "score DESC, discovered_at" if best_first else "discovered_at"
        return self.rows(
            f"SELECT * FROM jobs WHERE site=? AND status IN ({_marks(statuses)}) ORDER BY {order} LIMIT ?",
            site, *statuses, limit,
        )

    def applied_since(self, site: str, since: str) -> int:
        return self.scalar(
            f"SELECT COUNT(*) FROM jobs WHERE site=? AND status IN ({_marks(APPLIED_LIKE)}) AND applied_at>=?",
            site, *APPLIED_LIKE, since,
        )

    # -- companies --------------------------------------------------------------

    def upsert_company(self, key: str, name: str, source: str, **fields: Any) -> int:
        """Insert, or fill in fields on an existing company without touching its status."""
        existing = self.row("SELECT id, meta FROM companies WHERE key=?", key)
        if existing:
            fields.pop("status", None)
            if isinstance(fields.get("meta"), dict):
                fields["meta"] = {**meta(existing), **fields["meta"]}
            self.update("companies", existing["id"], name=name,
                        **{k: v for k, v in fields.items() if v not in (None, "")})
            return existing["id"]
        stamp = iso(utcnow())
        return self.insert("companies", key=key, name=name, source=source,
                           created_at=stamp, updated_at=stamp, **fields)

    def company(self, company_id: int | None) -> sqlite3.Row | None:
        return self.row("SELECT * FROM companies WHERE id=?", company_id) if company_id else None

    def set_company(self, company_id: int, **fields: Any) -> None:
        self.update("companies", company_id, **fields)

    def companies(self, status: str, source: str | None = None, limit: int = 50,
                  best_first: bool = False, newest_batch_first: bool = False) -> list[sqlite3.Row]:
        order = "fit DESC, id" if best_first else "id"
        if newest_batch_first:  # YC's own order is oldest batch first, which spends the daily budget on 2019
            order = "CAST(SUBSTR(yc_batch, 2, 2) AS INTEGER) DESC, id"
        if source:
            return self.rows(f"SELECT * FROM companies WHERE status=? AND source=? ORDER BY {order} LIMIT ?",
                             status, source, limit)
        return self.rows(f"SELECT * FROM companies WHERE status=? ORDER BY {order} LIMIT ?", status, limit)

    def drafted_since(self, source: str, since: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM companies WHERE source=? AND drafted_at>=?", source, since)

    def company_names(self) -> list[str]:
        return [r["name"] for r in self.rows("SELECT name FROM companies ORDER BY id DESC")]

    # -- people -----------------------------------------------------------------

    def upsert_person(self, company_id: int, name: str, linkedin_url: str | None = None, **fields: Any) -> int:
        if linkedin_url:
            existing = self.row("SELECT id FROM people WHERE linkedin_url=?", linkedin_url)
        else:
            existing = self.row("SELECT id FROM people WHERE company_id=? AND name=?", company_id, name)
        if existing:
            self.update("people", existing["id"], **{k: v for k, v in fields.items() if v not in (None, "")})
            return existing["id"]
        stamp = iso(utcnow())
        return self.insert("people", company_id=company_id, name=name, linkedin_url=linkedin_url,
                           created_at=stamp, updated_at=stamp, **fields)

    def person(self, person_id: int | None) -> sqlite3.Row | None:
        return self.row("SELECT * FROM people WHERE id=?", person_id) if person_id else None

    def set_person(self, person_id: int, **fields: Any) -> None:
        self.update("people", person_id, **fields)

    def people_of(self, company_id: int) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM people WHERE company_id=? ORDER BY id", company_id)

    def pending_connections(self, checked_before: str, limit: int) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM people WHERE li_state='requested' AND li_requested_at<=? "
            "AND (li_checked_at IS NULL OR li_checked_at<=?) ORDER BY li_checked_at, li_requested_at LIMIT ?",
            checked_before, checked_before, limit,
        )

    # -- outreach ---------------------------------------------------------------

    def add_outreach(self, channel: str, body: str, **fields: Any) -> int:
        stamp = iso(utcnow())
        return self.insert("outreach", channel=channel, body=body, created_at=stamp, updated_at=stamp, **fields)

    def outreach(self, outreach_id: int | str) -> sqlite3.Row | None:
        return self.row("SELECT * FROM outreach WHERE id=?", int(outreach_id))

    def set_outreach(self, outreach_id: int, **fields: Any) -> None:
        self.update("outreach", outreach_id, **fields)

    def outreach_in(self, status: str, channels: Iterable[str] | None = None,
                    limit: int = 100) -> list[sqlite3.Row]:
        order = "COALESCE(approved_at, created_at), id"
        if channels:
            channels = tuple(channels)
            return self.rows(
                f"SELECT * FROM outreach WHERE status=? AND channel IN ({_marks(channels)}) ORDER BY {order} LIMIT ?",
                status, *channels, limit,
            )
        return self.rows(f"SELECT * FROM outreach WHERE status=? ORDER BY {order} LIMIT ?", status, limit)

    def count_outreach(self, status: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM outreach WHERE status=?", status)

    def sent_since(self, channel: str, since: str) -> int:
        return self.scalar(
            f"SELECT COUNT(*) FROM outreach WHERE channel=? AND status IN ({_marks(SENT_LIKE)}) AND sent_at>=?",
            channel, *SENT_LIKE, since,
        )

    def last_sent(self, channel: str) -> str | None:
        return self.scalar(
            f"SELECT MAX(sent_at) FROM outreach WHERE channel=? AND status IN ({_marks(SENT_LIKE)})",
            channel, *SENT_LIKE,
        )

    def has_outreach(self, channel: str, *, person_id: int | None = None, job_id: str | None = None) -> bool:
        """Any live or finished draft for this person/job on this channel (rejected ones don't count)."""
        if job_id is not None:
            found = self.row("SELECT 1 FROM outreach WHERE channel=? AND job_id=? AND status!='rejected'",
                             channel, job_id)
        else:
            found = self.row("SELECT 1 FROM outreach WHERE channel=? AND person_id=? AND parent_id IS NULL "
                             "AND status!='rejected'", channel, person_id)
        return found is not None

    def release_waiting(self) -> None:
        """Approved applications that stopped on a form question go back in the queue once you've answered."""
        self.conn.execute("UPDATE outreach SET status='approved', error=NULL WHERE status='waiting' "
                          "AND job_id IN (SELECT id FROM jobs WHERE status='queued')")

    def requeue_dry_runs(self) -> dict[str, int]:
        """After switching to live: dry-run applications go back in the queue, and dry-run drafts
        come back to Telegram for a real decision (approving in a dry run wasn't consent to send)."""
        stamp = iso(utcnow())
        noted = "SELECT job_id FROM outreach WHERE channel='apply_note' AND job_id IS NOT NULL"
        self.conn.execute("BEGIN")
        try:
            jobs = self.conn.execute(
                f"UPDATE jobs SET status='matched', applied_at=NULL, updated_at=? "
                f"WHERE status='dry_run' AND id NOT IN ({noted})", (stamp,)).rowcount
            notes = self.conn.execute(
                f"UPDATE jobs SET status='awaiting_approval', applied_at=NULL, updated_at=? "
                f"WHERE status='dry_run' AND id IN ({noted} AND status='dry_run')", (stamp,)).rowcount
            drafts = self.conn.execute(
                "UPDATE outreach SET status='draft', approved_at=NULL, sent_at=NULL, tg_message_id=NULL, "
                "error=NULL, updated_at=? WHERE status='dry_run'", (stamp,)).rowcount
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"applications": jobs, "notes": notes, "drafts": drafts}

    def notes_drafted_since(self, site: str, since: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM outreach o JOIN jobs j ON o.job_id=j.id "
                           "WHERE o.channel='apply_note' AND j.site=? AND o.created_at>=?", site, since)

    def emails_awaiting_reply(self, since: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM outreach WHERE channel='email' AND status='sent' AND replied_at IS NULL "
                         "AND sent_at>=? ORDER BY sent_at", since)

    def thread(self, outreach_id: int) -> list[sqlite3.Row]:
        """Follow-ups of an email, oldest first."""
        return self.rows("SELECT * FROM outreach WHERE parent_id=? ORDER BY id", outreach_id)

    def first_emails_sent(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM outreach WHERE channel='email' AND parent_id IS NULL AND status='sent' "
                         "AND replied_at IS NULL ORDER BY sent_at")

    def stale_cards(self, before: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM outreach WHERE status='pending_approval' AND updated_at<=?", before)

    # -- summaries --------------------------------------------------------------

    def applied_list(self, since: str) -> list[sqlite3.Row]:
        return self.rows(
            f"SELECT site, title, company, status FROM jobs WHERE status IN ({_marks(APPLIED_LIKE)}) "
            "AND applied_at>=? ORDER BY site, applied_at", *APPLIED_LIKE, since)

    def applications(self, since: str, site: str | None = None) -> list[sqlite3.Row]:
        """Every application made since `since`, newest first, optionally for one site."""
        sql = (f"SELECT * FROM jobs WHERE status IN ({_marks(APPLIED_LIKE)}) AND applied_at>=?"
               + (" AND site=?" if site else "") + " ORDER BY applied_at DESC")
        return self.rows(sql, *APPLIED_LIKE, since, *((site,) if site else ()))

    def sent_list(self, since: str, channels: Iterable[str]) -> list[sqlite3.Row]:
        """Outreach that went out since `since`, newest first, with who it went to."""
        channels = tuple(channels)
        return self.rows(
            "SELECT o.*, p.name AS person_name, p.title AS person_title, c.name AS company_name "
            "FROM outreach o LEFT JOIN people p ON o.person_id=p.id LEFT JOIN companies c ON o.company_id=c.id "
            f"WHERE o.status IN ({_marks(SENT_LIKE)}) AND o.channel IN ({_marks(channels)}) AND o.sent_at>=? "
            "ORDER BY o.sent_at DESC", *SENT_LIKE, *channels, since)

    def queued_list(self) -> list[sqlite3.Row]:
        """Approved outreach that hasn't gone out yet, oldest approval first, with who and which job."""
        return self.rows(
            "SELECT o.*, p.name AS person_name, c.name AS company_name, "
            "j.title AS job_title, j.company AS job_company, j.url AS job_url "
            "FROM outreach o LEFT JOIN people p ON o.person_id=p.id LEFT JOIN companies c ON o.company_id=c.id "
            "LEFT JOIN jobs j ON o.job_id=j.id "
            "WHERE o.status IN ('approved', 'sending') ORDER BY COALESCE(o.approved_at, o.created_at), o.id")

    def applied_by_site(self, since: str) -> dict[str, int]:
        found = self.rows(
            f"SELECT site, COUNT(*) AS n FROM jobs WHERE status IN ({_marks(APPLIED_LIKE)}) AND applied_at>=? "
            "GROUP BY site", *APPLIED_LIKE, since)
        return {r["site"]: r["n"] for r in found}

    def sent_by_channel(self, since: str) -> dict[str, int]:
        found = self.rows(
            f"SELECT channel, COUNT(*) AS n FROM outreach WHERE status IN ({_marks(SENT_LIKE)}) AND sent_at>=? "
            "GROUP BY channel", *SENT_LIKE, since)
        return {r["channel"]: r["n"] for r in found}

    def count_jobs(self, status: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM jobs WHERE status=?", status)

    def replies_since(self, since: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM outreach WHERE replied_at>=?", since)

    # -- learned answers, Telegram prompts, key/value, events ---------------------

    def learned_answer(self, question: str) -> str | None:
        return self.scalar("SELECT answer FROM answers WHERE key=?", question_key(question))

    def learn_answer(self, question: str, answer: str, source: str) -> None:
        self.conn.execute(
            "INSERT INTO answers (key, question, answer, source, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET answer=excluded.answer, source=excluded.source",
            (question_key(question), question, answer, source, iso(utcnow())),
        )

    def add_prompt(self, tg_message_id: int, kind: str, ref: str | int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO prompts VALUES (?,?,?,?)",
                          (tg_message_id, kind, str(ref), iso(utcnow())))

    def take_prompt(self, tg_message_id: int | None = None, newer_than: str = "") -> sqlite3.Row | None:
        """The prompt a reply belongs to; without a reply target, the newest prompt created after `newer_than`."""
        if tg_message_id is not None:
            found = self.row("SELECT * FROM prompts WHERE tg_message_id=?", tg_message_id)
        else:
            found = self.row("SELECT * FROM prompts WHERE created_at>? "
                             "ORDER BY created_at DESC, tg_message_id DESC LIMIT 1", newer_than)
        if found:
            self.conn.execute("DELETE FROM prompts WHERE tg_message_id=?", (found["tg_message_id"],))
        return found

    def get(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM kv WHERE key=?", key)
        return default if value is None else value

    def put(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))

    def log(self, kind: str, ref: Any = None, detail: str | None = None) -> None:
        self.conn.execute("INSERT INTO events (at, kind, ref, detail) VALUES (?,?,?,?)",
                          (iso(utcnow()), kind, None if ref is None else str(ref), detail))
