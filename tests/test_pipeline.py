from __future__ import annotations

from datetime import timedelta

import pytest

from jobpilot import pipeline, yc
from jobpilot.db import meta
from jobpilot.llm import ScoutCompany, ScoutPerson
from jobpilot.mail import InboxReport, Mailbox
from jobpilot.sites.base import ApplyResult, JobPage, JobStub, Outcome
from jobpilot.sites.linkedin import Reach

from .conftest import FIXTURES, mock_http

JD = "We build payment APIs in Python and Go. " * 20


def page(title: str = "Backend Engineer", company: str | None = "Acme", applied: bool = False,
         can_apply: bool = True) -> JobPage:
    return JobPage(title=title, company=company, location=None, description=JD, can_apply=can_apply, applied=applied)


def people(app, company_name: str = "Acme AI", domain: str = "acme-ai.example"):
    company = app.db.upsert_company(domain, company_name, "yc", domain=domain, fit_reason="builds agents")
    person = app.db.upsert_person(company, "Dev Patel", "https://www.linkedin.com/in/dev/", title="CTO")
    return company, person


# -- job sites ------------------------------------------------------------------------------

async def test_one_click_site_scores_filters_and_applies(app):
    site = app.sites["instahyre"]
    site.stubs = [JobStub(str(i), f"https://instahyre.example/job-{i}") for i in (1, 2, 3, 4)]
    site.pages = {"https://instahyre.example/job-1": page(),
                  "https://instahyre.example/job-2": page(company="Ola Electric"),
                  "https://instahyre.example/job-3": page(applied=True),
                  "https://instahyre.example/job-4": page(title="Backend Intern")}
    summary = await pipeline.run_site(app, "instahyre")
    status = {i: app.db.job(f"instahyre:{i}")["status"] for i in (1, 2, 3, 4)}
    assert status == {1: "dry_run", 2: "filtered", 3: "applied_before", 4: "filtered"}
    assert site.applied == [("https://instahyre.example/job-1", None, True, "Backend Engineer at Acme")]
    assert app.db.job("instahyre:1")["applied_at"]
    assert "4 new" in summary
    assert [c[1]["company"] for c in app.llm.calls if c[0] == "score_job"] == ["Acme"]  # only job 1 was scored


async def test_low_scores_are_skipped(app):
    app.llm.job_score = 40
    site = app.sites["hirist"]
    site.stubs = [JobStub("9", "https://hirist.example/j/9")]
    site.pages = {"https://hirist.example/j/9": page()}
    await pipeline.run_site(app, "hirist")
    assert app.db.job("hirist:9")["status"] == "low_score"
    assert site.applied == []


async def test_daily_cap_limits_applications(app):
    app.settings.instahyre.apply_per_day = 1
    site = app.sites["instahyre"]
    site.stubs = [JobStub(str(i), f"https://instahyre.example/job-{i}") for i in (1, 2)]
    site.pages = {stub.url: page() for stub in site.stubs}
    await pipeline.run_site(app, "instahyre")
    assert len(site.applied) == 1
    await pipeline.run_site(app, "instahyre")
    assert len(site.applied) == 1  # same day, cap already used


async def test_form_questions_go_to_telegram(app):
    site = app.sites["linkedin"]
    site.stubs = [JobStub("7", "https://www.linkedin.com/jobs/view/7/")]
    site.pages = {"https://www.linkedin.com/jobs/view/7/": page()}
    site.results["https://www.linkedin.com/jobs/view/7/"] = ApplyResult(Outcome.NEEDS_INPUT, "whole number",
                                                                        question="Years of Go?")
    await pipeline.run_site(app, "linkedin")
    job = app.db.job("linkedin:7")
    assert (job["status"], job["question"]) == ("needs_input", "Years of Go?")
    assert ("ask", "linkedin:7", "Years of Go?") in app.bot.events


async def test_note_sites_draft_notes_for_approval(app):
    site = app.sites["wellfound"]
    site.stubs = [JobStub("5", "https://wellfound.com/jobs/5-backend")]
    site.pages = {"https://wellfound.com/jobs/5-backend": page()}
    await pipeline.run_site(app, "wellfound")
    assert app.db.job("wellfound:5")["status"] == "awaiting_approval"
    [note] = app.db.outreach_in("draft", ["apply_note"])
    assert note["body"] == "apply_note draft" and note["job_id"] == "wellfound:5"
    assert site.applied == []


