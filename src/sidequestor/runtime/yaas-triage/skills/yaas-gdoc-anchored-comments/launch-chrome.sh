#!/usr/bin/env bash
set -euo pipefail

URL="${1:-https://docs.google.com}"
WORKSPACE="${SIDEQUESTOR_WORKSPACE:-$PWD}"
PROFILE="${SIDEQUESTOR_GDOC_CHROME_PROFILE:-$WORKSPACE/state/browser-profiles/gdoc-comments}"
CHROME="${SIDEQUESTOR_GDOC_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
CDP_URL="${SIDEQUESTOR_GDOC_CDP_URL:-http://127.0.0.1:9222}"
PORT="${CDP_URL##*:}"
PORT="${PORT%%/*}"
HEADLESS="${HEADLESS:-0}"

if curl -sS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; then
  echo "Chrome CDP is already running at $CDP_URL"
  exit 0
fi

[ -x "$CHROME" ] || { echo "Chrome not found at: $CHROME" >&2; exit 1; }
mkdir -p "$PROFILE"
args=(--remote-debugging-port="$PORT" --user-data-dir="$PROFILE"
  --no-first-run --no-default-browser-check)
if [ "$HEADLESS" != "0" ]; then
  args+=(--headless=new --window-size=1400,1800)
fi
"$CHROME" "${args[@]}" "$URL" >/dev/null 2>&1 &

for _ in $(seq 1 20); do
  sleep 0.5
  if curl -sS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; then
    echo "Chrome CDP is ready at $CDP_URL (profile: $PROFILE)"
    if [ "$HEADLESS" = "0" ]; then
      echo "Sign into Google if prompted, then leave this Chrome profile available to Sidequestor."
    fi
    exit 0
  fi
done
echo "Timed out waiting for Chrome CDP at $CDP_URL" >&2
exit 1
