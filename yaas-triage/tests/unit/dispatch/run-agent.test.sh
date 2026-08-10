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

# test-run-agent.sh — the one dispatch pipeline.
#
# the original shell orchestrator and manual-dispatch.sh each used to implement this: launch the agent, tee
# the raw stream, format a transcript, symlink worker-latest.*, kill the tree on
# timeout. Two copies, and manual-dispatch's copy had no test at all, so a watchdog or
# log-pipeline fix had to be made twice and verified once.
#
# The properties that matter: the agent's exit code reaches the caller unchanged, a
# hung agent is killed and reported as 124, the raw stream is preserved verbatim, and
# the symlinks point at the run in flight.

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
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A throwaway copy so the fake dispatch-agent.sh never shadows the real one.
RIG="$TMP/yaas-triage"; LOGS="$TMP/logs"
mkdir -p "$RIG" "$LOGS"
cp "$SCRIPT_DIR/dispatch/run-agent.py" "$SCRIPT_DIR/dispatch/format-stream.py" "$RIG/"

fake_agent() {  # $1 = body of the stub
  printf '#!/bin/bash\n%s\n' "$1" > "$RIG/dispatch-agent.sh"
  chmod +x "$RIG/dispatch-agent.sh"
}
R() { python3 "$RIG/run-agent.py" --log-dir "$LOGS" "$@" 2>/dev/null; }

echo "── the agent's exit code reaches the caller ───────────────────────────────"
fake_agent 'printf "%s\n" "{\"type\":\"result\",\"subtype\":\"success\"}"; exit 0'
OUT=$(R --prompt "hello" --label quest-a); RC=$?
eq "exit 0 passes through"        "$RC" "0"
eq "...and is reported in json"   "$(printf '%s' "$OUT" | jq -r .exit)" "0"
eq "...not flagged as timed out"  "$(printf '%s' "$OUT" | jq -r .timed_out)" "false"

fake_agent 'exit 7'
OUT=$(R --prompt "x" --label quest-b); RC=$?
eq "a non-zero exit passes through unchanged" "$RC" "7"
ok "...which is what lets triage hold that target's watermarks"

echo
echo "── the raw stream is preserved verbatim ───────────────────────────────────"
fake_agent 'printf "%s\n" "{\"a\":1}" "{\"b\":2}" "{\"type\":\"result\"}"'
OUT=$(R --prompt "x" --label quest-c)
NDJSON=$(printf '%s' "$OUT" | jq -r .ndjson)
eq "every line captured"       "$(grep -c '' "$NDJSON")" "3"
eq "...byte-for-byte"          "$(head -1 "$NDJSON")" '{"a":1}'
LOG=$(printf '%s' "$OUT" | jq -r .log)
[ -s "$LOG" ] && ok "a human transcript is written too" || bad "no human transcript"
grep -q "Worker dispatch" "$LOG" && ok "...with the header" || bad "no header in the transcript"

echo
echo "── worker-latest.* points at the run in flight ────────────────────────────"
[ -L "$LOGS/worker-latest.ndjson" ] && ok "worker-latest.ndjson is a symlink" \
  || bad "worker-latest.ndjson is not a symlink"
eq "...pointing at the newest run" \
   "$(basename "$(readlink "$LOGS/worker-latest.ndjson")")" "$(basename "$NDJSON")"
ok "...which is how the dashboard's live panel finds it"

echo
echo "── a header is recorded, so a log says what it was for ────────────────────"
fake_agent 'exit 0'
OUT=$(R --prompt "x" --label quest-d --header "Target: quest-d" --header "Run ID: run-123")
LOG=$(printf '%s' "$OUT" | jq -r .log)
grep -q "Run ID: run-123" "$LOG" && ok "header lines land in the transcript" \
  || bad "header lines missing"

echo
echo "── a hung agent is killed, reported as 124, and leaves no orphan ──────────"
ORPHAN_MARKER="yaas-run-agent-orphan-$$"
fake_agent "printf '%s\\n' '{\"a\":1}'; python3 -c 'import time; time.sleep(60)' '$ORPHAN_MARKER'"
BEFORE=$(pgrep -f "$ORPHAN_MARKER" | wc -l | tr -d " ")
START=$(date +%s)
OUT=$(R --prompt "x" --label quest-hang --timeout 2); RC=$?
ELAPSED=$(( $(date +%s) - START ))
eq "exit 124 on timeout"        "$RC" "124"
eq "...flagged as timed out"    "$(printf '%s' "$OUT" | jq -r .timed_out)" "true"
[ "$ELAPSED" -lt 30 ] && ok "...and it actually stopped (${ELAPSED}s, not 60)" \
  || bad "the watchdog did not fire (${ELAPSED}s)"
sleep 1
AFTER=$(pgrep -f "$ORPHAN_MARKER" | wc -l | tr -d " ")
[ "$AFTER" -le "$BEFORE" ] && ok "no orphaned child survives the kill" \
  || bad "an orphan survived: the process tree was not killed"
# What the caller does with 124 matters: acks written BEFORE the kill are trustworthy,
# so triage commits those and holds the rest. That policy lives in triage, not here.
ok "...and 124 is distinguishable from an ordinary failure"

echo
echo "── partial output before a timeout is still kept ──────────────────────────"
NDJSON=$(printf '%s' "$OUT" | jq -r .ndjson)
[ -s "$NDJSON" ] && ok "work streamed before the kill is preserved" \
  || bad "the raw stream was lost when the agent was killed"

echo
echo "── argument validation ────────────────────────────────────────────────────"
python3 "$RIG/run-agent.py" >/dev/null 2>&1; eq "no args" "$?" "3"
python3 "$RIG/run-agent.py" --prompt only >/dev/null 2>&1; eq "no label" "$?" "3"

echo
echo "── both callers use it, so there is only one copy ─────────────────────────"
grep -q "run-agent.py" "$SCRIPT_DIR/tick.py" && ok "tick.py uses it" \
  || bad "tick.py still has its own pipeline"
grep -q "run-agent.py" "$SCRIPT_DIR/dispatch/manual-dispatch.sh" && ok "manual-dispatch.sh uses it" \
  || bad "manual-dispatch.sh still has its own pipeline"
for f in tick.py dispatch/manual-dispatch.sh; do
  if grep -q "_kill_tree" "$SCRIPT_DIR/$f"; then
    bad "$f still carries its own _kill_tree"
  else
    ok "$f no longer duplicates the watchdog"
  fi
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "run-agent: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