async def test_yc_jobs_are_scored_without_opening_a_browser(app):
    app.sites["waas"].default_searches = []
    app.db.add_job("waas", "501", "https://www.workatastartup.com/jobs/501", title="Backend Engineer",
                   company="Acme AI", location="Bengaluru", description=JD)
    await pipeline.run_site(app, "waas")
    assert app.browser.sessions == 0
    assert app.db.job("waas:501")["status"] == "awaiting_approval"


async def test_approved_application_note_is_submitted(app):
    app.settings.mode = "live"
    app.db.add_job("waas", "501", "https://www.workatastartup.com/jobs/501", title="Backend", description=JD)
    oid = app.db.add_outreach("apply_note", "Dear team", job_id="waas:501", to_addr="u", status="approved")
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == "sent"
    assert app.db.job("waas:501")["status"] == "applied"
    assert app.sites["waas"].applied[0][1] == "Dear team"


async def test_application_waiting_on_a_question_resumes_after_the_answer(app):
    app.settings.mode = "live"
    url = "https://www.workatastartup.com/jobs/502"
    app.db.add_job("waas", "502", url, title="Backend", description=JD)
    oid = app.db.add_outreach("apply_note", "note", job_id="waas:502", to_addr=url, status="approved")
    app.sites["waas"].results[url] = ApplyResult(Outcome.NEEDS_INPUT, question="Earliest start date?")
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == "waiting"
    del app.sites["waas"].results[url]
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == "waiting"  # not answered yet
    app.db.set_job("waas:502", status="queued")  # what the bot does with your answer
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == "sent"


# -- YC -------------------------------------------------------------------------------------

async def test_yc_sync_prepare_and_drafts(app):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    app.http = mock_http({
        yc.HIRING_URL: (FIXTURES / "yc_hiring.json").read_text(),
        f"{yc.YC_URL}/companies/acme-ai": (FIXTURES / "yc_company.html").read_text(),
        f"{yc.YC_URL}/companies/acme-ai/jobs/aa1-backend-engineer": (FIXTURES / "yc_job.html").read_text(),
    })
    summary = await pipeline.sync_yc(app)
    assert "2 hiring companies" in summary
    acme = app.db.row("SELECT * FROM companies WHERE name='Acme AI'")
    assert (acme["status"], acme["yc_batch"], acme["domain"]) == ("target", "W24", "acme-ai.example")

    await pipeline.prepare_yc(app)
    acme = app.db.company(acme["id"])
    assert acme["status"] == "drafted"
    assert meta(acme)["roles"][0] == "Backend Engineer (Bengaluru, KA, IN)"
    job = app.db.job("waas:501")
    assert "Own our Python services" in job["description"] and "Interview process" in job["description"]
    assert meta(job)["questions"] == ["What is your notice period?"]
    assert app.db.job("waas:502") is None and app.db.job("waas:503") is None  # US-only, and sales
    connects = app.db.outreach_in("draft", ["li_connect"])
    assert [app.db.person(o["person_id"])["name"] for o in connects] == ["Dev Patel", "Mira Shah"]
    [email] = app.db.outreach_in("draft", ["email"])
    assert email["to_addr"] == "dev@acme-ai.example"
    assert email["body"].endswith("linkedin.com/in/asha-example")  # signature appended
    rocket = app.db.row("SELECT * FROM companies WHERE name='Remote Rocket'")
    assert rocket["status"] == "target"  # its YC page failed; retried later


