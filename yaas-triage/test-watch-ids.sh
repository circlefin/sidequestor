#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
WATCH="$TMP_DIR/watch.json"

cat > "$WATCH" <<'JSON'
{
  "watches": [
    {"type":"slack_thread","channel_id":"C123","thread_ts":"123.456","last_checked_ts":"100"},
    {"type":"email","query":"from:test@example.com","last_checked_ts":"100"}
  ]
}
JSON

python3 "$SCRIPT_DIR/ensure-watch-ids.py" quest-test "$WATCH"
FIRST_IDS=$(jq -c '[.watches[].watch_id]' "$WATCH")
[ "$(jq '[.watches[].watch_id] | length' "$WATCH")" -eq 2 ]
[ "$(jq '[.watches[].watch_id] | unique | length' "$WATCH")" -eq 2 ]

# IDs are persisted and do not change when triage advances watermarks.
jq '.watches[].last_checked_ts = "200"' "$WATCH" > "$WATCH.tmp"
mv "$WATCH.tmp" "$WATCH"
python3 "$SCRIPT_DIR/ensure-watch-ids.py" quest-test "$WATCH"
[ "$(jq -c '[.watches[].watch_id]' "$WATCH")" = "$FIRST_IDS" ]

# A newly appended legacy entry receives its own ID without changing prior IDs.
jq '.watches += [{"type":"schedule","cron":"0 9 * * *","last_checked_ts":"200"}]' "$WATCH" > "$WATCH.tmp"
mv "$WATCH.tmp" "$WATCH"
python3 "$SCRIPT_DIR/ensure-watch-ids.py" quest-test "$WATCH"
[ "$(jq -c '[.watches[0].watch_id,.watches[1].watch_id]' "$WATCH")" = "$FIRST_IDS" ]
[ "$(jq '[.watches[].watch_id] | unique | length' "$WATCH")" -eq 3 ]

# Invalid or duplicated caller-provided IDs are repaired to the owned format.
jq '.watches[1].watch_id = .watches[0].watch_id | .watches[2].watch_id = "unsafe id"' "$WATCH" > "$WATCH.tmp"
mv "$WATCH.tmp" "$WATCH"
python3 "$SCRIPT_DIR/ensure-watch-ids.py" quest-test "$WATCH"
[ "$(jq '[.watches[].watch_id] | unique | length' "$WATCH")" -eq 3 ]
jq -e 'all(.watches[].watch_id; test("^watch-[0-9a-f]{16}(-[0-9]+)?$"))' "$WATCH" >/dev/null

echo "watch ID tests passed"
