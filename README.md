# jobpilot

Applies to jobs for you, finds the right people at startups, drafts every message, and sends nothing
to a person until you tap **Send** on Telegram.

## What it does

| | How | Your approval |
|---|---|---|
| **LinkedIn Easy Apply, Instahyre, Hirist** | Reads new jobs from your saved searches, scores each against your resume with Claude, applies to the ones above your threshold, and fills form questions from your answers | Automatic, within daily caps. Questions it can't answer come to Telegram |
| **Wellfound, Work at a Startup (YC)** | Same, but each application carries a note to the founders | Every note |
| **YC startups** | Takes the YC companies that are hiring in your regions, scores them for fit, gets founders and their LinkedIn URLs from the public YC pages, and records their open roles | Every connection request and email |
| **Other startups** | Once a week, Claude uses web search to find recently funded startups that fit you, along with founders or engineering leads (looked up on LinkedIn when needed) | Every connection request and email |
| **Follow-through** | Drafts a message when someone accepts your connection request. Watches Gmail for replies and bounces, drafts one follow-up after 5 quiet days, and stops a thread when they reply | Every message and follow-up |

Each draft arrives on Telegram as a card with **Send / Edit / Rewrite / Skip**. Edit takes your own text.
Rewrite takes an instruction ("shorter", "mention my Kafka work"). `/status`, `/pending`, `/pause` and
`/resume` work at any time, and a summary arrives at the end of each working day.

## Setup (about 20 minutes)

Run it on your **personal** machine. On a company network, TLS inspection can block Telegram
outright, and your job search shouldn't live on an employer's laptop anyway.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
jobpilot init              # creates config.yaml, .env, profile/answers.yaml
```

1. **Resume**: save it as `profile/resume.pdf`. If the PDF's text comes out garbled, also save a plain
   version as `profile/resume.md`, which then takes precedence.
2. **config.yaml**: fill in `profile` (notice period, CTC, target roles, locations), your LinkedIn job
   search URLs (with the Easy Apply filter on), and `filters.company_exclude` (your current employer is
   already in it).
3. **.env**:
   - `ANTHROPIC_API_KEY`: from console.anthropic.com.
   - `TELEGRAM_BOT_TOKEN`: message @BotFather, send `/newbot`, and copy the token. Then run
     `jobpilot telegram-id` to get `TELEGRAM_CHAT_ID`.
   - `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`: 2-step verification must be on. Get the App Password
     from Google Account > Security > App passwords.
   - `HUNTER_API_KEY` (optional): verified email addresses. Without it, emails are guessed as
     first@company.com and labelled as a guess on the card.
4. `jobpilot login`: a Chrome window opens with a tab per site. Log in to each one. Only the session
   cookies are kept (in `data/browser-profile`). Your passwords are never stored.
5. `jobpilot doctor instahyre` (then `hirist`, `wellfound`): shows what jobpilot sees on each site and
   saves snapshots. See [When a site changes](#when-a-site-changes).
6. `jobpilot check`: verifies the resume, Claude, Telegram, Gmail, YC data and Chrome in one go.
7. `jobpilot run`: leave it running. On a Mac, `caffeinate -i jobpilot run` keeps the machine awake.

It starts in **dry-run** mode, which does everything except the final click or send. Cards still
arrive, so you can see the drafts. When a few days of dry runs look right, set `mode: live` in
config.yaml, run `jobpilot requeue`, and restart. `requeue` puts the jobs the dry run only rehearsed back
in the queue, and sends its drafts back to Telegram so you can approve them for real.

## Useful commands

```bash
jobpilot once site:linkedin   # run one task now (also: site:instahyre, yc-sync, yc-prepare, scout, inbox, ...)
jobpilot status               # today's numbers
jobpilot answers              # form answers it has learned; --forget "question" drops a wrong one
```

## Limits and safety

- **Caps** (all in config.yaml): 15 Easy Apply applications, 15 connection requests (80 a week), 15
  LinkedIn messages and 15 emails a day. Emails go out at least 4 minutes apart.
- **When it runs**: only in your working hours (09:30–20:00 IST, Monday–Saturday by default). Only one
  Chrome window is open at a time, and actions are paced like a person's.
- **LinkedIn**: its terms forbid automation, and accounts can be restricted. The caps above are well
  under LinkedIn's own limits, but the risk isn't zero. If LinkedIn reports the weekly invitation
  limit, connection requests pause for 3 days on their own.
- **Free LinkedIn** allows only a few notes on connection requests per month. When LinkedIn refuses a
  note, the card offers to send the request without one. Set `linkedin.send_notes: false` to skip notes.
- **Right person**: before connecting or messaging, the name on the profile must match the person it
  meant to reach.
- **Logged out**: if a site logs you out or shows a security check, that site's run stops and Telegram
  tells you to run `jobpilot login`.
- **Crashes**: anything interrupted mid-send is marked failed with a Retry button. It is never resent
  silently.

## Cost

Claude API, rough estimate: $2–5 a day at the default volumes on `claude-opus-5`. Most of that is job
and company scoring. Setting `llm.scoring_model: claude-haiku-4-5` brings it to about $1–2 a day, and
drafts still use Opus. The weekly web search adds a few cents. Hunter's free plan is 25 lookups a month.

## When a site changes

LinkedIn is matched by button names and roles, which rarely change. Instahyre and Hirist only show
their job lists after login, so their default link and button patterns are best guesses until the
first `doctor` run. If `doctor` finds no job links, or no Apply button:

- Override the patterns in config.yaml under that site's `selectors` (keys `job_href`, `job_id`,
  `apply`, `done`, `submit`, `note_box`, `description`), or
- share the files `doctor` saved in `data/doctor/<site>/` and the patterns can be fixed from those.

## Where things are

| Path | What |
|---|---|
| `config.yaml`, `.env`, `profile/` | your settings, secrets, resume and answer rules (git-ignored) |
| `data/jobpilot.db` | every job, company, person, draft and learned answer (SQLite) |
| `data/jobpilot.log` | what happened and why |
| `data/screenshots/` | a screenshot of each failed application |
| `jobpilot/sites/` | one adapter per site; the shared flow for the non-LinkedIn sites is `generic.py` |

## Tests

```bash
pytest            # 104 tests; the site-flow tests drive headless Chrome against local copies of each site's page structure
pytest -m "not browser"   # skip the Chrome ones
```