async def test_scout_adds_startups_with_clean_linkedin_urls(app):
    app.db.upsert_company("known.example", "Known Co", "yc")
    app.llm.scouted = [
        ScoutCompany(name="Fresh AI", website="https://www.fresh.example", linkedin_url="https://linkedin.com/company/fresh",
                     location="Bengaluru", stage="Series A, Jun 2026", why="agent platform",
                     people=[ScoutPerson(name="Ria Sen", title="CTO", linkedin_url="https://in.linkedin.com/in/ria-sen?trk=x"),
                             ScoutPerson(name="Guess Who", title="CEO", linkedin_url="linkedin.com/in/made-up")]),
        ScoutCompany(name="Known Co", website="https://known.example", linkedin_url=None, location="", stage="",
                     why="", people=[]),
        ScoutCompany(name="Ola Maps", website="https://olamaps.example", linkedin_url=None, location="", stage="",
                     why="", people=[]),
    ]
    assert await pipeline.run_scout(app) == "Scout: found 1 new startups."
    fresh = app.db.row("SELECT * FROM companies WHERE domain='fresh.example'")
    assert (fresh["status"], fresh["linkedin_url"]) == ("target", "https://linkedin.com/company/fresh/")
    urls = [p["linkedin_url"] for p in app.db.people_of(fresh["id"])]
    assert urls == ["https://www.linkedin.com/in/ria-sen/", None]  # country hosts become www


async def test_scouted_company_without_people_gets_a_linkedin_lookup(app):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    cid = app.db.upsert_company("acme.example", "Acme", "scout", status="target", domain="acme.example",
                                fit_reason="agents")
    await pipeline.prepare_scouted(app)
    [lee] = app.db.people_of(cid)
    assert (lee["name"], lee["linkedin_url"]) == ("Lee Lead", "https://www.linkedin.com/in/lead-example/")
    assert app.db.company(cid)["status"] == "drafted"
    assert {o["channel"] for o in app.db.outreach_in("draft")} == {"li_connect", "email"}


# -- sending -----------------------------------------------------------------------------------

async def test_email_dry_run_then_live_with_spacing(app, monkeypatch):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    _, person = people(app)
    first = app.db.add_outreach("email", "Hi", subject="Hello", person_id=person, to_addr="dev@acme.example",
                                status="approved")
    await pipeline.send_email_queue(app)
    assert app.db.outreach(first)["status"] == "dry_run"

    sent = []
    monkeypatch.setattr(pipeline, "send", lambda box, message: sent.append(message))
    app.settings.mode = "live"
    second = app.db.add_outreach("email", "Hi", subject="Hello", person_id=person, to_addr="dev@acme.example",
                                 status="approved")
    await pipeline.send_email_queue(app)
    assert app.db.outreach(second)["status"] == "approved"  # too soon after the first
    app.clock.moment += timedelta(minutes=10)
    await pipeline.send_email_queue(app)
    o = app.db.outreach(second)
    assert o["status"] == "sent" and o["message_id"] == sent[0]["Message-ID"]


async def test_followup_is_not_sent_after_a_reply(app, monkeypatch):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    app.settings.mode = "live"
    monkeypatch.setattr(pipeline, "send", lambda box, message: pytest.fail("must not send"))
    first = app.db.add_outreach("email", "Hi", subject="Hello", to_addr="dev@acme.example", status="sent",
                                replied_at=app.clock.stamp())
    followup = app.db.add_outreach("email", "Bump", subject="Re: Hello", parent_id=first,
                                   to_addr="dev@acme.example", status="approved")
    await pipeline.send_email_queue(app)
    assert app.db.outreach(followup)["status"] == "cancelled"


@pytest.mark.parametrize("reach,status,li_state", [
    (Reach.SENT, "sent", "requested"),
    (Reach.PENDING, "sent", "requested"),
    (Reach.NOTE_QUOTA, "failed", "none"),
    (Reach.WEEKLY_LIMIT, "approved", "none"),
    (Reach.CONNECTED, "skipped", "connected"),
    (Reach.NAME_MISMATCH, "failed", "none"),
])
async def test_connection_request_outcomes(app, reach, status, li_state):
    app.settings.mode = "live"
    company, person = people(app)
    oid = app.db.add_outreach("li_connect", "note", person_id=person, company_id=company,
                              to_addr="https://www.linkedin.com/in/dev/", status="approved")
    app.sites["linkedin"].reach, app.sites["linkedin"].detail = reach, "detail"
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == status
    assert app.db.person(person)["li_state"] == li_state
    if reach is Reach.NOTE_QUOTA:
        assert app.bot.events[-1] == ("failed", oid, "detail", True)
    if reach is Reach.WEEKLY_LIMIT:
        assert app.db.get("li_connect_blocked_until") > app.clock.stamp()
        assert not pipeline._room(app, "li_connect")
    if reach is Reach.CONNECTED:
        assert app.db.outreach_in("draft", ["li_message"])  # a message is drafted instead


