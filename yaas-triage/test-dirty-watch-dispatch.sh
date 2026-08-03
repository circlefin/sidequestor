#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
ROOT="$TMP_DIR/repo"
TRIAGE="$ROOT/yaas-triage"
QUEST="$ROOT/state/quests/active/quest-test"
BROKEN_QUEST="$ROOT/state/quests/active/quest-broken"
CLEAN_QUEST="$ROOT/state/quests/active/quest-clean-blocked"
BUSINESS_QUEST="$ROOT/state/quests/active/quest-business-blocked"
ERROR_QUEST="$ROOT/state/quests/active/quest-error-blocked"
LOCAL_QUEST="$ROOT/state/quests/active/quest-local-only-blocked"
RECOVERY_QUEST="$ROOT/state/quests/active/quest-dirty-recovery"
CURRENT_BLOCK_QUEST="$ROOT/state/quests/active/quest-current-block"
MISCONFIG_QUEST="$ROOT/state/quests/active/quest-misconfigured"

mkdir -p "$TRIAGE/checkers" "$QUEST" "$BROKEN_QUEST" "$CLEAN_QUEST" "$BUSINESS_QUEST" "$ERROR_QUEST" "$LOCAL_QUEST" "$RECOVERY_QUEST" "$CURRENT_BLOCK_QUEST" "$MISCONFIG_QUEST" "$ROOT/state/triage" "$ROOT/logs"
cp "$SCRIPT_DIR/triage.sh" "$SCRIPT_DIR/ensure-watch-ids.py" "$SCRIPT_DIR/worker-source-evidence.py" "$TRIAGE/"

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
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","reason":"Slack MCP tools unavailable"}' > "$QUEST/timeline.ndjson"
printf '{broken json\n' > "$BROKEN_QUEST/watch.json"
cat > "$CLEAN_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_clean_fixture","last_checked_ts":"100","reason":"clean recovery source"}]}
JSON
printf '{"id":"quest-clean-blocked"}\n' > "$CLEAN_QUEST/meta.json"
printf '# Clean blocked quest\n' > "$CLEAN_QUEST/context.md"
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","reason":"Slack MCP tools unavailable"}' > "$CLEAN_QUEST/timeline.ndjson"
cat > "$BUSINESS_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_clean_fixture","last_checked_ts":"100","reason":"clean business source"}]}
JSON
printf '{"id":"quest-business-blocked"}\n' > "$BUSINESS_QUEST/meta.json"
printf '# Business blocked quest\n' > "$BUSINESS_QUEST/context.md"
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","reason":"Partner unreachable on Slack Connect; invitation missing"}' > "$BUSINESS_QUEST/timeline.ndjson"
cat > "$ERROR_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_error_fixture","last_checked_ts":"100","reason":"failing Slack source"}]}
JSON
printf '{"id":"quest-error-blocked"}\n' > "$ERROR_QUEST/meta.json"
printf '# Error blocked quest\n' > "$ERROR_QUEST/context.md"
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","blocker_kind":"slack_tooling_outage","note":"Worker tooling failed"}' > "$ERROR_QUEST/timeline.ndjson"
cat > "$LOCAL_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"clean_fixture","last_checked_ts":"100","reason":"local-only source"}]}
JSON
printf '{"id":"quest-local-only-blocked"}\n' > "$LOCAL_QUEST/meta.json"
printf '# Local-only blocked quest\n' > "$LOCAL_QUEST/context.md"
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","reason":"Slack MCP tools unavailable"}' > "$LOCAL_QUEST/timeline.ndjson"
cat > "$RECOVERY_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_dirty_fixture","last_checked_ts":"100","reason":"dirty recovery source"}]}
JSON
printf '{"id":"quest-dirty-recovery"}\n' > "$RECOVERY_QUEST/meta.json"
printf '# Dirty recovery quest\n' > "$RECOVERY_QUEST/context.md"
printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","event":"blocked","blocker_kind":"slack_tooling_outage","note":"Worker Slack unavailable"}' > "$RECOVERY_QUEST/timeline.ndjson"
cat > "$CURRENT_BLOCK_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_dirty_fixture","last_checked_ts":"100","reason":"currently blocked source"}]}
JSON
printf '{"id":"quest-current-block"}\n' > "$CURRENT_BLOCK_QUEST/meta.json"
printf '# Current block quest\n' > "$CURRENT_BLOCK_QUEST/context.md"
: > "$CURRENT_BLOCK_QUEST/timeline.ndjson"

