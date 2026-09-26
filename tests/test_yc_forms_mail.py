from __future__ import annotations

import json
import re

import httpx
import pytest

from jobpilot import email_finder, yc
from jobpilot.config import Profile
from jobpilot.db import DB
from jobpilot.email_finder import EmailFinder, domain_of, split_name
from jobpilot.forms import AnswerBook, Field, fit, from_profile, money, pick_option
from jobpilot.mail import InboxReport, Mailbox, build_message, read_headers

from .conftest import FIXTURES, FakeLLM, make_settings

# -- YC --------------------------------------------------------------------------------------


def test_batches():
    assert yc.batch_year("Winter 2012") == 2012
    assert yc.batch_year("S18") == 2018
    assert yc.batch_year("Unspecified") is None
    assert yc.short_batch("Winter 2024") == "W24"
    assert yc.short_batch("Spring 2025") == "X25"
    assert yc.short_batch("F24") == "F24"


def test_prefilter_keeps_recent_hiring_companies_in_your_regions():
    entries = json.loads((FIXTURES / "yc_hiring.json").read_text())
    kept = yc.prefilter(entries, make_settings().yc, exclude=["Ola"])
    assert [c["name"] for c in kept] == ["Acme AI", "Remote Rocket"]  # US-only, 2012, Ola and 5000-person dropped


def test_company_page_founders_and_jobs():
    props = yc.parse_page((FIXTURES / "yc_company.html").read_text())["props"]
    people = yc.founders(props)
    assert [p["name"] for p in people] == ["Dev Patel", "Mira Shah"]  # CTO first, inactive founder dropped
    assert people[1]["bio"].startswith("Previously")
    assert [j["id"] for j in yc.jobs(props)] == [501, 502, 503]
    summary = yc.job_summary(yc.jobs(props)[0])
    assert "Location: Bengaluru, KA, IN" in summary and "Skills: Python" in summary


def test_parse_page_without_data_fails_loudly():
    with pytest.raises(ValueError):
        yc.parse_page("<html></html>")


# -- forms -----------------------------------------------------------------------------------

def field(label: str, kind: str = "text", options: list[str] | None = None, required: bool = True) -> Field:
    return Field(idx=0, kind=kind, label=label, required=required, options=options or [], value="")


@pytest.mark.parametrize("answer,options,expected", [
    ("Yes", ["Yes", "No"], "Yes"),
    ("yes, I am", ["Yes", "No"], "Yes"),
    ("y", ["Yes, I am authorised", "No, I need a visa"], "Yes, I am authorised"),
    ("30", ["Immediate", "15 days", "30 days", "More than 30 days"], "30 days"),
    ("Immediately", ["Immediate", "30 days"], "Immediate"),
    ("4", ["0-2 years", "3-5 years", "5+ years"], "3-5 years"),
    ("Kotlin", ["Python", "Go"], None),
])
def test_pick_option(answer, options, expected):
    assert pick_option(answer, options) == expected


def test_fit_shapes_numbers_for_count_questions():
    assert fit("About 4.5 years", field("How many years of work experience do you have with Python?")) == "4"
    assert fit("24.5", field("Current CTC (LPA)", kind="number")) == "24.5"
    assert fit("I love your product", field("Why do you want to join us in the next few months?")) == \
        "I love your product"
    assert fit("none", field("How many years of experience with Go?")) is None


def test_money_converts_lakhs_only_when_asked_in_rupees():
    assert money("30 LPA", "current ctc (in inr)") == "3000000"
    assert money("30 LPA", "current ctc (in lakhs)") == "30 LPA"
    assert money("", "current ctc") is None


def test_profile_answers():
    profile = Profile(name="Asha Rao", email="asha@example.org", phone="+91 9000000000", city="Bengaluru",
                      years_experience=5, notice_period_days=0, current_ctc="30 LPA", expected_ctc="40 LPA")
    assert from_profile(profile, field("First name")) == "Asha"
    assert from_profile(profile, field("Mobile phone number")) == "+91 9000000000"
    assert from_profile(profile, field("Notice period", kind="select", options=["Immediate", "30 days"])) == "Immediately"
    assert from_profile(profile, field("How many years of work experience do you have?")) == "5"
    assert from_profile(profile, field("How many years of work experience do you have with Kafka?")) is None
    assert from_profile(profile, field("Expected CTC (INR per annum)")) == "4000000"


