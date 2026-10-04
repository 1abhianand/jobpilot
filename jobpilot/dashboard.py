"""The Vercel dashboard: a snapshot of every application and message, published from this laptop.

`dashboard/` is a tiny Vercel project. `export` writes `dashboard/data.json` (never committed);
`publish` redeploys the project so the site serves the fresh snapshot behind your password.
The deploy uses the Vercel CLI login on this machine (`vercel login`), so no Vercel token is
stored by jobpilot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .db import meta

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

JOB_FIELDS = ("id", "site", "title", "company", "location", "status", "score", "reason", "question", "error",
              "url", "discovered_at", "applied_at", "updated_at")


def folder(app: App) -> Path:
    return app.paths.home / "dashboard"


def snapshot(app: App) -> dict[str, Any]:
    """Everything the dashboard shows. Job descriptions and message bodies of drafts stay out."""
    db = app.db
    jobs = [{k: row[k] for k in JOB_FIELDS} for row in db.rows("SELECT * FROM jobs ORDER BY discovered_at DESC")]
    outreach = []
    for row in db.rows("""
            SELECT o.*, p.name AS person, p.title AS person_title, p.li_state, p.email_source,
                   c.name AS company_name, c.source AS company_source, c.yc_batch, j.title AS job_title
            FROM outreach o LEFT JOIN people p ON p.id=o.person_id LEFT JOIN companies c ON c.id=o.company_id
            LEFT JOIN jobs j ON j.id=o.job_id ORDER BY o.created_at DESC"""):
        sent = row["status"] in ("sent", "dry_run")
        outreach.append({
            "id": row["id"], "channel": row["channel"], "status": row["status"], "person": row["person"],
            "person_title": row["person_title"], "company": row["company_name"] or row["job_title"],
            "source": row["company_source"], "yc_batch": row["yc_batch"], "to": row["to_addr"],
            "email_source": row["email_source"], "li_state": row["li_state"], "subject": row["subject"],
            "body": row["body"] if sent else None, "followup": row["parent_id"] is not None,
            "created_at": row["created_at"], "approved_at": row["approved_at"], "sent_at": row["sent_at"],
            "replied_at": row["replied_at"], "error": row["error"],
        })
    companies = [{"name": r["name"], "source": r["source"], "status": r["status"], "fit": r["fit"],
                  "fit_reason": r["fit_reason"], "yc_batch": r["yc_batch"], "website": r["website"],
                  "location": r["location"], "stage": meta(r).get("stage")}
                 for r in db.rows("SELECT * FROM companies WHERE status!='new' ORDER BY fit DESC, id")]
    return {"generated_at": app.clock.stamp(), "mode": app.settings.mode, "timezone": app.settings.schedule.timezone,
            "jobs": jobs, "outreach": outreach, "companies": companies}


def export(app: App) -> Path:
    target = folder(app) / "data.json"
    target.write_text(json.dumps(snapshot(app), ensure_ascii=False))
    return target


def ready(app: App) -> bool:
    """Linked to a Vercel project (`vercel link` ran in dashboard/) and the CLI is installed."""
    return (folder(app) / ".vercel" / "project.json").exists() and shutil.which("vercel") is not None


async def publish(app: App) -> str:
    if not ready(app):
        return ""
    export(app)
    for attempt in (1, 2):  # the CLI renews an expired login on its first call; the retry then succeeds
        process = await asyncio.create_subprocess_exec(
            "vercel", "deploy", "--prod", "--yes", cwd=folder(app),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
        if process.returncode == 0:
            return "dashboard updated"
        log.warning("vercel deploy failed (attempt %s): %s", attempt, output.decode(errors="replace")[-200:])
        await asyncio.sleep(20)
    raise RuntimeError(f"vercel deploy failed: {output.decode(errors='replace')[-400:]}")