async def test_connection_caps(app):
    app.settings.mode = "live"
    app.settings.linkedin.connect_per_day = 1
    company, _ = people(app)
    for n in range(3):
        pid = app.db.upsert_person(company, f"Person {n}", f"https://www.linkedin.com/in/p{n}/")
        app.db.add_outreach("li_connect", "", person_id=pid, company_id=company,
                            to_addr=f"https://www.linkedin.com/in/p{n}/", status="approved")
    await pipeline.send_browser_queue(app)
    assert len(app.sites["linkedin"].connects) == 1
    app.clock.moment += timedelta(days=1)
    await pipeline.send_browser_queue(app)
    assert len(app.sites["linkedin"].connects) == 2


async def test_accepted_connections_get_a_message_draft(app):
    _, person = people(app)
    app.db.set_person(person, li_state="requested", li_requested_at=app.clock.ago(days=2))
    app.sites["linkedin"].connected = True
    await pipeline.check_connections(app)
    assert app.db.person(person)["li_state"] == "connected"
    [draft] = app.db.outreach_in("draft", ["li_message"])
    assert draft["person_id"] == person


async def test_inbox_replies_and_bounces(app, monkeypatch):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    _, person = people(app)
    replied = app.db.add_outreach("email", "Hi", subject="Hello", person_id=person, to_addr="Dev@acme.example",
                                  status="sent", sent_at=app.clock.ago(days=2), message_id="<m1@example.org>")
    pending_followup = app.db.add_outreach("email", "Bump", subject="Re: Hello", parent_id=replied,
                                           to_addr="dev@acme.example", status="pending_approval")
    bounced = app.db.add_outreach("email", "Hi", subject="Hello", to_addr="ghost@acme.example", status="sent",
                                  sent_at=app.clock.ago(days=1), message_id="<m2@example.org>")
    sent_followup = app.db.add_outreach("email", "Bump", subject="Re: Hello", parent_id=replied,
                                        to_addr="dev@acme.example", status="sent", sent_at=app.clock.ago(days=1))
    monkeypatch.setattr(pipeline, "scan_inbox", lambda box, since, ids, addresses: InboxReport(
        replied_ids={"<m1@example.org>"}, replied_from={"dev@acme.example"}, bounced={"ghost@acme.example"}))
    await pipeline.check_inbox(app)
    assert app.db.outreach(sent_followup)["replied_at"]
    assert app.db.outreach(replied)["replied_at"]
    assert app.db.outreach(pending_followup)["status"] == "cancelled"
    assert app.db.outreach(bounced)["status"] == "bounced"
    notes = [e[1] for e in app.bot.events if e[0] == "notify"]
    assert sum("Dev Patel replied" in n for n in notes) == 1  # one reply, one notification
    assert any("bounced" in n for n in notes)


async def test_followups_after_a_quiet_week(app):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    company, person = people(app)
    first = app.db.add_outreach("email", "Hi\n\nAsha Rao\nlinkedin.com/in/asha-example", subject="Hello",
                                person_id=person, company_id=company, to_addr="dev@acme.example", status="sent",
                                sent_at=app.clock.ago(days=6))
    app.db.add_outreach("email", "Recent", subject="Hey", person_id=person, to_addr="x@acme.example",
                        status="sent", sent_at=app.clock.ago(days=1))
    assert await pipeline.draft_followups(app) == "Drafted 1 follow-ups."
    [draft] = app.db.outreach_in("draft", ["email"])
    assert (draft["parent_id"], draft["subject"]) == (first, "Re: Hello")
    earlier = [c for c in app.llm.calls if c[0] == "write"][-1][2]["previous"]
    assert "linkedin.com/in/asha-example" not in earlier  # the signature isn't fed back to the model
    assert await pipeline.draft_followups(app) == ""  # one is already open


