"""Site flows driven through real Chrome (headless) against local pages shaped like each site."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from jobpilot import browser as browser_module
from jobpilot import forms as forms_module
from jobpilot.browser import Browser, LoggedOut, top_dialog
from jobpilot.config import BrowserSettings
from jobpilot.db import DB
from jobpilot.forms import SCAN_JS, AnswerBook, FormFiller
from jobpilot.sites import generic as generic_module
from jobpilot.sites import linkedin as linkedin_module
from jobpilot.sites.base import Outcome
from jobpilot.sites.generic import DONE, SPECS, GenericSite
from jobpilot.sites.linkedin import LinkedIn, Reach, names_match

from .conftest import FakeLLM, make_settings

HAS_CHROME = Path("/Applications/Google Chrome.app").exists() or bool(shutil.which("google-chrome"))
pytestmark = [pytest.mark.browser, pytest.mark.skipif(not HAS_CHROME, reason="needs Google Chrome")]

JD = "<p>" + "We build payment APIs in Python and Go for millions of users. " * 12 + "</p>"
Pages = dict[str, str | tuple[int, dict[str, str]] | Callable[[str], str]]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def instant(*_args, **_kwargs):
        return None

    for module in (browser_module, forms_module, generic_module, linkedin_module):
        monkeypatch.setattr(module, "pause", instant)


@pytest.fixture
async def page(tmp_path):
    browser = Browser(BrowserSettings(channel="chrome", headless=True, slow_mo_ms=0),
                      tmp_path / "profile", tmp_path / "shots")
    async with browser.session() as opened:
        yield opened


async def serve(page, pages: Pages) -> None:
    async def handler(route):
        url = route.request.url
        found = pages.get(url.split("?")[0])
        if callable(found):
            found = found(url)
        if found is None:
            await route.fulfill(status=404, body="not here")
        elif isinstance(found, tuple):
            status, headers = found
            await route.fulfill(status=status, headers=headers)
        else:
            await route.fulfill(status=200, content_type="text/html", body=found)

    await page.route("**/*", handler)


@pytest.fixture
def filler(tmp_path) -> FormFiller:
    llm = FakeLLM()
    llm.answers["How many years of work experience do you have with Python?"] = ("4", True)
    rules = [(re.compile(r"authori[sz]ed to work in india", re.I), "Yes"),
             (re.compile(r"sponsorship", re.I), "No"),
             (re.compile(r"work mode", re.I), "Remote")]
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return FormFiller(AnswerBook(DB(":memory:"), make_settings().profile, llm, rules), resume)


# -- LinkedIn Easy Apply ----------------------------------------------------------------------

EASY_APPLY = f"""<html><body><main>
<h1>Backend Engineer</h1><a href="https://www.linkedin.com/company/acme/">Acme</a>
<div id="job-details">{JD}</div>
<button class="jobs-apply-button" aria-label="Easy Apply to Backend Engineer at Acme" onclick="openModal()">Easy Apply</button>
</main>
<div id="modal" role="dialog" class="jobs-easy-apply-modal" style="display:none">
  <button aria-label="Dismiss" onclick="askDiscard()">x</button>
  <div class="step" id="s1">
    <label for="phone">Mobile phone number</label><input id="phone" required value="+91 9000000000">
    <div class="fb-dash-form-element"><label for="py">How many years of work experience do you have with Python?</label>
      <input id="py" required><div id="e1" class="artdeco-inline-feedback--error" style="display:none">Enter a whole number</div></div>
    <label for="notice">Notice period</label>
    <select id="notice" required><option>Select an option</option><option>Immediate</option><option>30 days</option><option>60 days</option></select>
    <button aria-label="Continue to next step" onclick="next(1)">Next</button>
  </div>
  <div class="step" id="s2" style="display:none">
    <fieldset><legend>Are you legally authorized to work in India?</legend>
      <input type="radio" name="auth" id="ay" value="Yes" required><label for="ay">Yes</label>
      <input type="radio" name="auth" id="an" value="No"><label for="an">No</label></fieldset>
    <button aria-label="Review your application" onclick="next(2)">Review</button>
  </div>
  <div class="step" id="s3" style="display:none">
    <input type="checkbox" id="follow" checked><label for="follow">Follow Acme to stay up to date with their page.</label>
    <button aria-label="Submit application" onclick="submitApp()">Submit application</button>
  </div>
  <div id="done" style="display:none"><h2>Your application was sent to Acme!</h2></div>
