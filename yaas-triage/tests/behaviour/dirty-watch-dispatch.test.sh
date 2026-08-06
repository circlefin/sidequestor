#!/bin/bash
set -eu

# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
# yaas-triage/, found by walking up rather than by counting "..": these suites live at
# varying depths under tests/, and counting is the bug A1 removed from the scripts.
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
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
FAIL_QUEST="$ROOT/state/quests/active/quest-isolation-fail"
CONTRACT_QUEST="$ROOT/state/quests/active/quest-contract"

mkdir -p "$TRIAGE/checkers" "$QUEST" "$BROKEN_QUEST" "$CLEAN_QUEST" "$BUSINESS_QUEST" "$ERROR_QUEST" "$LOCAL_QUEST" "$RECOVERY_QUEST" "$CURRENT_BLOCK_QUEST" "$MISCONFIG_QUEST" "$FAIL_QUEST" "$CONTRACT_QUEST" "$ROOT/state/triage" "$ROOT/logs"
# The fixture mirrors the real layout. Flattening was a small lie about the tree before
# the reorganisation; now it is a broken one, since triage.sh looks for its collaborators
# in ledger/ and dispatch/.
mkdir -p "$TRIAGE/ledger" "$TRIAGE/dispatch" "$TRIAGE/ops"
cp "$SCRIPT_DIR/triage.sh" "$TRIAGE/"
cp "$SCRIPT_DIR/ledger/ensure-watch-ids.py" "$SCRIPT_DIR/ledger/ack-watch.py" \
   "$SCRIPT_DIR/ledger/checker-health.py" "$SCRIPT_DIR/ledger/watch-guard.py" "$TRIAGE/ledger/"
cp "$SCRIPT_DIR/dispatch/source-evidence.py" "$SCRIPT_DIR/dispatch/spend-window.py" \
   "$SCRIPT_DIR/dispatch/run-agent.py" "$TRIAGE/dispatch/"
cp "$SCRIPT_DIR/checkers/result.py" "$TRIAGE/checkers/"

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

cat > "$FAIL_QUEST/watch.json" <<'JSON'
{"watches":[{"type":"slack_dirty_fixture","last_checked_ts":"100","reason":"dispatch fails for this quest"}]}
JSON
cat > "$CONTRACT_QUEST/watch.json" <<'JSON'
{"watches":[
  {"type":"json_dirty_fixture","last_checked_ts":"100","reason":"checker supplies advance_to"},
  {"type":"json_truncated_fixture","last_checked_ts":"100","reason":"checker reports a saturated window"},
  {"type":"error_fixture","last_checked_ts":"100","reason":"checker keeps failing"},
  {"type":"json_dirty_fixture","last_checked_ts":"100","reason":"this one gets acked blocked"},
  {"type":"json_clean_truncated_fixture","last_checked_ts":"100","reason":"clean but window not drained"}
]}
JSON
printf '{"id":"quest-contract"}\n' > "$CONTRACT_QUEST/meta.json"
printf '# Contract quest\n' > "$CONTRACT_QUEST/context.md"
: > "$CONTRACT_QUEST/timeline.ndjson"
printf '{"id":"quest-isolation-fail"}\n' > "$FAIL_QUEST/meta.json"
printf '# Isolation fail quest\n' > "$FAIL_QUEST/context.md"
: > "$FAIL_QUEST/timeline.ndjson"

# Legacy `count|preview` output, deliberately left UNCONVERTED: triage must keep
# parsing it so an unconverted or third-party checker does not silently break.
cat > "$TRIAGE/checkers/fixture.py" <<'PY'
#!/usr/bin/env python3
print("1|fixture preview")
PY
# New JSON contract, dirty, naming its own safe cursor.
cat > "$TRIAGE/checkers/json_dirty_fixture.py" <<'PY'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
result.counted(2, "json dirty", advance_to=555.5, complete=True)
PY
# New JSON contract, dirty but the window SATURATED: its cursor must not move
# even after the worker acks it, because older items may be unseen.
cat > "$TRIAGE/checkers/json_truncated_fixture.py" <<'PY'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
result.counted(50, "saturated window", advance_to=777.7, complete=False)
PY
# CLEAN but the window saturated: zero matches does NOT prove nothing is older, so the
# cursor must be held. This is the case that a filtered Slack watch hits in production
# and that the clean path used to advance straight past.
cat > "$TRIAGE/checkers/json_clean_truncated_fixture.py" <<'PY'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
result.counted(0, "", advance_to=888.8, complete=False)
PY
# A checker that keeps failing: must back off, never dispatch.
cat > "$TRIAGE/checkers/error_fixture.py" <<'PY'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
result.error("fixture checker failure")
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
# Fixture: three pending :writing_hand: reactions. The worker stub acks the first
# handled, the second blocked, and never acks the third.
import json, os, sys
pending_path = sys.argv[4]
os.makedirs(os.path.dirname(pending_path), exist_ok=True)
json.dump({"writing_hand": ["1000.000001", "1000.000002", "1000.000003"]},
          open(pending_path, "w"), indent=2)