async def test_rewrite_keeps_subject_and_signature(app):
    company, person = people(app)
    first = app.db.add_outreach("email", "Hi", subject="Hello", person_id=person, company_id=company,
                                to_addr="d@x", status="sent")
    followup = app.db.add_outreach("email", "Bump\n\nAsha Rao\nlinkedin.com/in/asha-example", subject="Re: Hello",
                                   parent_id=first, person_id=person, company_id=company, to_addr="d@x")
    draft = await pipeline.rewrite(app, app.db.outreach(followup), "shorter")
    assert draft.subject == "Re: Hello"
    assert draft.body == "followup draft\n\nAsha Rao\nlinkedin.com/in/asha-example"


async def test_recover_and_expire(app):
    stuck = app.db.add_outreach("email", "Hi", status="sending")
    old = app.db.add_outreach("li_connect", "", status="pending_approval")
    app.db.set_outreach(old, updated_at=app.clock.ago(days=8))
    await pipeline.recover(app)
    await pipeline.expire_cards(app)
    assert app.db.outreach(stuck)["status"] == "failed"
    assert app.db.outreach(old)["status"] == "expired"


async def test_approved_linkedin_message_is_sent(app):
    app.settings.mode = "live"
    company, person = people(app)
    oid = app.db.add_outreach("li_message", "Thanks for connecting!", person_id=person, company_id=company,
                              to_addr="https://www.linkedin.com/in/dev/", status="approved")
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(oid)["status"] == "sent"
    assert app.db.person(person)["li_state"] == "messaged"
    assert app.sites["linkedin"].messages == [("https://www.linkedin.com/in/dev/", "Dev Patel",
                                               "Thanks for connecting!", False)]
    assert app.sites["linkedin"].attachments == [None]  # off unless linkedin.attach_resume is on

    app.settings.linkedin.attach_resume = True
    app.paths.profile_dir.mkdir(parents=True, exist_ok=True)
    app.paths.resume_pdf.write_bytes(b"%PDF-1.4 resume")
    second = app.db.add_outreach("li_message", "Hello again", person_id=person, company_id=company,
                                 to_addr="https://www.linkedin.com/in/dev/", status="approved")
    await pipeline.send_browser_queue(app)
    assert app.db.outreach(second)["status"] == "sent"
    assert app.sites["linkedin"].attachments[-1] == app.paths.resume_pdf


async def test_requeue_after_going_live_redoes_the_dry_run(app):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    site = app.sites["instahyre"]
    site.stubs = [JobStub("1", "https://instahyre.example/job-1")]
    site.pages = {"https://instahyre.example/job-1": page()}
    await pipeline.run_site(app, "instahyre")
    _, person = people(app)
    email = app.db.add_outreach("email", "Hi", subject="Hello", person_id=person, to_addr="dev@acme.example",
                                status="approved")
    connect = app.db.add_outreach("li_connect", "note", person_id=person, to_addr="https://www.linkedin.com/in/dev/",
                                  status="approved")
    app.db.add_job("waas", "501", "https://www.workatastartup.com/jobs/501", title="Backend", description=JD)
    note = app.db.add_outreach("apply_note", "Dear team", job_id="waas:501", to_addr="u", status="approved")
    await pipeline.send_email_queue(app)
    await pipeline.send_browser_queue(app)
    assert {app.db.outreach(o)["status"] for o in (email, connect, note)} == {"dry_run"}
    assert app.db.job("instahyre:1")["status"] == app.db.job("waas:501")["status"] == "dry_run"

    assert app.db.requeue_dry_runs() == {"applications": 1, "notes": 1, "drafts": 3}
    for oid in (email, connect, note):
        o = app.db.outreach(oid)
        assert o["status"] == "draft" and o["sent_at"] is None and o["approved_at"] is None
    assert app.db.job("waas:501")["status"] == "awaiting_approval"
    assert app.db.job("instahyre:1")["applied_at"] is None

    app.settings.mode = "live"
    await pipeline.run_site(app, "instahyre")
    assert app.db.job("instahyre:1")["status"] == "applied"