</div>
<div id="discard" role="alertdialog" style="display:none"><button onclick="discard()">Discard</button><button>Save</button></div>
<script>
window.submitted = null; window.discarded = false;
const $ = id => document.getElementById(id);
function openModal() {{ $('modal').style.display = 'block'; }}
function show(id) {{ document.querySelectorAll('.step').forEach(s => s.style.display = 'none'); $(id).style.display = 'block'; }}
function next(step) {{
  if (step === 1) {{
    const ok = /^\\d+$/.test($('py').value) && $('notice').value !== 'Select an option';
    $('e1').style.display = ok ? 'none' : 'block';
    if (ok) show('s2');
  }} else if (document.querySelector('input[name=auth]:checked')) show('s3');
}}
function submitApp() {{
  window.submitted = {{py: $('py').value, notice: $('notice').value,
    auth: document.querySelector('input[name=auth]:checked').value, follow: $('follow').checked}};
  document.querySelectorAll('.step').forEach(s => s.style.display = 'none'); $('done').style.display = 'block';
}}
function askDiscard() {{ $('discard').style.display = 'block'; }}
function discard() {{ $('discard').style.display = 'none'; $('modal').style.display = 'none'; window.discarded = true; }}
</script></body></html>"""

JOB_URL = "https://www.linkedin.com/jobs/view/111/"


async def test_easy_apply_fills_every_step_and_submits(page, filler):
    await serve(page, {JOB_URL: EASY_APPLY})
    result = await LinkedIn(filler).apply(page, JOB_URL, None, dry_run=False)
    assert result.outcome is Outcome.APPLIED, result
    assert await page.evaluate("window.submitted") == {"py": "4", "notice": "30 days", "auth": "Yes", "follow": False}


async def test_easy_apply_dry_run_discards_before_submit(page, filler):
    await serve(page, {JOB_URL: EASY_APPLY})
    result = await LinkedIn(filler).apply(page, JOB_URL, None, dry_run=True)
    assert result.outcome is Outcome.DRY_RUN
    assert await page.evaluate("window.submitted") is None
    assert await page.evaluate("window.discarded") is True


async def test_easy_apply_asks_you_when_it_cannot_answer(page, filler):
    filler.book.llm.answers.clear()
    await serve(page, {JOB_URL: EASY_APPLY})
    result = await LinkedIn(filler).apply(page, JOB_URL, None, dry_run=False)
    assert result.outcome is Outcome.NEEDS_INPUT
    assert result.question == "How many years of work experience do you have with Python?"
    assert await page.evaluate("window.discarded") is True


async def test_linkedin_reads_jobs_and_spots_earlier_applications(page, filler):
    applied = EASY_APPLY.replace('<button class="jobs-apply-button"', '<span>Applied 3 days ago</span><button hidden')
    await serve(page, {JOB_URL: EASY_APPLY, "https://www.linkedin.com/jobs/view/222/": applied})
    site = LinkedIn(filler)
    info = await site.read(page, JOB_URL)
    assert (info.title, info.company, info.can_apply, info.applied) == ("Backend Engineer", "Acme", True, False)
    assert "payment APIs" in info.description
    assert (await site.apply(page, "https://www.linkedin.com/jobs/view/222/", None, False)).outcome is Outcome.ALREADY


async def test_linkedin_search_pages_until_empty(page, filler):
    def search(url: str) -> str:
        if "start=0" in url:
            items = "".join(f'<li data-occludable-job-id="{i}"><a href="/jobs/view/{i}/">Job {i}</a></li>' for i in (11, 22))
        else:
            items = ""
        return f"<html><body><main><ul>{items}</ul></main></body></html>"

    await serve(page, {"https://www.linkedin.com/jobs/search/": search})
    stubs = await LinkedIn(filler).discover(page, ["https://www.linkedin.com/jobs/search/?keywords=backend"], 10)
    assert [(s.external_id, s.url) for s in stubs] == [("11", "https://www.linkedin.com/jobs/view/11/"),
                                                       ("22", "https://www.linkedin.com/jobs/view/22/")]


async def test_linkedin_searches_share_the_budget(page, filler):
    def search(url: str) -> str:
        start = int(re.search(r"start=(\d+)", url).group(1))
        if "keywords=busy" in url:  # a search with endless results
            ids = range(1000 + start, 1025 + start)
        else:
            ids = (7, 8) if start == 0 else ()
        items = "".join(f'<li data-occludable-job-id="{i}"><a href="/jobs/view/{i}/">Job {i}</a></li>' for i in ids)
        return f"<html><body><main><ul>{items}</ul></main></body></html>"

    await serve(page, {"https://www.linkedin.com/jobs/search/": search})
    busy, quiet = ("https://www.linkedin.com/jobs/search/?keywords=busy",
                   "https://www.linkedin.com/jobs/search/?keywords=quiet")
    stubs = await LinkedIn(filler).discover(page, [busy, quiet], 10)
    ids = [s.external_id for s in stubs]
    assert "7" in ids and "8" in ids and len(ids) == 7  # the busy search can't crowd out the quiet one
    stubs = await LinkedIn(filler).discover(page, [quiet, busy], 10)
    assert len(stubs) == 10  # what the quiet search doesn't use goes to the busy one


# -- LinkedIn connections and messages ------------------------------------------------------------

def profile(name: str = "Mavi Kapoor", degree: str = "2nd", pending: bool = False, quota: bool = False,
            stale: str | None = None, stale_closable: bool = True, chat_name: str | None = None) -> str:
    """`stale`: someone else's chat LinkedIn left open on the page. `chat_name`: who the Message button opens."""
    button = (f'<button aria-label="Pending, click to withdraw invitation sent to {name}">Pending</button>' if pending
              else f'<button id="connect" aria-label="Invite {name} to connect" onclick="openInvite()">Connect</button>')
    closer = (f'<button aria-label="Close your conversation with {stale}" '
              f'onclick="this.parentElement.style.display=\'none\'">x</button>' if stale_closable else "")
    left_open = f"""<div id="stale"><h2>{stale}</h2><div class="msg-list"><p>Earlier chat</p></div>
  <div class="msg-form__contenteditable" contenteditable="true" role="textbox"></div>
  <button class="msg-form__send-button" onclick="window.staleSent = this.previousElementSibling.innerText">Send</button>
  {closer}</div>""" if stale else ""
    return f"""<html><body>{left_open}<main>
<section class="top-card"><h1>{name}</h1><div>CTO at Acme · {degree}</div>{button}
  <button aria-label="Message {name}" onclick="openChat()">Message</button><button aria-label="More actions">More</button></section>
<aside><section><h2>People also viewed</h2>
  <button aria-label="Invite Other Person to connect" onclick="window.decoy = true">Connect</button></section></aside>
</main>
<div id="invite" role="dialog" style="display:none">
  <button aria-label="Add a note" onclick="addNote()">Add a note</button>
  <button aria-label="Send without a note" onclick="send(false)">Send without a note</button>
  <div id="notebox" style="display:none"><textarea name="message"></textarea>
    <button aria-label="Send invitation" onclick="send(true)">Send</button></div>
</div>
<div id="upsell" role="dialog" style="display:none"><p>Personalize invites with Premium</p><button aria-label="Dismiss">x</button></div>
<div id="toast" role="alert" style="display:none">Invitation sent to {name}</div>
<div id="chat" style="display:none"><div class="msg-list"></div>
  <div class="msg-form__contenteditable" contenteditable="true" role="textbox"></div>
  <form class="msg-form"><input type="file" id="attach" style="display:none"></form>
  <button class="msg-form__send-button" onclick="sendMessage()">Send</button>
  <button aria-label="Close your conversation with {chat_name or name}" onclick="this.parentElement.style.display='none'">x</button></div>
<script>
const $ = id => document.getElementById(id);
function openInvite() {{ $('invite').style.display = 'block'; }}
function addNote() {{ if ({str(quota).lower()}) {{ $('invite').style.display = 'none'; $('upsell').style.display = 'block'; }}
                     else $('notebox').style.display = 'block'; }}
function send(withNote) {{ window.sentNote = withNote ? document.querySelector('textarea').value : '';
  $('invite').style.display = 'none'; $('toast').style.display = 'block'; }}
function openChat() {{ $('chat').style.display = 'block'; }}
function sendMessage() {{ const box = $('chat').querySelector('.msg-form__contenteditable');
  const line = document.createElement('p'); line.textContent = box.innerText;
  $('chat').querySelector('.msg-list').appendChild(line); window.sentMessage = box.innerText; box.innerText = ''; }}
</script></body></html>"""


