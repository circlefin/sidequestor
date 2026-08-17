#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# unacked-backoff.test.sh — a watch that makes no progress backs off; it is never parked.
#
# Two failure modes shape this. (1) A worker acks one watch `blocked` on consecutive ticks, hits
# YAAS_UNACKED_PROMOTE, and classify() returns misconfig "watermark held pending review". Nothing
# re-dispatches it and nothing surfaces it, so the real request behind it sits untouched behind a
# "coming right up" reply. Parking it and filing an approval card for a human is not a fix: it
# just moves the stall. (2) An offline stretch produces those cards in bulk, because a worker that
# cannot reach the API cannot ack, and "did not ack" scores the same as "ran and refused to ack".
# Cards like that are not actionable.
#
# So no-progress BACKS OFF and keeps retrying forever — 5m doubling to a 24h cap — and files
# nothing. A transient failure heals itself; a permanent one costs a dispatch a day and stays
# visible on the dashboard. The watermark is held throughout either way.
#
# Case (2) is also handled upstream of all this: a tick with no network does not run at all, so
# an offline stretch cannot produce a strike in the first place. See tick-offline-gate.test.sh.

set -u
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
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

ROOT="$TMP/repo"
TRIAGE="$ROOT/yaas-triage"
QUEST="$ROOT/state/quests/active/quest-stranded"
mkdir -p "$TRIAGE/checkers" "$TRIAGE/ledger" "$TRIAGE/dispatch" "$TRIAGE/ops" \
         "$TRIAGE/surfaces" "$QUEST" "$ROOT/state/triage" "$ROOT/logs"

cp "$SCRIPT_DIR/tick.py" "$SCRIPT_DIR/tick_state.py" "$SCRIPT_DIR/tick_check.py" \
   "$SCRIPT_DIR/tick_dispatch.py" "$SCRIPT_DIR/reaction_config.py" "$TRIAGE/"
cp "$SCRIPT_DIR/approval_state.py" "$SCRIPT_DIR/approval_store.py" "$TRIAGE/"
cp "$SCRIPT_DIR/ledger/ensure-watch-ids.py" "$SCRIPT_DIR/ledger/ack-watch.py" \
   "$SCRIPT_DIR/ledger/checker-health.py" "$SCRIPT_DIR/ledger/watch-guard.py" \
   "$SCRIPT_DIR/ledger/commit.py" "$SCRIPT_DIR/ledger/housekeep.py" \
   "$SCRIPT_DIR/ledger/approval-helper.py" "$SCRIPT_DIR/ledger/add-watch.py" "$TRIAGE/ledger/"
cp "$SCRIPT_DIR/dispatch/slack-read-health.py" "$SCRIPT_DIR/dispatch/spend-window.py" \
   "$SCRIPT_DIR/dispatch/run-agent.py" "$SCRIPT_DIR/dispatch/plan.py" "$TRIAGE/dispatch/"
cp "$SCRIPT_DIR/checkers/result.py" "$SCRIPT_DIR/checkers/approval.py" "$TRIAGE/checkers/"

# Side-effect helpers tick calls unconditionally. Stubbed so this suite tests the gate, not
# the network: an unstubbed mcp-call.sh makes tick report SLACK DOWN and skip everything.
for helper in ops/sync-yaas-v2.sh surfaces/mcp-call.sh ops/notify.py ops/rotate-logs.py; do
  mkdir -p "$TRIAGE/$(dirname "$helper")"
  printf '#!/bin/bash\nexit 0\n' > "$TRIAGE/$helper"
  chmod +x "$TRIAGE/$helper"
done
printf '#!/usr/bin/env python3\n' > "$TRIAGE/dispatch/extract-tokens.py"