cat > "$MISCONFIG_QUEST/watch.json" <<'JSON'
{"watches":[
  {"type":"no_such_checker_fixture","last_checked_ts":"100","reason":"unknown watch type"},
  {"type":"clean_fixture","last_checked_ts":"100","reason":"healthy sibling watch"}
]}
JSON
printf '{"id":"quest-misconfigured"}\n' > "$MISCONFIG_QUEST/meta.json"
printf '# Misconfigured quest\n' > "$MISCONFIG_QUEST/context.md"
: > "$MISCONFIG_QUEST/timeline.ndjson"

cat > "$TRIAGE/checkers/fixture.py" <<'PY'
#!/usr/bin/env python3
print("1|fixture preview")
PY
cat > "$TRIAGE/checkers/clean_fixture.py" <<'PY'
#!/usr/bin/env python3
print("0|")
PY
cat > "$TRIAGE/checkers/slack_clean_fixture.py" <<'PY'
#!/usr/bin/env python3
print("0|")
PY
cat > "$TRIAGE/checkers/slack_error_fixture.py" <<'PY'
#!/usr/bin/env python3
print("error|fixture Slack failure")
PY
cat > "$TRIAGE/checkers/slack_dirty_fixture.py" <<'PY'
#!/usr/bin/env python3
print("1|fixture Slack activity")
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
printf '%s\n' '{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":null,"result":{"content":[{"type":"text","text":"ok"}]}}}'
printf '{"ts":"%s","event":"blocked","blocker_kind":"slack_tooling_outage","reason":"Slack MCP failed during this worker run"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> state/quests/active/quest-current-block/timeline.ndjson
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
[ "$(printf '%s' "$DISPATCH" | jq '.dirty_watches | length')" -eq 5 ]
[ "$(printf '%s' "$DISPATCH" | jq '.targets | length')" -eq 4 ]
[ "$(printf '%s' "$DISPATCH" | jq --arg id "$WATCH_ID_1" '[.dirty_watches[] | select(.quest_id == "quest-test" and .watch_id == $id and .checker_outcome == "dirty")] | length')" -eq 1 ]
[ "$(printf '%s' "$DISPATCH" | jq --arg id "$WATCH_ID_2" '[.dirty_watches[] | select(.quest_id == "quest-test" and .watch_id == $id and .checker_outcome == "dirty")] | length')" -eq 1 ]
[ "$(printf '%s' "$DISPATCH" | jq '[.dirty_watches[] | select(.quest_id == "quest-error-blocked" and .checker_outcome == "error")] | length')" -eq 1 ]