PROFILE_URL = "https://www.linkedin.com/in/mavi-example/"
NOTE = "Hi Mavi, I build LLM support agents in Python and liked Acme's approach to ticket resolution."


async def test_connect_with_note_clicks_only_that_persons_button(page, filler):
    await serve(page, {PROFILE_URL: profile()})
    reach, _ = await LinkedIn(filler).connect(page, PROFILE_URL, "Mavi Kapoor", NOTE, dry_run=False)
    assert reach is Reach.SENT
    assert await page.evaluate("window.sentNote") == NOTE
    assert await page.evaluate("window.decoy") is None


async def test_connect_dry_run_clicks_nothing(page, filler):
    await serve(page, {PROFILE_URL: profile()})
    reach, _ = await LinkedIn(filler).connect(page, PROFILE_URL, "Mavi Kapoor", NOTE, dry_run=True)
    assert reach is Reach.DRY_RUN
    assert await top_dialog(page) is None


async def test_connect_refuses_the_wrong_person(page, filler):
    await serve(page, {PROFILE_URL: profile(name="Rahul Verma")})
    reach, detail = await LinkedIn(filler).connect(page, PROFILE_URL, "Mavi Kapoor", NOTE, dry_run=False)
    assert reach is Reach.NAME_MISMATCH and "Rahul Verma" in detail
    assert await top_dialog(page) is None


