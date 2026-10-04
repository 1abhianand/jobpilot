# jobpilot

**A job-search autopilot that runs on your own laptop.** It finds jobs that fit your resume and applies to
them. It also finds the right people at startups and drafts every connection request and email. Nothing
reaches a person until you tap **Send** on Telegram.

- Scores every job and startup against *your* resume with Claude, and applies only above your threshold.
- Applies on LinkedIn Easy Apply, Instahyre, Hirist, Wellfound and YC's Work at a Startup, and fills
  application forms from your answers.
- Finds founders at hiring YC startups, and once a week uses Claude's web search to find recently funded
  startups that fit you.
- Every message to a person (emails, LinkedIn notes, application notes) is a Telegram card you approve,
  edit or rewrite.
- Runs in a real Chrome window, paced like a person, with daily caps well under each site's limits.
- Starts in **dry-run** mode, which goes through everything except the final click or send.

> **Use it responsibly.** LinkedIn's terms forbid automation, and accounts can be restricted. jobpilot
> keeps volumes low and asks before every message, but the risk is yours. Review your drafts, and don't
> use it to spam people.

---

## How it works

```
 job boards ──► discover ──► score with Claude ──► apply (one-click sites, within caps)
                                   │
                                   └──► draft a note ──► Telegram card ──► you approve ──► apply

 YC directory ─┐
 web search  ──┴► score startups ──► find founders ──► draft LinkedIn note / email ──► Telegram ──► send

 Gmail inbox ──► replies stop a thread · bounces are flagged · one follow-up after 5 quiet days
 LinkedIn    ──► when someone accepts your request, a follow-up message is drafted for approval
```

| Source | What jobpilot does | Your approval |
|---|---|---|
| **LinkedIn Easy Apply, Instahyre, Hirist** | Reads new jobs from your saved searches, scores them and applies to the best ones, filling in form questions | Automatic, within daily caps. Questions it can't answer come to you on Telegram |
| **Wellfound, Work at a Startup** | Same, but each application carries a note to the team | Every note |
| **YC startups** | Takes the hiring YC companies in your regions, scores them for fit, and gets the founders and their LinkedIn profiles from public YC pages | Every connection request and email |
| **Other startups** | Weekly: Claude web search finds recently funded startups and the people to contact there | Every connection request and email |
| **Follow-through** | Messages people who accept your request, spots replies and bounces in Gmail, and drafts one follow-up | Every message |

Each card on Telegram has **Send / Edit / Rewrite / Skip**. **Edit** replaces the text with your own,
and **Rewrite** takes an instruction like "shorter" or "mention my Kafka work". These commands work at
any time: `/status`, `/pending`, `/pause` and `/resume`. A summary arrives at the end of each working day.

## Requirements