# Only the two dispatched watches commit. Clean and rate-limited watches hold.
[ "$(jq -r '.watches[1].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[3].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[0].last_checked_ts' "$QUEST/watch.json")" = "100" ]
[ "$(jq -r '.watches[2].last_checked_ts' "$QUEST/watch.json")" = "100" ]
# The rate-limited watch means this quest has not fully recovered yet.
[ "$(tail -n 1 "$QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# One malformed quest does not prevent valid quests from dispatching.
grep -F "SKIP: quest-broken" "$ROOT/logs/triage.log" >/dev/null
[ "$(jq -r '.quests_dirty' "$ROOT/state/triage/last-run.json")" -eq 4 ]
[ "$(jq -r '.quests_skipped' "$ROOT/state/triage/last-run.json")" -eq 2 ]  # quest-broken + quest-misconfigured
[ "$(jq -r '.watches_skipped' "$ROOT/state/triage/last-run.json")" -eq 3 ]
jq -e 'select(.event == "gate_quest_unreadable" and .quest == "quest-broken")' "$ROOT/state/run-log.ndjson" >/dev/null

# Clean checker reads alone cannot prove that the worker's Slack path recovered.
[ "$(tail -n 1 "$CLEAN_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# A successful dirty dispatch with an observed worker Slack read does recover.
[ "$(tail -n 1 "$RECOVERY_QUEST/timeline.ndjson" | jq -r '.event')" = "note" ]
[ "$(tail -n 1 "$RECOVERY_QUEST/timeline.ndjson" | jq -r '.recovered_from')" = "blocked" ]
[ "$(tail -n 1 "$RECOVERY_QUEST/timeline.ndjson" | jq -r '.recovered_source')" = "slack" ]

# A blocker created during this dispatch cannot be cleared by an earlier read.
[ "$(tail -n 1 "$CURRENT_BLOCK_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# A successful poll does not clear a genuine business/dependency blocker.
[ "$(tail -n 1 "$BUSINESS_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# Checker errors and successful non-Slack checks are not Slack recovery proof.
[ "$(tail -n 1 "$ERROR_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]
[ "$(tail -n 1 "$LOCAL_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# Failed worker tool calls are not recovery evidence.
cat > "$TMP_DIR/failed-worker.ndjson" <<'JSON'
{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":{"message":"failed"},"result":null}}
JSON
if python3 "$SCRIPT_DIR/worker-source-evidence.py" slack "$TMP_DIR/failed-worker.ndjson"; then
  echo "failed Slack tool call was incorrectly accepted as recovery evidence" >&2
  exit 1
fi
cat > "$TMP_DIR/slack-error-body.ndjson" <<'JSON'
{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":null,"result":{"content":[{"type":"text","text":"{\"ok\":false,\"error\":\"invalid_auth\"}"}]}}}
JSON
if python3 "$SCRIPT_DIR/worker-source-evidence.py" slack "$TMP_DIR/slack-error-body.ndjson"; then
  echo "Slack ok:false body was incorrectly accepted as recovery evidence" >&2
  exit 1
fi

# A watch type with no checker is a permanent misconfiguration, not a transient
# skip: it must be loud, recorded for the dashboard, and must not masquerade as
# a rate limit that will self-heal.
grep -F "MISCONFIG: quest-misconfigured" "$ROOT/logs/triage.log" >/dev/null
! grep -F "SKIP: quest-misconfigured" "$ROOT/logs/triage.log" >/dev/null
jq -e 'select(.event == "gate_watch_misconfigured" and .quest == "quest-misconfigured" and .type == "no_such_checker_fixture")' "$ROOT/state/run-log.ndjson" >/dev/null
[ "$(jq -r '.watches_misconfigured' "$ROOT/state/triage/last-run.json")" -eq 1 ]
# The healthy sibling watch is held too, which is why the event above must exist.
[ "$(jq -r '.watches[1].last_checked_ts' "$MISCONFIG_QUEST/watch.json")" = "100" ]

# Message content is not an envelope: a real read of a thread that discusses
# rate limits or auth errors still counts as recovery evidence.
cat > "$TMP_DIR/prose-worker.ndjson" <<'JSON'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"mcp__slack__slack_read_thread","input":{}}]}}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"Alice: the API returned ratelimited and invalid_auth all morning"}]}]}}
JSON
if ! python3 "$SCRIPT_DIR/worker-source-evidence.py" slack "$TMP_DIR/prose-worker.ndjson"; then
  echo "a successful Slack read was rejected because of its message content" >&2
  exit 1
fi

echo "dirty watch dispatch tests passed"
