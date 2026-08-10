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

# test-budget-gate.sh — the spend/dispatch ceilings actually withhold dispatch.
#
# The gate is the one thing standing between a runaway loop and the >$1k/13.5h
# incident repeating, and until now nothing tested it. Two layers here:
#
#   1. spend-window.py's window arithmetic and cap precedence, against a seeded log.
#   2. the original shell orchestrator end-to-end: a breach must withhold the dispatch, log the event, and
#      leave the dirty quest's watermark UNTOUCHED so the work re-surfaces. A gate
#      that dropped work in order to save money would be worse than no gate.

set -u
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
. "$SCRIPT_DIR/tests/lib/harness.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# jq renders a float as 5.0, so dollar amounts are compared numerically rather than
# as strings.
eqn() { if python3 -c "import sys; sys.exit(0 if abs(float(sys.argv[1])-float(sys.argv[2]))<1e-6 else 1)" "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

# ── seed a run log: $5 an hour ago, $30 six hours ago, $9 twenty hours ago ────
LOG="$TMP_DIR/run-log.ndjson"
stamp() { date -u -v-"$1"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "$1 hours ago" +%Y-%m-%dT%H:%M:%SZ; }
{
  printf '{"ts":"%s","event":"gate_dispatch_tokens","cost_usd":5.00,"targets":["q1"]}\n'  "$(stamp 0)"
  printf '{"ts":"%s","event":"gate_dispatch_tokens","cost_usd":30.00,"targets":["q1"]}\n' "$(stamp 5)"
  printf '{"ts":"%s","event":"gate_dispatch_tokens","cost_usd":9.00,"targets":["q1"]}\n'  "$(stamp 20)"
  printf '{"ts":"%s","event":"gate_dispatch_tokens","backend":"codex","targets":["q1"]}\n' "$(stamp 2)"
  printf '{"ts":"%s","event":"gate_dispatch_tokens","cost_usd":999.00,"targets":["q1"]}\n' "$(stamp 30)"
} > "$LOG"

echo "── spend-window.py arithmetic ─────────────────────────────────────────────"
W=$(python3 "$SCRIPT_DIR/dispatch/spend-window.py" "$LOG")
eqn "1h window sees only the most recent dispatch"  "$(printf '%s' "$W" | jq -r '.spend_1h')"  "5"
eqn "6h window adds the 5h-old one"                 "$(printf '%s' "$W" | jq -r '.spend_6h')"  "35"
eqn "24h window adds the 20h-old one"               "$(printf '%s' "$W" | jq -r '.spend_24h')" "44"
eqn "the 30h-old \$999 is outside every window"     "$(printf '%s' "$W" | jq -r '.spend_24h')" "44"
eq "a costless codex dispatch is counted, not costed" "$(printf '%s' "$W" | jq -r '.uncosted_24h')" "1"

echo
echo "── cap precedence: the tightest breached window is named ─────────────────"
b() { python3 "$SCRIPT_DIR/dispatch/spend-window.py" "$LOG" "$@" | jq -r '.breach'; }
case "$(b --cap-1h 1)"   in "1h spend \$5.00 over cap \$1.00")  ok "hourly cap breach is reported first" ;;
                            *) bad "hourly cap breach: got '$(b --cap-1h 1)'" ;; esac
case "$(b --cap-1h 100 --cap-24h 10)" in "24h spend \$44.00 over cap \$10.00") ok "daily cap breaches when hourly is fine" ;;
                            *) bad "daily cap breach: got '$(b --cap-1h 100 --cap-24h 10)'" ;; esac
case "$(b --cap-dispatch-6h 1)" in "6h dispatch count "*) ok "count cap breaches on dispatch volume alone" ;;
                            *) bad "count cap breach: got '$(b --cap-dispatch-6h 1)'" ;; esac
eq "within every cap there is no breach" "$(b --cap-1h 100 --cap-24h 100 --cap-dispatch-6h 100)" ""
eq "an unreadable log yields no output, so triage fails OPEN" \
   "$(python3 "$SCRIPT_DIR/dispatch/spend-window.py" "$TMP_DIR/nope" 2>/dev/null || true)" ""

