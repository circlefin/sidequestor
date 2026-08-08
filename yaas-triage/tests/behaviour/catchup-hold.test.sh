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

# catchup-hold.test.sh — after a long silence, read everything before answering anything.
#
# THE SCENARIO. Triage is off for a week and 500-1000 messages accumulate. Watermarks hold,
# so nothing is lost, but on resume the checkers hand the worker the OLDEST unseen slice
# first — they must, since a watermark can only cross a prefix of the gap. Left alone the
# worker answers a seven-day-old question and then walks forward through the backlog, each
# reply blind to the hundreds of messages after it, in live customer threads.
#
# TWO DEFENCES, and they are complementary:
#   1. surfaces/slack-send.py holds any reply to a thread quiet >24h. Makes a backlog SAFE.
#   2. This: triage stops before dispatching or committing anything, writes a digest, and
#      waits for a human. Makes a backlog VISIBLE.
# Defence 1 alone still produces dozens of drafts about resolved threads; defence 2 alone
# leaves a trickle of stale sends after release.
#
# This suite lives in behaviour/ rather than unit/ because it spans triage.sh, catchup.py and
# health-monitor.py: the hold is only correct if all three agree.

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
TESTS_DIR="$SCRIPT_DIR/tests"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

CU() { YAAS_CATCHUP_REPO_ROOT="$1" python3 "$SCRIPT_DIR/ops/catchup.py" "${@:2}" 2>&1; }

# ── detection, in isolation ─────────────────────────────────────────────────────
mk_repo() {  # mk_repo <dir> <last_activity_iso|NONE>
  rm -rf "$1"; mkdir -p "$1/yaas-triage" "$1/state/triage"
  [ "$2" = "NONE" ] || printf '{"ts":"%s","event":"gate_idle"}\n' "$2" > "$1/state/run-log.ndjson"
}
armed() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get('armed'))"; }

echo "── detection ──────────────────────────────────────────────────────────────"
R="$TMP/r1"; mk_repo "$R" "2026-07-28T00:00:00Z"
eq "a week-long gap arms a hold" "$(armed "$(CU "$R" detect)")" "True"

R="$TMP/r2"; mk_repo "$R" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
eq "a fresh tick does not" "$(armed "$(CU "$R" detect)")" "False"

# A brand-new install has no history at all. Measuring a gap against nothing would arm a
# hold on first run and the agent would never start.
R="$TMP/r3"; mk_repo "$R" NONE
eq "a fresh install with no history never holds" "$(armed "$(CU "$R" detect)")" "False"

R="$TMP/r4"; mk_repo "$R" "$(python3 -c "import time;print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-3*3600)))")"
eq "3h is under the 6h default" "$(armed "$(CU "$R" detect)")" "False"
eq "...but not under a 1h threshold" \
   "$(armed "$(YAAS_CATCHUP_AFTER_HOURS=1 YAAS_CATCHUP_REPO_ROOT="$R" python3 "$SCRIPT_DIR/ops/catchup.py" detect)")" "True"

echo
echo "── release, and the trap that would make it unreleasable ──────────────────"
R="$TMP/r5"; mk_repo "$R" "2026-07-28T00:00:00Z"
CU "$R" detect >/dev/null
CU "$R" release | grep -q "released" && ok "release clears the hold" || bad "release failed"
# THE TRAP: right after release the gap is still a week, because nothing has logged yet.
# Without a resume marker, the very next detect re-arms and the hold can never be cleared.
eq "the next detect does NOT immediately re-arm" "$(armed "$(CU "$R" detect)")" "False"
# Once a real tick logs activity, normal behaviour resumes.
printf '{"ts":"%s","event":"gate_idle"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$R/state/run-log.ndjson"
eq "...and stays unarmed once a tick has logged" "$(armed "$(CU "$R" detect)")" "False"
CU "$R" release | grep -qi "no catch-up hold" \
  && ok "releasing when nothing is held says so" || bad "release did not report a no-op"

echo
echo "── the hold in a real tick: nothing sent, NOTHING committed ───────────────"
# The strong promise. Clean watermarks are held too: advancing them while holding dirty ones
# would leave a half-applied tick whose end state depends on how far it got.
FX="$TMP/fx"
python3 "$TESTS_DIR/lib/scenario.py" build \
  "$TESTS_DIR/differential/scenarios/dirty_acked_handled.json" "$FX" >/dev/null