async def test_connect_reports_the_free_plan_note_limit(page, filler):
    await serve(page, {PROFILE_URL: profile(quota=True)})
    reach, _ = await LinkedIn(filler).connect(page, PROFILE_URL, "Mavi Kapoor", NOTE, dry_run=False)
    assert reach is Reach.NOTE_QUOTA
    assert await page.evaluate("window.sentNote") is None


async def test_connect_without_note_and_pending_profiles(page, filler):
    await serve(page, {PROFILE_URL: profile(), "https://www.linkedin.com/in/pending/": profile(pending=True)})
    site = LinkedIn(filler)
    assert (await site.connect(page, PROFILE_URL, "Mavi Kapoor", "", dry_run=False))[0] is Reach.SENT
    assert await page.evaluate("window.sentNote") == ""
    reach, _ = await site.connect(page, "https://www.linkedin.com/in/pending/", "Mavi Kapoor", NOTE, dry_run=False)
    assert reach is Reach.PENDING


async def test_message_after_they_accept(page, filler):
    await serve(page, {PROFILE_URL: profile(degree="1st"), "https://www.linkedin.com/in/stranger/": profile()})
    site = LinkedIn(filler)
    resume = filler.resume
    reach, detail = await site.message(page, PROFILE_URL, "Mavi Kapoor", "Thanks for connecting, Mavi.", dry_run=False,
                                       attachment=resume)
    assert reach is Reach.SENT and "attached resume.pdf" in detail
    assert await page.evaluate("window.sentMessage") == "Thanks for connecting, Mavi."
    assert await page.evaluate("document.getElementById('attach').files[0].name") == "resume.pdf"
    reach, _ = await site.message(page, "https://www.linkedin.com/in/stranger/", "Mavi Kapoor", "Hi", dry_run=False)
    assert reach is Reach.NOT_CONNECTED


@pytest.mark.parametrize("closable", [True, False])
async def test_message_never_goes_into_someone_elses_open_chat(page, filler, closable):
    # 2026-10-12: a chat with Arushi stayed open and took the messages meant for five other people
    await serve(page, {PROFILE_URL: profile(degree="1st", stale="Arushi Gandhi", stale_closable=closable)})
    reach, _ = await LinkedIn(filler).message(page, PROFILE_URL, "Mavi Kapoor", "Hi Mavi, thanks for connecting.",
                                              dry_run=False)
    assert reach is Reach.SENT
    assert await page.evaluate("window.sentMessage") == "Hi Mavi, thanks for connecting."
    assert await page.evaluate("window.staleSent") is None
    assert await page.evaluate("document.querySelector('#stale .msg-form__contenteditable').innerText") == ""


async def test_message_refuses_a_chat_that_opens_for_someone_else(page, filler):
    await serve(page, {PROFILE_URL: profile(degree="1st", chat_name="Arushi Gandhi")})
    reach, detail = await LinkedIn(filler).message(page, PROFILE_URL, "Mavi Kapoor", "Hi Mavi, thanks for connecting.",
                                                   dry_run=False)
    assert reach is Reach.FAILED and "Arushi Gandhi" in detail and "nothing was typed" in detail
    assert await page.evaluate("window.sentMessage") is None
    assert await page.evaluate("document.querySelector('#chat .msg-form__contenteditable').innerText") == ""


