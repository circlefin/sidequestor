#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
ROOT="$TMP_DIR/repo"
TRIAGE="$ROOT/yaas-triage"
QUEST="$ROOT/state/quests/active/quest-test"
BROKEN_QUEST="$ROOT/state/quests/active/quest-broken"

mkdir -p "$TRIAGE/checkers" "$QUEST" "$BROKEN_QUEST" "$ROOT/state/triage" "$ROOT/logs"
cp "$SCRIPT_DIR/triage.sh" "$SCRIPT_DIR/ensure-watch-ids.py" "$TRIAGE/"

cat > "$QUEST/watch.json" <<'JSON'
{"watches":[
  {"type":"slack_ratelimit_fixture","last_checked_ts":"100","reason":"temporarily unreadable source"},
  {"type":"fixture","last_checked_ts":"100","reason":"first trigger"},
  {"type":"clean_fixture","last_checked_ts":"100","reason":"clean source"},
  {"type":"fixture","last_checked_ts":"100","reason":"second trigger"}
]}
JSON
printf '{"id":"quest-test"}\n' > "$QUEST/meta.json"
printf '# Test quest\n' > "$QUEST/context.md"
: > "$QUEST/timeline.ndjson"
printf '{broken json\n' > "$BROKEN_QUEST/watch.json"

cat > "$TRIAGE/checkers/fixture.py" <<'PY'
#!/usr/bin/env python3
print("1|fixture preview")
PY
cat > "$TRIAGE/checkers/clean_fixture.py" <<'PY'
#!/usr/bin/env python3
print("0|")
PY
cat > "$TRIAGE/checkers/slack_ratelimit_fixture.py" <<'PY'
#!/usr/bin/env python3
print("ratelimited|fixture rate limit")
PY
cat > "$TRIAGE/checkers/reactions.py" <<'PY'
#!/usr/bin/env python3
PY
cat > "$TRIAGE/dispatch-agent.sh" <<'SH'
#!/bin/bash
printf '%s' "$1" > state/captured-prompt.txt
printf '%s\n' '{"type":"result","subtype":"success"}'
SH
cat > "$TRIAGE/format-stream.py" <<'PY'
#!/usr/bin/env python3
import sys
for _ in sys.stdin:
    pass
PY
for helper in rotate-logs.sh notify.sh sync-yaas-v2.sh mcp-call.sh; do
  cat > "$TRIAGE/$helper" <<'SH'
#!/bin/bash
exit 0
SH
done
cat > "$TRIAGE/extract-tokens.py" <<'PY'
#!/usr/bin/env python3
PY
chmod +x "$TRIAGE"/*.sh "$TRIAGE"/checkers/*.py

YAAS_AGENT=claude YAAS_TRIAGE_MAX_PARALLEL=1 bash "$TRIAGE/triage.sh" >"$TMP_DIR/triage.out" 2>&1

if [ ! -f "$ROOT/state/captured-prompt.txt" ]; then
  cat "$TMP_DIR/triage.out" >&2
  cat "$ROOT/logs/triage.log" >&2
  exit 1
fi

WATCH_ID_1=$(jq -r '.watches[1].watch_id' "$QUEST/watch.json")
WATCH_ID_2=$(jq -r '.watches[3].watch_id' "$QUEST/watch.json")
PROMPT=$(cat "$ROOT/state/captured-prompt.txt")
DISPATCH=$(jq -c 'select(.event == "gate_dispatch")' "$ROOT/state/run-log.ndjson")

printf '%s' "$WATCH_ID_1" | grep -Eq '^watch-[0-9a-f]{16}$'
printf '%s' "$WATCH_ID_2" | grep -Eq '^watch-[0-9a-f]{16}$'
printf '%s' "$PROMPT" | grep -F "Exact dirty watches (JSON):" >/dev/null
printf '%s' "$PROMPT" | grep -F "\"watch_id\":\"$WATCH_ID_1\"" >/dev/null
printf '%s' "$PROMPT" | grep -F "\"watch_id\":\"$WATCH_ID_2\"" >/dev/null
[ "$(printf '%s' "$DISPATCH" | jq '.dirty_watches | length')" -eq 2 ]
[ "$(printf '%s' "$DISPATCH" | jq '.targets | length')" -eq 1 ]
[ "$(printf '%s' "$DISPATCH" | jq -r '.dirty_watches[0].quest_id')" = "quest-test" ]
[ "$(printf '%s' "$DISPATCH" | jq -r '.dirty_watches[0].watch_id')" = "$WATCH_ID_1" ]
[ "$(printf '%s' "$DISPATCH" | jq -r '.dirty_watches[1].watch_id')" = "$WATCH_ID_2" ]
[ "$(printf '%s' "$DISPATCH" | jq -r '.dirty_watches[0].type')" = "fixture" ]

# Only the two dispatched watches commit. Clean and rate-limited watches hold.
[ "$(jq -r '.watches[1].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[3].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[0].last_checked_ts' "$QUEST/watch.json")" = "100" ]
[ "$(jq -r '.watches[2].last_checked_ts' "$QUEST/watch.json")" = "100" ]

# One malformed quest does not prevent valid quests from dispatching.
grep -F "SKIP: quest-broken" "$ROOT/logs/triage.log" >/dev/null
[ "$(jq -r '.quests_dirty' "$ROOT/state/triage/last-run.json")" -eq 1 ]
[ "$(jq -r '.quests_skipped' "$ROOT/state/triage/last-run.json")" -eq 1 ]
[ "$(jq -r '.watches_skipped' "$ROOT/state/triage/last-run.json")" -eq 2 ]
jq -e 'select(.event == "gate_quest_unreadable" and .quest == "quest-broken")' "$ROOT/state/run-log.ndjson" >/dev/null

echo "dirty watch dispatch tests passed"