- macOS or Linux, Python 3.11+, and Google Chrome.
- An [Anthropic API key](https://console.anthropic.com/settings/keys) (Claude).
- A Telegram bot. Message [@BotFather](https://t.me/BotFather) and send `/newbot`.
- *Optional:* a Gmail account with an [App Password](https://myaccount.google.com/apppasswords), for cold
  email. You also need 2-step verification turned on.
- *Optional:* a [Hunter.io](https://hunter.io) key for verified email addresses. Without it, addresses
  are guessed as `first@company.com` and marked as guesses on the card.

Run it on a **personal** machine. Company networks often inspect TLS and block Telegram, and your job
search shouldn't live on an employer's laptop anyway.

## Setup (about 20 minutes)

```bash
git clone https://github.com/<you>/jobpilot.git && cd jobpilot
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
jobpilot init        # creates config.yaml, .env and profile/answers.yaml from the templates
```

1. **Resume:** save it as `profile/resume.pdf`. If its text comes out garbled, add a plain-text
   `profile/resume.md`, which then takes precedence.
2. **`config.yaml`:**
   - your profile: notice period, CTC, target roles, locations, and a one-paragraph pitch;
   - your LinkedIn job-search URLs, with the Easy Apply filter (`f_AL=true`) on;
   - `filters.company_exclude`, with your current employer at least.
3. **`.env`:** your keys. See [Keeping secrets out of git](#keeping-secrets-out-of-git).
4. `jobpilot telegram-id`, then put the `TELEGRAM_CHAT_ID` it prints into `.env`.
5. `jobpilot login`: Chrome opens one tab per site; log in to each one. Only the session cookies are
   kept, in `data/browser-profile`. Your passwords are never stored.
6. `jobpilot check`: checks the resume, Claude, Telegram, Gmail, YC data and Chrome in one go.
7. `jobpilot doctor instahyre` (then `hirist`, `wellfound`): shows what jobpilot sees on each site.
   See [When a site changes](#when-a-site-changes).
8. `jobpilot run`, and leave it running. On a Mac, use `caffeinate -i jobpilot run`.

### Going live

Keep `mode: dry_run` for a few days and check the drafts on Telegram. When they look right:

1. Set `mode: live` in `config.yaml`.
2. Run `jobpilot requeue`. Dry-run results count as final, so without this step the jobs and drafts the
   dry run only rehearsed would never be acted on. `requeue` puts those jobs back in the queue and sends
   the drafts back to Telegram for real approval.
3. Restart `jobpilot run`.

## Commands

| Command | What it does |
|---|---|
| `jobpilot init` | Creates `config.yaml`, `.env` and `profile/answers.yaml` from the templates |
| `jobpilot check` | Checks config, resume, keys, Telegram, Gmail and Chrome |
| `jobpilot login [site…]` | Opens Chrome so you can log in to the job sites |
| `jobpilot telegram-id` | Finds your Telegram chat ID |
| `jobpilot doctor <site>` | Shows what jobpilot sees on a site, without applying, and saves snapshots |
| `jobpilot run` | Runs everything on schedule until Ctrl+C |
| `jobpilot once <task>` | Runs one task now, e.g. `site:linkedin`, `yc-sync`, `scout`, `inbox` |
| `jobpilot status` | Shows today's numbers |
| `jobpilot answers [--forget Q]` | Lists the form answers it has learned, or drops a wrong one |
| `jobpilot requeue` | After switching to live, redoes what the dry run only rehearsed |
| `jobpilot dashboard` | Publishes the optional web dashboard (see below) |

## Configuration highlights

Every option is documented in [`jobpilot/templates/config.example.yaml`](jobpilot/templates/config.example.yaml).

- **Caps (per day):** 15 Easy Apply applications, 15 connection requests (80 a week), 15 LinkedIn
  messages and 15 emails. Emails go out at least 4 minutes apart.
- **Working hours:** 09:30–20:00, Monday–Saturday, in your timezone. Only one Chrome window is ever open.
- **Form answers** are tried in this order:
  1. answers you've given on Telegram before;
  2. the rules in `profile/answers.yaml`;
  3. your profile;
  4. Claude, answering only from your resume;
  5. if none of those can answer, the question comes to you.
- **Models:** `llm.model` writes the drafts. Set `llm.scoring_model: claude-haiku-4-5` to make scoring
  several times cheaper.

### Rough cost

On Claude Opus at the default volumes, expect about $2–5 a day, mostly for scoring. With
`scoring_model: claude-haiku-4-5` it comes to about $1–2 a day. The weekly web search adds a few cents.

## Keeping secrets out of git

Everything personal stays on your machine. The repo's `.gitignore` already excludes it:

| Ignored path | What's in it |
|---|---|
| `.env`, `.env.*` | API keys, bot token, Gmail App Password |
| `config.yaml` | your name, contact details, salary and searches |
| `profile/` | your resume and answer rules |
| `data/` | the SQLite database, logs, screenshots and browser login cookies |
| `dashboard/.env*`, `dashboard/.vercel/`, `dashboard/data.json` | dashboard secrets and your application data |

Only the templates in `jobpilot/templates/` are committed: `config.example.yaml`, `env.example` and
`answers.example.yaml`. The `.env` file looks like this:

```bash
ANTHROPIC_API_KEY=        # console.anthropic.com
TELEGRAM_BOT_TOKEN=       # from @BotFather
TELEGRAM_CHAT_ID=         # from `jobpilot telegram-id`
GMAIL_ADDRESS=            # optional, for cold email
GMAIL_APP_PASSWORD=       # optional, a Google App Password (not your normal password)
HUNTER_API_KEY=           # optional
```

Before you push, `git status` should never list `.env`, `config.yaml`, `profile/` or `data/`.

## Optional: run it every working day (macOS)

`scripts/daily.sh` starts jobpilot, keeps the Mac awake with `caffeinate`, and stops it at 20:15.
To start it each morning, save the following as `~/Library/LaunchAgents/com.you.jobpilot.plist`,
replacing `/path/to/jobpilot`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.jobpilot</string>
  <key>ProgramArguments</key><array><string>/path/to/jobpilot/scripts/daily.sh</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
    <!-- repeat for Weekday 2-5 (and 6 for Saturday) -->
  </array>
  <key>StandardOutPath</key><string>/path/to/jobpilot/data/daily.log</string>
  <key>StandardErrorPath</key><string>/path/to/jobpilot/data/daily.log</string>
</dict></plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.you.jobpilot.plist
```

Keep the project outside `~/Downloads`, `~/Documents` and `~/Desktop`. macOS doesn't let launchd read
those folders.

**Nothing runs while the Mac is asleep.** Keep it plugged in with the lid open during working hours.
A scheduled wake (`sudo pmset repeat wakeorpoweron MTWRF 09:20:00`) helps, but it can't wake a Mac that
has hibernated on a flat battery.

## Optional: web dashboard

`dashboard/` is a small password-protected Vercel site. It lists every application, email and LinkedIn
request, and refreshes every 30 minutes while jobpilot runs. Setup is in
[`dashboard/README.md`](dashboard/README.md).

## When a site changes

LinkedIn is matched by button names and roles, which rarely change. Instahyre and Hirist only show their
job lists after login, so their default patterns are best guesses until you run `doctor`. If `doctor`
finds no job links or no Apply button, override the patterns under that site's `selectors` in
`config.yaml`. The keys are `job_href`, `job_id`, `apply`, `done`, `submit`, `note_box` and
`description`. Build the new patterns from the snapshots `doctor` saves in `data/doctor/<site>/`.

## Safety built in

- **Right person:** before connecting or messaging, the name on the LinkedIn profile must match the
  person jobpilot meant to reach.
- **Weekly invitation limit:** if LinkedIn reports it, connection requests pause for 3 days.
- **Logged out:** if a site logs you out or shows a security check, that site's run stops and Telegram
  tells you to run `jobpilot login`.
- **Crashes:** anything interrupted mid-send is marked failed and gets a **Retry** button. It is never
  resent silently.
- **Replies:** a reply stops the whole thread, including any queued follow-up.

## Project layout

| Path | What |
|---|---|
| `jobpilot/pipeline.py` | the tasks: discover, score, apply, draft, send, follow up |
| `jobpilot/scheduler.py` | runs each task on its interval during working hours |
| `jobpilot/approvals.py`, `telegram.py` | Telegram cards and commands |
| `jobpilot/llm.py`, `prompts.py` | Claude calls (structured outputs) and prompt text |
| `jobpilot/forms.py` | generic application-form reader and filler |
| `jobpilot/sites/` | one adapter per site; `generic.py` is the shared flow for the non-LinkedIn sites |
| `jobpilot/yc.py`, `email_finder.py`, `mail.py` | YC data, email lookup, Gmail SMTP/IMAP |
| `jobpilot/db.py` | SQLite state |
| `dashboard/`, `scripts/` | optional web dashboard and daily launcher |

## Tests

```bash
pytest                     # 115 tests; the site-flow tests drive headless Chrome against local pages
pytest -m "not browser"    # skips the Chrome tests
```