def shadow_profile(chips: tuple[str, ...] = ("Mavi Kapoor",), link: str = "mavi-example") -> str:
    """The 2026 layout: Message opens a "New message" draft inside the #interop-outlet shadow root, addressed by
    "Remove <name>" chips; once sent, LinkedIn redraws it as the conversation with them."""
    chip_html = "".join(f'<button aria-label="Remove {n}">x</button>' for n in chips)
    window = f"""<aside class="msg-overlay-container"><div class="msg-convo-wrapper msg-overlay-conversation-bubble">
  <h2>New message</h2><button aria-label="Close your draft conversation" class="close">x</button>
  {chip_html}<a href="/in/{link}/">{chips[0]}</a><div class="msg-list"></div>
  <form class="msg-form"><div class="msg-form__contenteditable" contenteditable="true" role="textbox"></div>
    <input type="file"></form>
  <button class="msg-form__send-button">Send</button></div></aside>"""
    page = profile(degree="1st").replace("onclick=\"openChat()\"", "onclick=\"openShadowChat()\"")
    return page.replace("</script>", f"""
function openShadowChat() {{
  const host = document.createElement('div'); host.id = 'interop-outlet'; document.body.appendChild(host);
  const root = host.attachShadow({{mode: 'open'}});
  root.innerHTML = `{window}`;
  root.querySelector('.close').onclick = () => host.remove();
  root.querySelector('.msg-form__send-button').onclick = () => {{
    const text = root.querySelector('[role=textbox]').innerText;
    window.sentMessage = text;
    root.innerHTML = `<div class="msg-convo-wrapper"><h2>Mavi Kapoor</h2><a href="/in/{link}/">Mavi Kapoor</a>
      <div class="msg-list"><p>${{text}}</p></div><div class="msg-form__contenteditable" contenteditable="true"
      role="textbox"></div><button aria-label="Close your conversation with Mavi Kapoor">x</button></div>`;
  }};
}}
</script>""")


async def test_message_through_the_2026_shadow_root_new_message_window(page, filler):
    await serve(page, {PROFILE_URL: shadow_profile()})
    reach, detail = await LinkedIn(filler).message(page, PROFILE_URL, "Mavi Kapoor", "Hi Mavi, thanks for connecting.",
                                                   dry_run=False)
    assert reach is Reach.SENT and "didn't confirm" not in detail, detail
    assert await page.evaluate("window.sentMessage") == "Hi Mavi, thanks for connecting."


@pytest.mark.parametrize("chips, link", [(("Mavi Kapoor", "Arushi Gandhi"), "mavi-example"),  # a group draft
                                         (("Mavi Kapoor",), "another-mavi-kapoor")])  # a namesake
async def test_message_refuses_a_new_message_window_not_addressed_to_them_alone(page, filler, chips, link):
    await serve(page, {PROFILE_URL: shadow_profile(chips, link)})
    reach, detail = await LinkedIn(filler).message(page, PROFILE_URL, "Mavi Kapoor", "Hi Mavi, thanks for connecting.",
                                                   dry_run=False)
    assert reach is Reach.FAILED and "nothing was typed" in detail
    assert await page.evaluate("window.sentMessage") is None


async def test_an_unlabelled_required_date_is_named_from_the_captions_around_it(page):
    await page.set_content("""<div role="dialog"><h2>Apply to Acme</h2><h3>Work experience</h3><p>Senior Advisor</p>
      <div><p>From</p><div><label for="m"><div></div></label>
        <select id="m" aria-describedby="e"><option value="">Month</option><option>January</option></select></div></div>
      <div id="e">This field is required</div></div>""")
    errors = await page.locator("[role=dialog]").evaluate(linkedin_module.ERRORS_JS)
    assert errors[0]["label"] == "Work experience · From (Month)"


async def test_a_linkedin_warning_page_stops_everything(page, filler):
    warning = "<html><body><main><h1>Your account has been temporarily restricted</h1></main></body></html>"
    await serve(page, {PROFILE_URL: warning})
    with pytest.raises(LoggedOut, match="linkedin.com showed a warning"):
        await LinkedIn(filler).connect(page, PROFILE_URL, "Mavi Kapoor", NOTE, dry_run=False)


async def test_logged_out_is_reported(page, filler):
    await serve(page, {PROFILE_URL: (302, {"location": "https://www.linkedin.com/authwall?trk=x"}),
                       "https://www.linkedin.com/authwall": "<html><body>Sign in</body></html>"})
    with pytest.raises(LoggedOut):
        await LinkedIn(filler).member(page, PROFILE_URL)


def test_profile_urls_use_www():
    from jobpilot.sites.linkedin import profile_url
    assert profile_url("https://in.linkedin.com/in/aayush-rai") == "https://www.linkedin.com/in/aayush-rai/"
    assert profile_url("https://www.linkedin.com/in/dev?trk=x") == "https://www.linkedin.com/in/dev/"


def test_names_match():
    assert names_match("Adit Jain", "Aditya Jain")
    assert names_match("Anand Kumar Prajapati", "Anand Prajapati")
    assert names_match("Mavi Kapoor", "Mavi Kapoor, PhD")
    assert not names_match("Mavi Kapoor", "Rahul Verma")
    assert not names_match("Mavi Kapoor", "Mavi Shah")


