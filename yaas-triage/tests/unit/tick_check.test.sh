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

# tick_check.test.sh — the six-way verdict routing of the tick.py orchestrator's analyze phase.
#
# classify() decides, per watch: misconfig | backoff | skip | hold | dirty | clean. Two
# production incidents lived in this routing: a ratelimited read that dispatched as dirty (the
# 2026-07-24 storm), and a clean-but-not-drained result that advanced past unseen items. Both
# are pinned here. The dangerous verdict is `dirty` (it dispatches) and `clean` (it advances),
# so most cases assert something HOLDS.

set -u
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
CHK="$SCRIPT_DIR/tick_check.py"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

WID='watch-abc123def456'
W='{"watch_id":"'$WID'","type":"slack_thread"}'
# c <result-json> <watch-json> [extra flags...] ; prints the verdict
c() { python3 "$CHK" "$1" "$2" "${@:3}" | python3 -c "import json,sys;print(json.load(sys.stdin)['verdict'])"; }
field() { python3 "$CHK" "$1" "$2" "${@:3}" | python3 -c "import json,sys;print(json.load(sys.stdin).get(sys.argv[1]))" "$3x" 2>/dev/null; }

echo "── the happy verdicts ─────────────────────────────────────────────────────"
eq "count>0 → dirty"  "$(c '{"outcome":"dirty","count":2,"complete":true,"advance_to":"100"}' "$W")" "dirty"
eq "count=0 complete → clean" "$(c '{"outcome":"clean","count":0,"complete":true}' "$W")" "clean"

echo
echo "── the 2026-07-24 lesson: ratelimited SKIPS, never dirty ──────────────────"
eq "ratelimited → skip (not dirty)" "$(c '{"outcome":"ratelimited","preview":"slow down"}' "$W")" "skip"

echo
echo "── the saturation lesson: clean-but-not-drained HOLDS ─────────────────────"
eq "count=0 complete=false → hold" "$(c '{"outcome":"clean","count":0,"complete":false}' "$W")" "hold"
# but count>0 with complete=false is still dirty (there's something to act on) — it just
# won't advance the watermark; that's the commit layer's job, not classify's.
eq "count>0 complete=false → still dirty" \
   "$(c '{"outcome":"dirty","count":3,"complete":false}' "$W")" "dirty"

echo
echo "── checker errors: backoff, then misconfig past the threshold ─────────────"
eq "first error → backoff" "$(c '{"outcome":"error","preview":"boom"}' "$W")" "backoff"
eq "5th error (promote=6) → still backoff" "$(c '{"outcome":"error","preview":"boom"}' "$W" --errors 4)" "backoff"
eq "6th error → misconfig" "$(c '{"outcome":"error","preview":"boom"}' "$W" --errors 5)" "misconfig"

echo
echo "── an active backoff window holds WITHOUT running the checker ─────────────"
eq "in-backoff → backoff (even with a dirty result)" \
   "$(c '{"outcome":"dirty","count":9}' "$W" --in-backoff)" "backoff"

echo
echo "── no-progress promotion: dispatched repeatedly, nothing acked ────────────"
eq "unacked below threshold → normal (dirty)" \
   "$(c '{"outcome":"dirty","count":1}' "$W" --unacked 2)" "dirty"
eq "unacked at threshold (3) → misconfig" \
   "$(c '{"outcome":"dirty","count":1}' "$W" --unacked 3)" "misconfig"

echo
echo "── structural: a bad watch_id or missing checker is misconfig ─────────────"
eq "malformed watch_id → misconfig" \
   "$(c '{"outcome":"dirty","count":1}' '{"watch_id":"nope","type":"slack_thread"}')" "misconfig"
eq "no executable checker → misconfig" \
   "$(c '{"outcome":"clean","count":0}' "$W" --no-checker)" "misconfig"

echo
echo "── the checker's own misconfig verdict is honoured ────────────────────────"
eq "outcome=misconfig → misconfig" \
   "$(c '{"outcome":"misconfig","preview":"no such checker type"}' "$W")" "misconfig"

echo

echo "── a checker's own outcome=hold is honoured (B1 bug: shell mismapped it) ───"
# github_pr/jira emit outcome=hold on a saturated/tie page. classify must hold, not error.
eq "outcome=hold count=0 → hold" "$(c '{"outcome":"hold","count":0,"complete":false,"preview":"tie"}' "$W")" "hold"
eq "outcome=hold count>0 → still hold (jira does this)" "$(c '{"outcome":"hold","count":2,"complete":false}' "$W")" "hold"

echo "── malformed/empty checker output does not crash — routes to a hold ───────"
eq "null result → backoff (treated as a checker error)" "$(c 'null' "$W")" "backoff"
eq "garbage outcome → backoff (error path)" "$(c '{"outcome":"weird"}' "$W")" "backoff"

echo
echo "── dirty carries advance_to and complete through for the commit layer ─────"
OUT=$(python3 "$CHK" '{"outcome":"dirty","count":2,"complete":true,"advance_to":"1785920000.0"}' "$W")
eq "advance_to preserved" "$(printf '%s' "$OUT" | python3 -c "import json,sys;print(json.load(sys.stdin)['advance_to'])")" "1785920000.0"
eq "complete preserved" "$(printf '%s' "$OUT" | python3 -c "import json,sys;print(json.load(sys.stdin)['complete'])")" "True"

echo
echo "── priority: structural checks beat the checker result ────────────────────"
# A dirty result on a watch that is ALSO past its no-progress threshold must still misconfig,
# because dispatching it again just burns another invocation.
eq "unacked-promote beats a dirty result" \
   "$(c '{"outcome":"dirty","count":5}' "$W" --unacked 3)" "misconfig"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "tick_check: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