# A local (non-slack) checker so the quest needs no Slack surface at all. It reports clean:
# if the gate ever stops holding, the watch goes clean rather than dispatching, which keeps a
# regression here from spawning an agent.
cat > "$TRIAGE/checkers/local_fixture.py" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({"outcome": "clean", "count": 0, "preview": "", "complete": True}))
PY
chmod +x "$TRIAGE"/tick.py "$TRIAGE"/checkers/*.py

cat > "$QUEST/watch.json" <<'JSON'
{"watches":[{"type":"local_fixture","channel_id":"D0STRAND","thread_ts":"1786408315.983449","last_checked_ts":"100","reason":"the watch that goes quiet"}]}
JSON
printf '{"id":"quest-stranded","title":"Stranded quest"}\n' > "$QUEST/meta.json"
printf '# Stranded quest\n' > "$QUEST/context.md"
printf '%s\n' '{"ts":"2026-08-11T00:40:00Z","event":"blocked","note":"helper CLI reported no credentials"}' > "$QUEST/timeline.ndjson"

cd "$ROOT" || exit 1
python3 "$TRIAGE/ledger/ensure-watch-ids.py" quest-stranded "$QUEST/watch.json" >/dev/null
WID=$(jq -r '.watches[0].watch_id' "$QUEST/watch.json")

APPROVALS="$ROOT/state/pending-approvals.json"
COUNTS="$ROOT/state/triage/unacked-counts.json"

run_tick() {
  YAAS_AGENT=claude YAAS_TRIAGE_MAX_PARALLEL=1 YAAS_MAX_DISPATCH_FANOUT=10 \
    python3 "$TRIAGE/tick.py" >"$TMP/tick.$1.out" 2>&1 || true
}
n_items() { [ -f "$APPROVALS" ] && jq '[.items[]] | length' "$APPROVALS" || echo 0; }
watermark() { jq -r '.watches[0].last_checked_ts' "$QUEST/watch.json"; }

# `count` at the promote threshold is exactly what three no-progress dispatches leave behind.
# next_retry_ts decides whether this tick may check it.
seed_counts() { # $1 = next_retry_ts, $2 = last_utc
  jq -n --arg k "quest-stranded|$WID" --arg r "$1" --arg u "$2" \
    '{($k): {count: 3, first_utc: "2026-08-11T00:33:49Z", last_utc: $u,
             type: "local_fixture", last_status: "blocked",
             next_retry_ts: $r, backoff_sec: 300,
             last_error: "Cannot reach the API server (ENOTFOUND)"}}' > "$COUNTS"
}
reset_watch() { # put the watermark back so each case starts from the same place
  jq '.watches[0].last_checked_ts = "100"' "$QUEST/watch.json" > "$TMP/w" && mv "$TMP/w" "$QUEST/watch.json"
}

echo "── inside the backoff window: held, and nothing is asked of a human ───────"
seed_counts "9999999999" "2026-08-11T00:39:46Z"
run_tick 1
eq "no approval item is filed"     "$(n_items)" "0"
eq "the watermark does not move"   "$(watermark)" "100"
grep -q "gate_watch_stranded" "$ROOT/state/run-log.ndjson" 2>/dev/null \
  && bad "still emits gate_watch_stranded" || ok "nothing is parked for review"
grep -qi "backing off" "$TMP/tick.1.out" \
  && ok "the tick log says it is backing off" || bad "no backoff line in the tick log"

echo "── still nothing after repeated ticks ─────────────────────────────────────"
run_tick 2
run_tick 3
eq "no approvals after three ticks" "$(n_items)" "0"
eq "watermark still held"           "$(watermark)" "100"

echo "── once the window elapses, it retries on its own ─────────────────────────"
# next_retry_ts in the past: the checker runs, reports clean, and the watermark advances.
# Nobody clicked anything; this is the property the approval card could not provide.
seed_counts "1" "2026-08-11T00:39:46Z"
reset_watch
run_tick 4
[ "$(watermark)" != "100" ] && ok "the watch is checked again with no human action" \
                            || bad "watch still held after its backoff elapsed"
eq "and still files no approval" "$(n_items)" "0"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