def test_title_from_tab_title():
    """LinkedIn job pages have no h1, so the title filters depend on reading the tab title."""
    from jobpilot.sites.linkedin import title_and_company
    assert title_and_company("Voice AI Engineer | Revspot | LinkedIn") == ("Voice AI Engineer", "Revspot")
    assert title_and_company("Revspot hiring Voice AI Engineer in Bengaluru, India | LinkedIn") == (
        "Voice AI Engineer", "Revspot")
    assert title_and_company("Backend Engineer | LinkedIn") == ("Backend Engineer", None)
    assert title_and_company("LinkedIn") == (None, None)
    assert title_and_company("") == (None, None)
    assert title_and_company(None) == (None, None)


# -- the shared flow for Instahyre, Hirist, Wellfound and Work at a Startup --------------------------

WELLFOUND_JOB = f"""<html><body><main><h1>Backend Engineer</h1><div class="description">{JD}</div>
<button onclick="document.getElementById('apply').style.display='block'">Apply</button></main>
<div id="apply" role="dialog" style="display:none">
  <label for="note">What interests you about working for this company?</label><textarea id="note" required></textarea>
  <label for="start">When can you start?</label><input id="start" required>
  <button onclick="sendApp()">Send application</button></div>
<div id="ok" role="status" style="display:none">Application sent</div>
<script>function sendApp() {{
  window.applied = {{note: document.getElementById('note').value, start: document.getElementById('start').value}};
  document.getElementById('apply').style.display = 'none'; document.getElementById('ok').style.display = 'block';
}}</script></body></html>"""

WELLFOUND_SEARCH = """<html><body><main>
<a href="/jobs/111-backend-engineer">Backend Engineer</a><a href="/jobs/222-data-engineer?utm=feed">Data Engineer</a>
<a href="/company/acme">Acme</a></main></body></html>"""


async def test_note_site_discovers_and_applies_with_the_approved_note(page, filler):
    job_url = "https://wellfound.com/jobs/111-backend-engineer"
    await serve(page, {"https://wellfound.com/jobs": WELLFOUND_SEARCH, job_url: WELLFOUND_JOB})
    site = GenericSite(SPECS["wellfound"], filler)
    stubs = await site.discover(page, ["https://wellfound.com/jobs"], 10)
    assert [(s.external_id, s.url) for s in stubs] == [("111", job_url),
                                                       ("222", "https://wellfound.com/jobs/222-data-engineer")]
    result = await site.apply(page, job_url, "I'd love to build payments with you.", dry_run=False)
    assert result.outcome is Outcome.APPLIED, result
    assert await page.evaluate("window.applied") == {"note": "I'd love to build payments with you.",
                                                     "start": "30 days"}


async def test_note_site_dry_run_stops_before_sending(page, filler):
    job_url = "https://wellfound.com/jobs/111-backend-engineer"
    await serve(page, {job_url: WELLFOUND_JOB})
    result = await GenericSite(SPECS["wellfound"], filler).apply(page, job_url, "note", dry_run=True)
    assert result.outcome is Outcome.DRY_RUN
    assert await page.evaluate("window.applied") is None


ONE_CLICK = f"""<html><body><main><h1>Backend Engineer</h1><h3>Applied AI Engineer</h3><div>{JD}</div>
<button id="apply" onclick="this.textContent = 'Applied'">Apply</button></main></body></html>"""


async def test_one_click_site(page, filler):
    job_url = "https://www.instahyre.com/job-12345-backend-engineer-at-acme/"
    search = f'<html><body><a id="employer-profile-opportunity" href="{job_url}">Acme - Backend Engineer</a></body></html>'
    await serve(page, {job_url: ONE_CLICK, "https://www.instahyre.com/candidate/opportunities/": search})
    site = GenericSite(SPECS["instahyre"], filler)
    [stub] = await site.discover(page, SPECS["instahyre"].default_searches, 5)
    assert stub.external_id == "12345"
    info = await site.read(page, job_url)
    assert info.can_apply and not info.applied  # "Applied AI Engineer" is a job title, not a status
    dry = await site.apply(page, job_url, None, dry_run=True)
    assert dry.outcome is Outcome.DRY_RUN and await page.inner_text("#apply") == "Apply"
    assert (await site.apply(page, job_url, None, dry_run=False)).outcome is Outcome.APPLIED


COVERED = f"""<html><body><main><h1>Backend Engineer</h1><div>{JD}</div>
<div style="position:relative"><button id="apply" onclick="this.textContent = 'Applied'">Apply now</button>
<a style="position:absolute;inset:0"></a></div></main></body></html>"""


async def test_apply_button_under_an_overlay_is_still_clicked(page, filler):
    job_url = "https://www.instahyre.com/job-12345-backend-engineer-at-acme/"
    await serve(page, {job_url: COVERED})
    result = await GenericSite(SPECS["instahyre"], filler).apply(page, job_url, None, dry_run=False)
    assert result.outcome is Outcome.APPLIED, result


