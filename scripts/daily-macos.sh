#!/bin/zsh
# Started by launchd at 09:25 Mon–Sat: runs jobpilot for the working day, keeps the Mac awake, stops at 20:15.
cd "$(dirname "$0")/.." || exit 1
# launchd starts with a bare PATH; the Vercel CLI (for the dashboard) lives under nvm
for dir in $HOME/.nvm/versions/node/*/bin(N); do PATH="$dir:$PATH"; done
export PATH
if pgrep -f "[.]venv/bin/jobpilot run" >/dev/null; then
  echo "$(date) jobpilot is already running"; exit 0
fi
stop=$(date -j -f "%H:%M" "20:15" +%s)
seconds=$(( stop - $(date +%s) ))
(( seconds < 600 )) && { echo "$(date) too late in the day to start"; exit 0; }
echo "$(date) starting jobpilot for ${seconds}s"
caffeinate -i .venv/bin/jobpilot run &
pid=$!
# Check the real clock: a plain `sleep` pauses while the Mac sleeps, which kept yesterday's run alive overnight.
while kill -0 "$pid" 2>/dev/null && (( $(date +%s) < stop )); do sleep 60; done
kill -INT "$pid" 2>/dev/null
for _ in {1..60}; do kill -0 "$pid" 2>/dev/null || break; sleep 2; done
kill -TERM "$pid" 2>/dev/null
wait "$pid"
echo "$(date) stopped for the day"