print("REACTIONS_DIRTY=1")
PY
cat > "$TRIAGE/dispatch/dispatch-agent.sh" <<'SH'
#!/bin/bash
# Per-target worker stub. Records every prompt, acks the dispatched manifest
# (minus any item listed in state/withhold-acks.txt), and exits non-zero for any
# target listed in state/fail-targets.txt.
PROMPT="$1"
printf '%s\n' "$PROMPT" >> state/captured-prompts.txt
printf '%s' "$PROMPT" > state/captured-prompt.txt
TARGET=$(printf '%s' "$PROMPT" | sed -n 's/.*dirty target: \([^.]*\)\..*/\1/p')
RID=$(printf '%s' "$PROMPT" | sed -n 's/.*run_id \([A-Za-z0-9._-]*\)\..*/\1/p')
printf '%s\n' "$TARGET" >> state/dispatched-targets.txt

if [ "$TARGET" = "quest-current-block" ]; then
  printf '{"ts":"%s","event":"blocked","blocker_kind":"slack_tooling_outage","reason":"Slack MCP failed during this worker run"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> state/quests/active/quest-current-block/timeline.ndjson
fi

if [ -n "$RID" ] && [ -f "state/triage/dispatch-$RID.json" ]; then
  for iid in $(jq -r '.items[].item_id' "state/triage/dispatch-$RID.json"); do
    grep -Fxq "$iid" state/withhold-acks.txt 2>/dev/null && continue
    if grep -Fxq "$iid" state/block-acks.txt 2>/dev/null; then
      python3 yaas-triage/ledger/ack-watch.py ack "$RID" "$iid" blocked "fixture blocked" >/dev/null
      continue
    fi
    python3 yaas-triage/ledger/ack-watch.py ack "$RID" "$iid" handled "fixture ack" >/dev/null
  done
fi

printf '%s\n' '{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":null,"result":{"content":[{"type":"text","text":"ok"}]}}}'
printf '%s\n' '{"type":"result","subtype":"success"}'

if grep -Fxq "$TARGET" state/fail-targets.txt 2>/dev/null; then exit 1; fi
exit 0
SH
cat > "$TRIAGE/dispatch/format-stream.py" <<'PY'
#!/usr/bin/env python3
import sys
for _ in sys.stdin:
    pass
PY
# Each stub must land where triage.sh looks for it, or triage falls through to the real
# helper (or to nothing). mcp-call.sh especially: if the health ping cannot find it, the
# tick reports SLACK DOWN and skips dispatch, and every dispatch assertion below fails
# for a reason that has nothing to do with what is being tested.
for helper in ops/sync-yaas-v2.sh surfaces/mcp-call.sh ops/notify.py ops/rotate-logs.py; do
  mkdir -p "$TRIAGE/$(dirname "$helper")"
  cat > "$TRIAGE/$helper" <<'SH'
#!/bin/bash
exit 0
SH
  chmod +x "$TRIAGE/$helper"
