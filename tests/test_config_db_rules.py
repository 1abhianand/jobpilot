from __future__ import annotations

from importlib import resources

import yaml

from jobpilot.config import Secrets, Settings
from jobpilot.db import DB
from jobpilot.rules import company_excluded, location_ok, reject_reason

from .conftest import make_settings


def test_template_config_is_valid():
    raw = yaml.safe_load((resources.files("jobpilot") / "templates" / "config.example.yaml").read_text())
    settings = Settings.model_validate(raw)
    assert settings.dry_run
    assert settings.site("linkedin").search_urls[0].startswith("https://www.linkedin.com/jobs/search/")
    assert settings.waas.apply_per_day == 8
    assert "Ola" in settings.filters.company_exclude


def test_template_answers_compile():
    rules = yaml.safe_load((resources.files("jobpilot") / "templates" / "answers.example.yaml").read_text())
    assert all({"match", "answer"} <= set(rule) for rule in rules)


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-test  ")
    secrets = Secrets.from_env()
    assert secrets.gmail_app_password == "abcdefghijklmnop"
    assert secrets.telegram_chat_id == -100123
    assert secrets.anthropic_api_key == "sk-test"


def test_add_job_is_idempotent(db: DB):
    assert db.add_job("linkedin", "1", "https://x/1", title="A") is True
    assert db.add_job("linkedin", "1", "https://x/1", title="B") is False
    assert db.job("linkedin:1")["title"] == "A"


def test_applied_counts_include_dry_runs(db: DB):
    for i, status in enumerate(["applied", "dry_run", "failed"]):
        db.add_job("hirist", str(i), f"u{i}")
        db.set_job(f"hirist:{i}", status=status, applied_at="2026-09-24T06:00:00+00:00")
    assert db.applied_since("hirist", "2026-09-24T00:00:00+00:00") == 2
    assert db.applied_since("hirist", "2026-09-25T00:00:00+00:00") == 0


def test_upsert_company_keeps_status_and_merges_meta(db: DB):
    cid = db.upsert_company("acme.example", "Acme", "yc", status="new", meta={"roles": ["Backend"]})
    db.set_company(cid, status="target")
    again = db.upsert_company("acme.example", "Acme Inc", "yc", status="new", meta={"stage": "Early"}, one_liner="")
    company = db.company(again)
    assert again == cid
    assert company["status"] == "target"
    assert company["name"] == "Acme Inc"
    assert yaml.safe_load(company["meta"]) == {"roles": ["Backend"], "stage": "Early"}


def test_people_dedupe_on_linkedin_url(db: DB):
    cid = db.upsert_company("acme.example", "Acme", "yc")
    first = db.upsert_person(cid, "Dev Patel", "https://www.linkedin.com/in/dev/", title="CTO")
    second = db.upsert_person(cid, "Dev P.", "https://www.linkedin.com/in/dev/", title="")
    assert first == second
    assert db.person(first)["title"] == "CTO"


def test_outreach_caps_and_duplicates(db: DB):
    cid = db.upsert_company("acme.example", "Acme", "yc")
    pid = db.upsert_person(cid, "Dev Patel", "https://www.linkedin.com/in/dev/")
    rejected = db.add_outreach("li_connect", "hi", person_id=pid)
    db.set_outreach(rejected, status="rejected")
    assert not db.has_outreach("li_connect", person_id=pid)
    sent = db.add_outreach("li_connect", "hi", person_id=pid)
    db.set_outreach(sent, status="sent", sent_at="2026-09-24T06:00:00+00:00")
    assert db.has_outreach("li_connect", person_id=pid)
    assert db.sent_since("li_connect", "2026-09-24T00:00:00+00:00") == 1
    assert db.last_sent("li_connect") == "2026-09-24T06:00:00+00:00"


def test_learned_answers_normalise_the_question(db: DB):
    db.learn_answer("How many years of experience with Python?", "4", "llm")
    assert db.learned_answer("how many YEARS of experience with python") == "4"
    db.learn_answer("How many years of experience with Python?", "5", "you")
    assert db.learned_answer("How many years of experience with Python?") == "5"


def test_prompts_by_reply_or_recent(db: DB):
    db.add_prompt(10, "edit", 3)
    assert db.take_prompt(10)["ref"] == "3"
    assert db.take_prompt(10) is None
    db.add_prompt(11, "answer", "linkedin:1")
    assert db.take_prompt(newer_than="2999-01-01T00:00:00+00:00") is None  # too old for a plain message
    assert db.take_prompt(newer_than="2000-01-01T00:00:00+00:00")["kind"] == "answer"


def test_company_exclusion_is_whole_word():
    assert company_excluded("Ola Electric", ["Ola"])
    assert company_excluded("OLA", ["Ola"])
    assert not company_excluded("Motorola", ["Ola"])
    assert not company_excluded("Olacabs", ["Ola"])
    assert not company_excluded(None, ["Ola"])


def test_reject_reason():
    filters = make_settings(filters={"title_include": ["engineer"], "title_exclude": [r"\bintern\b"],
                                     "company_exclude": ["Ola"]}).filters
    assert reject_reason(filters, "Backend Engineer", "Acme") is None
    assert "exclude list" in reject_reason(filters, "Backend Engineer", "Ola Cabs")
    assert "exclude pattern" in reject_reason(filters, "Engineering Intern", "Acme")
    assert "include" in reject_reason(filters, "Product Manager", "Acme")


def test_yc_job_locations():
    patterns = make_settings().yc.job_locations
    assert location_ok("Bengaluru, KA, IN", patterns)
    assert location_ok("IN / Remote (IN)", patterns)
    assert location_ok("Remote", patterns)
    assert location_ok("US / ES / IN / Remote (US; ES; IN)", patterns)
    assert not location_ok("San Francisco, CA, US / Remote (US)", patterns)
    assert not location_ok("Remote (US)", patterns)
    assert not location_ok("Berlin, Germany", patterns)
    assert not location_ok(None, patterns)