echo
echo "── the original shell orchestrator end-to-end: a breach withholds dispatch and holds watermarks ─"
ROOT="$TMP_DIR/repo"; TRIAGE="$ROOT/yaas-triage"; Q="$ROOT/state/quests/active/quest-b"
mkdir -p "$TRIAGE/checkers" "$Q" "$ROOT/state/triage" "$ROOT/logs"
# The fixture mirrors the real layout. Flattening was a small lie about the tree before
# the reorganisation; now it is a broken one, since the original shell orchestrator looks for its collaborators
# in ledger/ and dispatch/.
mkdir -p "$TRIAGE/ledger" "$TRIAGE/dispatch" "$TRIAGE/ops"
cp "$SCRIPT_DIR/tick.py" "$SCRIPT_DIR/tick_state.py" "$SCRIPT_DIR/tick_check.py" "$SCRIPT_DIR/tick_dispatch.py" "$SCRIPT_DIR/atomic.py" "$TRIAGE/"
cp "$SCRIPT_DIR/ledger/ensure-watch-ids.py" "$SCRIPT_DIR/ledger/ack-watch.py" \
   "$SCRIPT_DIR/ledger/checker-health.py" "$SCRIPT_DIR/ledger/watch-guard.py" "$SCRIPT_DIR/ledger/commit.py" "$SCRIPT_DIR/ledger/housekeep.py" "$TRIAGE/ledger/"
cp "$SCRIPT_DIR/dispatch/source-evidence.py" "$SCRIPT_DIR/dispatch/spend-window.py" \
   "$SCRIPT_DIR/dispatch/run-agent.py" "$SCRIPT_DIR/dispatch/plan.py" "$TRIAGE/dispatch/"
cp "$SCRIPT_DIR/checkers/result.py" "$TRIAGE/checkers/"
cp "$LOG" "$ROOT/state/run-log.ndjson"

printf '{"watches":[{"type":"budget_fixture","last_checked_ts":"100","reason":"dirty"}]}\n' > "$Q/watch.json"
printf '{"id":"quest-b"}\n' > "$Q/meta.json"
printf '# b\n' > "$Q/context.md"
: > "$Q/timeline.ndjson"
cat > "$TRIAGE/checkers/budget_fixture.py" <<'PY'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
result.counted(1, "budget fixture activity")
PY
cat > "$TRIAGE/checkers/reactions.py" <<'PY'
#!/usr/bin/env python3
PY
# If the gate works, this must never run.
cat > "$TRIAGE/dispatch/dispatch-agent.sh" <<'SH'
#!/bin/bash
touch state/DISPATCH_HAPPENED
printf '%s\n' '{"type":"result","subtype":"success"}'
SH
cat > "$TRIAGE/dispatch/format-stream.py" <<'PY'
#!/usr/bin/env python3
import sys
for _ in sys.stdin: pass
PY
for helper in sync-yaas-v2.sh mcp-call.sh; do
  printf '#!/bin/bash\nexit 0\n' > "$TRIAGE/$helper"
done
printf '#!/usr/bin/env python3\n' > "$TRIAGE/ops/notify.py"
printf '#!/usr/bin/env python3\n' > "$TRIAGE/ops/rotate-logs.py"
chmod +x "$TRIAGE"/tick.py "$TRIAGE"/checkers/*.py

YAAS_AGENT=claude YAAS_MAX_SPEND_1H=1 python3 "$TRIAGE/tick.py" >"$TMP_DIR/breach.out" 2>&1 || true

if [ -f "$ROOT/state/DISPATCH_HAPPENED" ]; then
  bad "dispatch ran despite the hourly cap being breached"
else
  ok "dispatch withheld on breach"
fi
if jq -e 'select(.event == "gate_budget_exceeded")' "$ROOT/state/run-log.ndjson" >/dev/null 2>&1; then
  ok "gate_budget_exceeded recorded for the dashboard and notifier"
else
  bad "no gate_budget_exceeded event written"
fi
grep -q "BUDGET EXCEEDED" "$ROOT/logs/triage.log" && ok "operator-facing log line written" \
  || bad "nothing in triage.log about the breach"
# The point that matters most: withholding spend must not lose work.
eq "the dirty quest's watermark is HELD, so the work re-surfaces" \
   "$(jq -r '.watches[0].last_checked_ts' "$Q/watch.json")" "100"

echo
echo "── and with headroom, the same tick dispatches normally ───────────────────"
rm -f "$ROOT/state/DISPATCH_HAPPENED"
YAAS_AGENT=claude YAAS_MAX_SPEND_1H=500 python3 "$TRIAGE/tick.py" >"$TMP_DIR/ok.out" 2>&1 || true
[ -f "$ROOT/state/DISPATCH_HAPPENED" ] && ok "dispatch proceeds when within the cap" \
  || bad "dispatch withheld even though the cap had headroom (gate is stuck closed)"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "budget gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