async def test_cold_emails_use_your_template(app):
    app.mailbox = Mailbox("asha@example.org", "pw", "Asha Rao")
    app.settings.email.template_subject = "Application for {role} at {company}"
    app.settings.email.template_body = "Hi {first_name},\n\nI'd like to apply for the {role} role at {company}.\n\nBest,"
    company, person = people(app)
    app.db.set_company(company, meta={"roles": ["Head of Sales (Remote)", "Backend Engineer (Bengaluru)"]})
    oid = await pipeline.draft_email(app, app.db.person(person), app.db.company(company))
    o = app.db.outreach(oid)
    assert o["subject"] == "Application for Backend Engineer at Acme AI"
    assert o["body"].startswith("Hi Dev,\n\nI'd like to apply for the Backend Engineer role at Acme AI.")
    assert o["body"].endswith("Asha Rao\nlinkedin.com/in/asha-example")  # signature still added
    assert not [c for c in app.llm.calls if c[0] == "write"]  # no Claude drafting

    other = app.db.upsert_company("beta.example", "Beta Labs", "yc", domain="beta.example")
    lead = app.db.upsert_person(other, "Riya Shah", "https://www.linkedin.com/in/riya/", title="CEO")
    oid = await pipeline.draft_email(app, app.db.person(lead), app.db.company(other))
    assert app.db.outreach(oid)["subject"] == "Application for Software Engineer (Backend / AI) at Beta Labs"


async def test_regular_update_reports_what_happened(app):
    app.db.add_job("hirist", "1", "https://hirist.example/j/1", title="Backend Engineer", company="Acme",
                   status="applied", applied_at=app.clock.ago(minutes=5))
    await pipeline.send_update(app)
    [(kind, text)] = app.bot.events
    assert "New jobs found: 1" in text and "Applied: 1" in text and "Backend Engineer · Acme (Hirist)" in text
    app.clock.moment += timedelta(hours=2)
    await pipeline.send_update(app)
    assert "Applied: 0" in app.bot.events[-1][1]  # only what's new since the last update


async def test_connection_notes_and_application_notes_use_your_templates(app):
    app.settings.outreach.connect_template = "Hi {first_name}, exploring {role} roles at {company}!"
    app.settings.outreach.apply_note_template = "Hi {company} team, I'd love the {role} role."
    company, person = people(app)
    app.db.set_company(company, meta={"roles": ["AI Engineer (Remote)"]})
    oid = await pipeline.draft_connect(app, app.db.person(person), app.db.company(company))
    assert app.db.outreach(oid)["body"] == "Hi Dev, exploring AI Engineer roles at Acme AI!"
    app.db.add_job("waas", "9", "https://www.workatastartup.com/jobs/9", title="Backend Engineer", company="Acme AI",
                   meta={"company_id": company})
    note = await pipeline.draft_application_note(app, app.db.job("waas:9"))
    assert app.db.outreach(note)["body"] == "Hi Acme AI team, I'd love the Backend Engineer role."
    assert not [c for c in app.llm.calls if c[0] == "write"]


async def test_priority_regions_are_contacted_first(app):
    app.settings.yc.priority_regions = ["United States of America"]
    app.settings.yc.companies_per_day = 1
    app.http = mock_http({})  # company pages 404, so prepare_yc skips drafting but keeps the order visible
    india = app.db.upsert_company("in.example", "Bharat AI", "yc", status="target", fit=95, yc_slug="bharat",
                                  meta={"regions": ["India"]})
    us = app.db.upsert_company("us.example", "Frisco AI", "yc", status="target", fit=60, yc_slug="frisco",
                               meta={"regions": ["United States of America"]})
    seen = []
    async def page(http, slug):
        seen.append(slug)
        return {"company": {"founders": []}, "jobPostings": []}
    import jobpilot.yc as yc_module
    original, yc_module.company_page = yc_module.company_page, page
    try:
        await pipeline.prepare_yc(app)
    finally:
        yc_module.company_page = original
    assert seen == ["frisco"]  # the US company goes first despite a lower fit score
    assert app.db.company(us)["status"] == "drafted" and app.db.company(india)["status"] == "target"