async def test_answer_book_order_and_learning(db: DB):
    llm = FakeLLM()
    profile = make_settings().profile
    book = AnswerBook(db, profile, llm, [(re.compile("relocat", re.I), "Yes")])
    db.learn_answer("Are you willing to relocate?", "No", "you")
    assert await book.answer(field("Are you willing to relocate?", "radio", ["Yes", "No"]), None) == "No"
    assert await book.answer(field("Open to relocation?", "radio", ["Yes", "No"]), None) == "Yes"
    assert await book.answer(field("Email address"), None) == "asha@example.org"
    assert llm.calls == []

    llm.answers["How many years of experience with Kafka?"] = ("3", True)
    assert await book.answer(field("How many years of experience with Kafka?"), "Backend at Acme") == "3"
    assert db.learned_answer("How many years of experience with Kafka?") == "3"  # short facts are remembered

    llm.answers["Why Acme?"] = ("Because your agents product matches what I built at my last job.", True)
    long_answer = await book.answer(field("Why Acme?", "textarea"), None)
    assert long_answer.startswith("Because")
    assert db.learned_answer("Why Acme?") is None  # long free text is job-specific, never reused

    llm.answers["Do you hold a security clearance?"] = ("No", False)
    assert await book.answer(field("Do you hold a security clearance?", "radio", ["Yes", "No"]), None) is None


# -- email -------------------------------------------------------------------------------------

BOX = Mailbox(address="asha@example.org", password="x", name="Asha Rao")


def test_build_message_threads_followups(tmp_path):
    first = build_message(BOX, "dev@acme.example", "Backend work", "Hi Dev")
    assert first["Message-ID"].endswith("@example.org>")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    followup = build_message(BOX, "dev@acme.example", "Re: Backend work", "Following up",
                             in_reply_to=first["Message-ID"], attachment=resume)
    assert followup["In-Reply-To"] == first["Message-ID"] == followup["References"]
    assert [part.get_filename() for part in followup.iter_attachments()] == ["resume.pdf"]


def headers(**values: str) -> bytes:
    return "".join(f"{k.replace('_', '-')}: {v}\r\n" for k, v in values.items()).encode() + b"\r\n"


def test_read_headers_classifies_replies_and_bounces():
    report = InboxReport()
    ours, watched = {"<abc@example.org>"}, {"dev@acme.example", "mira@acme.example"}
    read_headers(headers(From="Dev <dev@acme.example>", In_Reply_To="<abc@example.org>"), ours, watched, report)
    read_headers(headers(From="mira@acme.example"), ours, watched, report)
    unclear = read_headers(headers(From="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                                   References="<abc@example.org>",
                                   X_Failed_Recipients="ghost@acme.example, mira@acme.example"),
                           ours, watched, report)
    assert report.replied_ids == {"<abc@example.org>"}
    assert report.replied_from == {"dev@acme.example", "mira@acme.example"}
    assert report.bounced == {"mira@acme.example"}
    assert unclear is False
    assert read_headers(headers(From="postmaster@acme.example"), ours, watched, InboxReport()) is True


def test_domain_and_names():
    assert domain_of("https://www.acme-ai.example/careers") == "acme-ai.example"
    assert domain_of("acme.example") == "acme.example"
    assert domain_of("https://www.linkedin.com/company/acme") is None
    assert split_name("Anand Kumar Prajapati") == ("anand", "prajapati")
    assert split_name("Dr. José A. Núñez") == ("jose", "nunez")


async def test_email_finder_prefers_hunter_then_guesses(monkeypatch):
    async def mx(domain):
        return domain != "nomail.example"
    monkeypatch.setattr(email_finder, "has_mx", mx)

    def hunter(request: httpx.Request) -> httpx.Response:
        score = 92 if request.url.params["domain"] == "acme.example" else 20
        return httpx.Response(200, json={"data": {"email": f"x@{request.url.params['domain']}", "score": score}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(hunter))
    finder = EmailFinder(http, hunter_key="k")
    assert await finder.find("Dev Patel", "acme.example") == ("x@acme.example", "hunter:92")
    assert await finder.find("Dev Patel", "lowscore.example") == ("dev@lowscore.example", "guess")
    assert await finder.find("Dev Patel", "nomail.example") == (None, "domain takes no email")
    assert (await EmailFinder(http).find("", "acme.example"))[0] is None