INSTAHYRE_CARDS = """<html><body>
<button ng-click="openApplyModal(opp)" data-i="0">View job »</button>
<button ng-click="openApplyModal(opp)" data-i="1">View job »</button>
<script>
const opps = [
  {employer: {company_name: 'Viven'}, job: {title: 'Backend Engineer', locations: 'Bangalore', keywords: ['Python'],
   opportunity_url: '/job-395159-backend-engineer-at-viven-bangalore/'}},
  {employer: {company_name: 'Kulfi'}, job: {title: 'Software Engineer', locations: 'Bangalore', keywords: [],
   opportunity_url: '/job-445794-software-engineer-at-kulfi-bangalore/'}},
];
window.angular = {element: el => ({scope: () => ({opp: opps[el.dataset.i]})})};
</script></body></html>"""


async def test_instahyre_discovers_jobs_from_angular_cards(page, filler):
    await serve(page, {"https://www.instahyre.com/candidate/opportunities/": INSTAHYRE_CARDS})
    stubs = await GenericSite(SPECS["instahyre"], filler).discover(page, SPECS["instahyre"].default_searches, 5)
    assert [(s.external_id, s.url, s.title) for s in stubs] == [
        ("395159", "https://www.instahyre.com/job-395159-backend-engineer-at-viven-bangalore/", "Viven - Backend Engineer"),
        ("445794", "https://www.instahyre.com/job-445794-software-engineer-at-kulfi-bangalore/", "Kulfi - Software Engineer"),
    ]
    assert "Python" in stubs[0].hint


class FakeApi:
    """page.request for instahyre_search: two pages of results, recording every query."""

    def __init__(self):
        self.queries: list[dict[str, list[str]]] = []

    async def get(self, url):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(url).query)
        self.queries.append(query)
        offset = int(query["offset"][0])
        objects = [{"public_url": f"https://www.instahyre.com/job-{offset + i}-x/", "title": f"Job {offset + i}",
                    "employer": {"company_name": "Acme"}, "locations": "Bangalore", "keywords": ["Python"]}
                   for i in range(35)]
        body = {"objects": objects, "meta": {"next": "more" if offset == 0 else None}}

        class Response:
            ok = True
            status = 200

            async def json(self):
                return body
        return Response()


async def test_instahyre_search_url_runs_through_the_api(monkeypatch):
    from types import SimpleNamespace
    from jobpilot.sites.generic import instahyre_search

    async def no_pause(*args):
        return None
    monkeypatch.setattr(generic_module, "pause", no_pause)
    api = FakeApi()
    page = SimpleNamespace(request=api)
    url = ("https://www.instahyre.com/candidate/opportunities/?search=true&skills=Python,Generative AI"
           "&location=Bangalore,Work From Home&years=1")
    found = await instahyre_search(page, url, 50)
    assert len(found) == 70 and found[0]["url"] == "https://www.instahyre.com/job-0-x/"
    assert found[0]["text"] == "Acme - Job 0" and "Python" in found[0]["card"]
    assert api.queries[0]["skills"] == ["Python", "Generative AI"]
    assert api.queries[0]["jobLocations"] == ["Bangalore", "Work From Home"]
    assert api.queries[0]["years"] == ["1"] and [q["offset"] for q in api.queries] == [["0"], ["35"]]
    assert await instahyre_search(page, SPECS["instahyre"].default_searches[0], 50) is None


def test_unknown_selector_overrides_are_rejected(filler):
    with pytest.raises(ValueError, match="unknown keys"):
        GenericSite(SPECS["hirist"], filler, {"apply_button": "x"})


# -- reading arbitrary forms ------------------------------------------------------------------------

MIXED_FORM = """<html><body><form id="f">
<label for="a">First name *</label><input id="a">
<input aria-label="Current CTC (in LPA)" type="number" required>
<div><span id="mode">Preferred work mode</span>
  <select aria-labelledby="mode" required><option value="">Select</option><option>Remote</option><option>Hybrid</option></select></div>
<div class="q"><p>Do you need visa sponsorship?</p>
  <label><input type="radio" name="visa" value="yes" required>Yes</label><label><input type="radio" name="visa" value="no">No</label></div>
<label><input type="checkbox" name="terms" required> I agree to the terms</label>
<input type="file" id="cv" required style="display:none">
<input type="hidden" name="csrf" value="x">
</form></body></html>"""