done
cat > "$TRIAGE/dispatch/extract-tokens.py" <<'PY'
#!/usr/bin/env python3
PY
chmod +x "$TRIAGE"/*.sh "$TRIAGE"/checkers/*.py

# Pre-assign watch IDs (deterministic, idempotent — triage re-runs this as a
# no-op) so the fixture can name the exact watch whose ack it will withhold.
python3 "$TRIAGE/ledger/ensure-watch-ids.py" quest-test "$QUEST/watch.json"
WITHHELD_ID=$(jq -r '.watches[3].watch_id' "$QUEST/watch.json")
# The blocked-ack case needs a quest that actually DISPATCHES. quest-error-blocked
# no longer does (its checker errors, which now backs off instead of dispatching),
# so the blocked ack rides on the contract quest's 4th watch.
python3 "$TRIAGE/ledger/ensure-watch-ids.py" quest-contract "$CONTRACT_QUEST/watch.json"
BLOCKED_ACK_ID=$(jq -r '.watches[3].watch_id' "$CONTRACT_QUEST/watch.json")
{ printf '%s\n' "$WITHHELD_ID"; printf '%s\n' "writing_hand:1000.000003"; } > "$ROOT/state/withhold-acks.txt"
{ printf '%s\n' "$BLOCKED_ACK_ID"; printf '%s\n' "writing_hand:1000.000002"; } > "$ROOT/state/block-acks.txt"
printf '%s\n' "quest-isolation-fail" > "$ROOT/state/fail-targets.txt"

YAAS_AGENT=claude YAAS_TRIAGE_MAX_PARALLEL=1 YAAS_MAX_DISPATCH_FANOUT=10 \
  bash "$TRIAGE/triage.sh" >"$TMP_DIR/triage.out" 2>&1 || true

if [ ! -f "$ROOT/state/captured-prompt.txt" ]; then
  cat "$TMP_DIR/triage.out" >&2
  cat "$ROOT/logs/triage.log" >&2
  exit 1
fi

WATCH_ID_1=$(jq -r '.watches[1].watch_id' "$QUEST/watch.json")
WATCH_ID_2=$(jq -r '.watches[3].watch_id' "$QUEST/watch.json")
PROMPTS=$(cat "$ROOT/state/captured-prompts.txt")
QUEST_PROMPT=$(grep -F "dirty target: quest-test." "$ROOT/state/captured-prompts.txt")
DISPATCH=$(jq -c 'select(.event == "gate_dispatch")' "$ROOT/state/run-log.ndjson")

printf '%s' "$WATCH_ID_1" | grep -Eq '^watch-[0-9a-f]{16}$'
printf '%s' "$WATCH_ID_2" | grep -Eq '^watch-[0-9a-f]{16}$'

# ── One invocation PER dirty target, not one carrying all of them. ───────────
[ "$(sort -u "$ROOT/state/dispatched-targets.txt" | wc -l | tr -d ' ')" -eq 6 ]
[ "$(wc -l < "$ROOT/state/captured-prompts.txt" | tr -d ' ')" -eq 6 ]
for _q in quest-test quest-contract quest-dirty-recovery quest-current-block quest-isolation-fail reactions; do
  grep -Fxq "$_q" "$ROOT/state/dispatched-targets.txt"
done
# Each prompt names exactly ONE target, so a failure in one cannot commit another.
[ "$(grep -c "dirty target: quest-test\." "$ROOT/state/captured-prompts.txt" | tr -d ' ')" -eq 1 ]
! grep -F "dirty target: quest-test," "$ROOT/state/captured-prompts.txt" >/dev/null

# quest-test's prompt carries its own two watch_ids and nobody else's.
printf '%s' "$QUEST_PROMPT" | grep -F "Exact dirty watches (JSON):" >/dev/null
printf '%s' "$QUEST_PROMPT" | grep -F "\"item_id\":\"$WATCH_ID_1\"" >/dev/null
printf '%s' "$QUEST_PROMPT" | grep -F "\"item_id\":\"$WATCH_ID_2\"" >/dev/null
printf '%s' "$QUEST_PROMPT" | grep -F "ack-watch.py ack" >/dev/null
printf '%s' "$QUEST_PROMPT" | grep -Eq 'run_id run-[0-9A-Za-z]+T[0-9]+Z-[0-9]+-[0-9]+'

# The tick-level manifest still records the whole dirty set for the dashboard.
[ "$(printf '%s' "$DISPATCH" | jq '.dirty_watches | length')" -eq 8 ]
[ "$(printf '%s' "$DISPATCH" | jq '.targets | length')" -eq 6 ]
[ "$(printf '%s' "$DISPATCH" | jq --arg id "$WATCH_ID_1" '[.dirty_watches[] | select(.quest_id == "quest-test" and .watch_id == $id and .checker_outcome == "dirty")] | length')" -eq 1 ]
[ "$(printf '%s' "$DISPATCH" | jq --arg id "$WATCH_ID_2" '[.dirty_watches[] | select(.quest_id == "quest-test" and .watch_id == $id and .checker_outcome == "dirty")] | length')" -eq 1 ]
# A checker `error` is no longer a dirty signal at all: it holds the watermark and
# backs off instead of waking a paid worker. So quest-error-blocked must NOT appear
# in the dirty manifest, and must NOT be dispatched.
[ "$(printf '%s' "$DISPATCH" | jq '[.dirty_watches[] | select(.checker_outcome == "error")] | length')" -eq 0 ]
! grep -Fxq "quest-error-blocked" "$ROOT/state/dispatched-targets.txt"
grep -F "BACKOFF: quest-error-blocked" "$ROOT/logs/triage.log" >/dev/null
[ "$(jq -r '.watches[0].last_checked_ts' "$ERROR_QUEST/watch.json")" = "100" ]
# and the failure is recorded so the next tick backs off further
[ "$(jq -r '[.[] | select(.consecutive_errors == 1)] | length' "$ROOT/state/triage/checker-health.json")" -ge 1 ]

# ── Only ACKED dispatched watches commit. ───────────────────────────────────
# watches[1] was dispatched and acked → advances.
# watches[3] was dispatched and its ack was withheld → held, even though the
#   worker exited 0. This is the whole point of the ledger: exit 0 is no longer
#   evidence that an item was handled.
# watches[0] rate-limited → held: we never saw its source this tick.
# watches[2] clean and drained → ADVANCES, even though a sibling is rate-limited and
#   another sibling is dirty. A watch's cursor is its own; the old code held every
#   watch in a quest whenever any one of them was skipped, which punished healthy
#   sources for a noisy neighbour.
[ "$(jq -r '.watches[1].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[3].last_checked_ts' "$QUEST/watch.json")" = "100" ]
[ "$(jq -r '.watches[0].last_checked_ts' "$QUEST/watch.json")" = "100" ]
[ "$(jq -r '.watches[2].last_checked_ts | tonumber > 100' "$QUEST/watch.json")" = "true" ]

# ── One commit path, not two. ───────────────────────────────────────────────
# A clean watch must advance using the CHECKER'S cursor, exactly like a dirty one.
# The clean path used to ignore advance_to and move everything to now-lag, which is
# how a saturated filtered window kept advancing after the tripwire was added.
CLEAN_ADV=$(jq -r '.watches[4].last_checked_ts' "$CONTRACT_QUEST/watch.json")
[ "$CLEAN_ADV" = "100" ]   # this one is clean but INCOMPLETE, so it is still held

# The withheld item is counted so a permanently silent worker cannot re-dispatch
# the same watch forever; check_quest promotes it to misconfig at the threshold.
[ "$(jq -r --arg k "quest-test|$WITHHELD_ID" '.[$k].count' "$ROOT/state/triage/unacked-counts.json")" -eq 1 ]
[ "$(jq -r --arg k "quest-test|$WATCH_ID_1" '.[$k] // "absent"' "$ROOT/state/triage/unacked-counts.json")" = "absent" ]

# ── Per-target isolation: quest-isolation-fail's worker exited 1, so its
#    watermark is held — and that failure did NOT stop quest-test from
#    committing above, which is what the old single-dispatch design could not do.
[ "$(jq -r '.watches[0].last_checked_ts' "$FAIL_QUEST/watch.json")" = "100" ]
jq -e 'select(.event == "gate_dispatch_failure" and (.targets | index("quest-isolation-fail")))' "$ROOT/state/run-log.ndjson" >/dev/null
jq -e 'select(.event == "gate_dispatch_success" and (.targets | index("quest-test")))' "$ROOT/state/run-log.ndjson" >/dev/null

# Every dispatch opened its own ack manifest.
[ "$(ls "$ROOT/state/triage"/dispatch-run-*.json | wc -l | tr -d ' ')" -eq 6 ]

# ── The checker result contract: advance_to and complete. ───────────────────
# watches[0] is dirty via the new JSON contract and named advance_to=555.5, so the
# cursor must land on exactly that, NOT on triage's now-minus-lag guess. This is
# what stops triage advancing past activity the checker never actually covered.
[ "$(jq -r '.watches[0].last_checked_ts' "$CONTRACT_QUEST/watch.json")" = "555.500000" ]
# watches[1] is dirty AND reported complete=false (its window saturated). It was
# dispatched and acked, but its cursor must still be held: a full page cannot prove
# there is nothing older, and advancing would skip messages nobody ever read.
[ "$(jq -r '.watches[1].last_checked_ts' "$CONTRACT_QUEST/watch.json")" = "100" ]
jq -e 'select(.event == "gate_watch_backlog" and .quest == "quest-contract")' "$ROOT/state/run-log.ndjson" >/dev/null
grep -F "BACKLOG [quest-contract]" "$ROOT/logs/triage.log" >/dev/null
# watches[2]'s checker errored: held, backed off, and NOT part of the dirty set.
[ "$(jq -r '.watches[2].last_checked_ts' "$CONTRACT_QUEST/watch.json")" = "100" ]
# watches[4] is CLEAN but reported complete=false. Zero new items means there is
# nothing to act on, so it must not make the quest dirty — but its cursor must ALSO be
# held, because a saturated window that matched nothing cannot prove nothing is older.
# Before this was fixed, a clean result dropped `complete` entirely and the clean-quest
# pass advanced it to now, skipping messages nobody had read. 14 live watches use the
# filters that make this reachable.
[ "$(jq -r '.watches[4].last_checked_ts' "$CONTRACT_QUEST/watch.json")" = "100" ]
grep -F "HOLD: quest-contract" "$ROOT/logs/triage.log" >/dev/null
jq -e 'select(.event == "gate_watch_backlog" and .reason == "clean but window not drained")' "$ROOT/state/run-log.ndjson" >/dev/null

# A watch whose cursor was HELD because its window was incomplete must be recorded as
# NO progress, even though the worker acked it. Passing the merely-acked set here would
# clear the breaker and let it re-dispatch forever at a cost.
TRUNC_ID=$(jq -r '.watches[1].watch_id' "$CONTRACT_QUEST/watch.json")
[ "$(jq -r --arg k "quest-contract|$TRUNC_ID" '.[$k].count' "$ROOT/state/triage/unacked-counts.json")" -eq 1 ]
# ...while the watch that genuinely committed has no counter at all.
COMMITTED_ID=$(jq -r '.watches[0].watch_id' "$CONTRACT_QUEST/watch.json")
[ "$(jq -r --arg k "quest-contract|$COMMITTED_ID" '.[$k] // "absent"' "$ROOT/state/triage/unacked-counts.json")" = "absent" ]

# The legacy `count|preview` fixture still parsed correctly (quest-test committed
# above via WATCH_ID_1), so the fallback path is not broken.
[ "$(jq -r '.watches_in_backoff' "$ROOT/state/triage/last-run.json")" -ge 2 ]
[ "$(jq -r '.watches_truncated' "$ROOT/state/triage/last-run.json")" -eq 2 ]  # one dirty+truncated, one clean+truncated

# ── An ack of `blocked` is NOT progress. ────────────────────────────────────
# quest-error-blocked's only watch was acked `blocked`: watermark held, and the
# no-progress counter must still increment. Clearing it here would defeat the
# retry bound and allow an item acked blocked forever to re-dispatch forever.
[ "$(jq -r '.watches[3].last_checked_ts' "$CONTRACT_QUEST/watch.json")" = "100" ]
[ "$(jq -r --arg k "quest-contract|$BLOCKED_ACK_ID" '.[$k].count' "$ROOT/state/triage/unacked-counts.json")" -eq 1 ]
[ "$(jq -r --arg k "quest-contract|$BLOCKED_ACK_ID" '.[$k].last_status' "$ROOT/state/triage/unacked-counts.json")" = "blocked" ]

# ── Reactions commit per pair, not all-or-nothing. ──────────────────────────
# Old behaviour deleted the whole pending file on exit 0, burying every reaction
# the worker skipped. Now: the handled pair clears; the blocked pair and the
# unacked pair are both retained for the next tick.
[ -f "$ROOT/state/triage/pending_reactions.json" ]
[ "$(jq -r '.writing_hand | length' "$ROOT/state/triage/pending_reactions.json")" -eq 2 ]
[ "$(jq -r '.writing_hand | index("1000.000001") // "gone"' "$ROOT/state/triage/pending_reactions.json")" = "gone" ]
jq -e '.writing_hand | index("1000.000002")' "$ROOT/state/triage/pending_reactions.json" >/dev/null
jq -e '.writing_hand | index("1000.000003")' "$ROOT/state/triage/pending_reactions.json" >/dev/null
jq -e 'select(.event == "gate_reactions_partial")' "$ROOT/state/run-log.ndjson" >/dev/null
# Reactions get the same retry bound as watches (they have no check_quest gate).
[ "$(jq -r '.["reactions|writing_hand:1000.000002"].count' "$ROOT/state/triage/unacked-counts.json")" -eq 1 ]
[ "$(jq -r '.["reactions|writing_hand:1000.000003"].count' "$ROOT/state/triage/unacked-counts.json")" -eq 1 ]
[ "$(jq -r '.["reactions|writing_hand:1000.000001"] // "absent"' "$ROOT/state/triage/unacked-counts.json")" = "absent" ]
# The rate-limited watch means this quest has not fully recovered yet.
[ "$(tail -n 1 "$QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# One malformed quest does not prevent valid quests from dispatching.
grep -F "SKIP: quest-broken" "$ROOT/logs/triage.log" >/dev/null
[ "$(jq -r '.quests_dirty' "$ROOT/state/triage/last-run.json")" -eq 5 ]  # error quest out, contract quest in
[ "$(jq -r '.quests_skipped' "$ROOT/state/triage/last-run.json")" -eq 3 ]  # broken + misconfigured + error-blocked (now backoff)
[ "$(jq -r '.watches_skipped' "$ROOT/state/triage/last-run.json")" -ge 4 ]
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
# (quest-error-blocked is not even dispatched now — its checker backs off.)
[ "$(tail -n 1 "$ERROR_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]
[ "$(tail -n 1 "$LOCAL_QUEST/timeline.ndjson" | jq -r '.event')" = "blocked" ]

# Failed worker tool calls are not recovery evidence.
cat > "$TMP_DIR/failed-worker.ndjson" <<'JSON'
{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":{"message":"failed"},"result":null}}
JSON
if python3 "$SCRIPT_DIR/dispatch/source-evidence.py" slack "$TMP_DIR/failed-worker.ndjson"; then
  echo "failed Slack tool call was incorrectly accepted as recovery evidence" >&2
  exit 1
fi
cat > "$TMP_DIR/slack-error-body.ndjson" <<'JSON'
{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"slack.slack_read_thread","status":"completed","error":null,"result":{"content":[{"type":"text","text":"{\"ok\":false,\"error\":\"invalid_auth\"}"}]}}}
JSON
if python3 "$SCRIPT_DIR/dispatch/source-evidence.py" slack "$TMP_DIR/slack-error-body.ndjson"; then
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
# The healthy sibling ADVANCES. A misconfigured watch is a permanent fault on ONE
# source; holding its neighbours as well just manufactures a second backlog while a
# human investigates. The broken watch is held and the event above makes it visible,
# which is what actually matters.
[ "$(jq -r '.watches[1].last_checked_ts | tonumber > 100' "$MISCONFIG_QUEST/watch.json")" = "true" ]
[ "$(jq -r '.watches[0].last_checked_ts' "$MISCONFIG_QUEST/watch.json")" = "100" ]

# Message content is not an envelope: a real read of a thread that discusses
# rate limits or auth errors still counts as recovery evidence.
cat > "$TMP_DIR/prose-worker.ndjson" <<'JSON'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"mcp__slack__slack_read_thread","input":{}}]}}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"Alice: the API returned ratelimited and invalid_auth all morning"}]}]}}
JSON
if ! python3 "$SCRIPT_DIR/dispatch/source-evidence.py" slack "$TMP_DIR/prose-worker.ndjson"; then
  echo "a successful Slack read was rejected because of its message content" >&2
  exit 1
fi

echo "dirty watch dispatch tests passed"
