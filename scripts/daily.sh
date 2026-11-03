#!/usr/bin/env bash
# Started by the systemd user timer at 09:25 every day: runs jobpilot for the working day, keeps the
# machine from sleeping, and stops at 20:15. See scripts/systemd/ for the timer.
# The macOS version of this script is scripts/daily-macos.sh.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

STOP_AT="20:15"

# A systemd user service starts with a bare PATH: add the venv and nvm (the Vercel CLI for the dashboard).
PATH="$PWD/.venv/bin:$PATH"
shopt -s nullglob
for dir in "$HOME"/.nvm/versions/node/*/bin; do PATH="$dir:$PATH"; done
shopt -u nullglob
export PATH

# Chrome runs with a visible window (browser.headless: false), which needs an X display. A systemd
# user service inherits none, so fall back to the session display.
export DISPLAY="${DISPLAY:-:1}"

if pgrep -f "[.]venv/bin/jobpilot run" >/dev/null; then
  echo "$(date) jobpilot is already running"; exit 0
fi
if [[ ! -x .venv/bin/jobpilot ]]; then
  echo "$(date) .venv/bin/jobpilot not found: run  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

stop=$(date -d "today $STOP_AT" +%s)
seconds=$(( stop - $(date +%s) ))
if (( seconds < 600 )); then
  echo "$(date) too late in the day to start"; exit 0
fi

# Hold a sleep/idle inhibitor for exactly the run window, so the machine doesn't suspend mid-apply.
inhibit_pid=""
if command -v systemd-inhibit >/dev/null; then
  systemd-inhibit --what=sleep:idle --who=jobpilot --why="job-search run" --mode=block \
    sleep "$seconds" >/dev/null 2>&1 &
  inhibit_pid=$!
fi
release() { [[ -n "$inhibit_pid" ]] && kill "$inhibit_pid" 2>/dev/null; }
trap release EXIT

echo "$(date) starting jobpilot for ${seconds}s (display $DISPLAY, stopping at $STOP_AT)"
.venv/bin/jobpilot run &
pid=$!

# Check the real clock: a plain `sleep` would drift across a suspend and keep yesterday's run alive.
while kill -0 "$pid" 2>/dev/null && (( $(date +%s) < stop )); do sleep 60; done
kill -INT "$pid" 2>/dev/null                      # Ctrl+C: jobpilot shuts down cleanly
for _ in {1..60}; do kill -0 "$pid" 2>/dev/null || break; sleep 2; done
kill -TERM "$pid" 2>/dev/null
wait "$pid"
echo "$(date) stopped for the day"