async def test_form_scan_and_fill(page, filler):
    await serve(page, {"https://forms.example/apply": MIXED_FORM})
    await page.goto("https://forms.example/apply")
    form = page.locator("#f")
    fields = await form.evaluate(SCAN_JS)
    assert [(f["kind"], f["label"], f["required"]) for f in fields] == [
        ("text", "First name", True), ("number", "Current CTC (in LPA)", True),
        ("select", "Preferred work mode", True), ("radio", "Do you need visa sponsorship?", True),
        ("checkbox", "I agree to the terms", True), ("file", "", True)]
    assert fields[2]["options"] == ["Remote", "Hybrid"] and fields[3]["options"] == ["Yes", "No"]
    assert await filler.fill(form, "Backend at Acme") is None
    values = await page.evaluate("""() => ({first: document.querySelector('#a').value,
        ctc: document.querySelector('[aria-label="Current CTC (in LPA)"]').value,
        mode: document.querySelector('select').value, visa: document.querySelector('input[name=visa]:checked').value,
        terms: document.querySelector('input[name=terms]').checked, cv: document.querySelector('#cv').files.length})""")
    assert values == {"first": "Asha", "ctc": "30", "mode": "Remote", "visa": "no", "terms": True, "cv": 1}


SLOW_CONFIRM = f"""<html><body><main><h1>Backend Engineer</h1><div>{JD}</div>
<button id="apply" onclick="this.textContent = '…'; setTimeout(() => {{
  this.textContent = 'Application sent!';
  document.getElementById('toast').style.display = 'block'; }}, 4000)">Apply</button></main>
<div id="toast" style="display:none">Application sent to Acme!</div></body></html>"""


async def test_slow_confirmation_with_instahyre_wording_counts_as_applied(page, filler):
    job_url = "https://www.instahyre.com/job-12345-backend-engineer-at-acme/"
    await serve(page, {job_url: SLOW_CONFIRM})
    site = GenericSite(SPECS["instahyre"], filler)
    result = await site.apply(page, job_url, None, dry_run=False)
    assert result.outcome is Outcome.APPLIED, result
    assert (await site.read(page, job_url)).can_apply  # a fresh page load: not applied yet in this fixture
    assert re.search(DONE, "Application sent!", re.I) and re.search(DONE, "Application sent to Acme!", re.I)
    assert not re.search(DONE, "Applied AI Engineer", re.I) and not re.search(DONE, "Application process", re.I)


async def test_linkedin_scrolls_past_jobs_it_has_already_seen(page, filler):
    def search(url: str) -> str:
        start = int(re.search(r"start=(\d+)", url).group(1))
        ids = range(100 + start, 125 + start) if start < 75 else ()
        items = "".join(f'<li data-occludable-job-id="{i}"><a href="/jobs/view/{i}/">Job {i}</a></li>' for i in ids)
        return f"<html><body><main><ul>{items}</ul></main></body></html>"

    await serve(page, {"https://www.linkedin.com/jobs/search/": search})
    seen = {str(i) for i in range(100, 150)}  # the first two pages were read on earlier runs
    stubs = await LinkedIn(filler).discover(page, ["https://www.linkedin.com/jobs/search/?keywords=x"], 5, known=seen)
    assert [s.external_id for s in stubs] == ["150", "151", "152", "153", "154"]


NO_CONFIRM = f"""<html><body><main><h1>Product Engineer</h1><div>{JD}</div>
<button id="apply" onclick="this.textContent = '…'">Apply</button></main></body></html>"""

APPLIED_ON_RELOAD = f"""<html><body><main><h1>Product Engineer</h1><div>{JD}</div>
<span>Applied</span></main></body></html>"""


async def test_a_reload_settles_an_application_that_never_showed_a_confirmation(page, filler, monkeypatch):
    """Hirist often swallows the confirmation; the reloaded job page is what the site really thinks."""
    monkeypatch.setattr(generic_module, "CONFIRM_WAIT", 1)
    job_url = "https://www.hirist.tech/j/product-engineer-1676086"
    loads: list[int] = []

    def handler(_url: str) -> str:
        loads.append(1)
        return NO_CONFIRM if len(loads) <= 1 else APPLIED_ON_RELOAD  # apply()'s own read, then the reload

    await serve(page, {job_url: handler})
    result = await GenericSite(SPECS["hirist"], filler).apply(page, job_url, None, dry_run=False)
    assert result.outcome is Outcome.APPLIED, result
    assert len(loads) == 2  # it really did reload before deciding


async def test_a_reload_that_still_shows_unapplied_stays_failed(page, filler, monkeypatch):
    monkeypatch.setattr(generic_module, "CONFIRM_WAIT", 1)
    job_url = "https://www.hirist.tech/j/product-engineer-1676087"
    await serve(page, {job_url: NO_CONFIRM})
    result = await GenericSite(SPECS["hirist"], filler).apply(page, job_url, None, dry_run=False)
    assert result.outcome is Outcome.FAILED
    assert "still doesn't show it as applied" in (result.detail or "")