printf '{"ts":"2026-07-28T00:00:00Z","event":"gate_idle"}\n' > "$FX/state/run-log.ndjson"
W="$FX/state/quests/active/q-demo/watch.json"
BEFORE_DIRTY=$(jq -r '.watches[0].last_checked_ts' "$W")
BEFORE_CLEAN=$(jq -r '.watches[1].last_checked_ts' "$W")
OUT=$( cd "$FX" && YAAS_SCENARIO="$FX/scenario.json" YAAS_TRIAGE_DIR="$FX/yaas-triage" \
         REPO_ROOT="$FX" python3 yaas-triage/tick.py 2>&1 )
printf '%s' "$OUT" | grep -q "CATCHUP HOLD" && ok "the tick reports a hold" || bad "no hold reported"
printf '%s' "$OUT" | grep -q "DISPATCH DONE" && bad "it dispatched during a hold" || ok "no dispatch"
eq "the DIRTY watermark did not move" "$(jq -r '.watches[0].last_checked_ts' "$W")" "$BEFORE_DIRTY"
eq "the CLEAN watermark did not move either" "$(jq -r '.watches[1].last_checked_ts' "$W")" "$BEFORE_CLEAN"
grep -q '"event":"gate_catchup_hold"' "$FX/state/run-log.ndjson" \
  && ok "the hold is recorded in the run log" || bad "no gate_catchup_hold event"

echo
echo "── the digest tells a human what accumulated ──────────────────────────────"
D="$FX/state/catchup-digest.md"
[ -f "$D" ] && ok "a digest was written" || bad "no digest"
# Match the SHAPE, not a value: the fixture's gap grows with wall-clock time, so a
# hardcoded 230 passes today and fails tomorrow. (It did.)
grep -qE 'silent for \*\*[0-9]+(\.[0-9]+)? hours\*\*' "$D" \
  && ok "...naming how long the silence was" || bad "no gap duration in the digest"
grep -q "q-demo" "$D" && ok "...and which quest has new activity" || bad "no quest named"
# The count comes from the scenario, so match the SHAPE rather than a number: what matters
# is that the human-readable detail survived and it is not just a bare watch type.
grep -qE '^- \[slack_thread\] [0-9]+ new' "$D" \
  && ok "...with the human-readable detail, not just a type" \
  || bad "digest lost the count/preview detail"
grep -q "catchup.py release" "$D" && ok "...and how to release it" || bad "no release instruction"

echo
echo "── release, then the SAME tick behaves normally ───────────────────────────"
CU "$FX" release >/dev/null
OUT=$( cd "$FX" && YAAS_SCENARIO="$FX/scenario.json" YAAS_TRIAGE_DIR="$FX/yaas-triage" \
         REPO_ROOT="$FX" python3 yaas-triage/tick.py 2>&1 )
printf '%s' "$OUT" | grep -q "DISPATCH DONE" && ok "it dispatches after release" || bad "still held after release"
AFTER=$(jq -r '.watches[0].last_checked_ts' "$W")
[ "$AFTER" != "$BEFORE_DIRTY" ] && ok "...and the watermark advances ($AFTER)" \
  || bad "watermark still frozen after release"

echo
echo "── a hold must never look like a healthy idle system ──────────────────────"
# While held, ticks complete and nothing is dirty, so every other condition reads healthy.
# Without this the agent would sit doing nothing for days and look fine.
H="$TMP/h"; mkdir -p "$H/yaas-triage" "$H/state/triage" "$H/logs"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{"last_triage_completed_utc":"%s","tick_started_utc":"%s"}\n' "$NOW" "$NOW" \
  > "$H/state/triage/last-run.json"
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$H" >/dev/null 2>&1 \
  && ok "baseline fixture is healthy" || bad "baseline fixture is not healthy"
printf '{"status":"awaiting_release","armed_at":"%s","gap_hours":230.8}\n' "$NOW" \
  > "$H/state/catchup.json"
HOUT=$(python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$H" 2>&1); HRC=$?
[ "$HRC" -ne 0 ] && ok "health-monitor reports a problem while held" \
  || bad "a held system reported healthy — it would look idle for days"
printf '%s' "$HOUT" | grep -q "catchup_awaiting_release" \
  && ok "...under its own condition name" || bad "condition not named"
printf '%s' "$HOUT" | grep -q "release" \
  && ok "...and says how to clear it" || bad "no remedy in the message"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "catch-up hold: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
