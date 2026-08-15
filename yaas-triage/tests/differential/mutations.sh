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

# mutations.sh — does the differential harness actually catch anything?
#
# A green suite proves nothing on its own. This deliberately breaks the orchestrator in
# ways that would each cause real data loss, and asserts the harness NOTICES. If a
# mutation survives, the harness has a blind spot at exactly that spot and the scenario
# set needs another case, not a shrug.
#
# It also guards against a subtler failure: a mutation whose search string has gone stale
# still "passes" because nothing was actually changed. Every mutation asserts its target
# text exists before running, so a refactor that moves the predicate makes this script
# fail loudly instead of quietly testing nothing. (That happened on the first attempt at
# this file: three mutations were no-ops against an older copy of the predicate and all
# three appeared to be caught by a harness that had never seen them.)
#
#   ./mutations.sh              run every mutation against tick.py (the orchestrator)
#
# The live tree is NEVER modified: mutations are applied to a throwaway copy, so this is
# safe to run alongside other suites and safe to kill at any moment.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TRIAGE="$(cd "$HERE/../.." && pwd)"
ORCH="${1:-tick.py}"

# Work on a COPY of the whole tree. Mutating the live orchestrator meant any concurrently
# running suite tested deliberately-broken code, and a SIGKILL mid-run would leave the
# real orchestrator mutated. Both are unacceptable for a tool whose whole job is to
# break things. YAAS_TRIAGE_SRC points the fixture builder at the copy.
SRC="${TMPDIR:-/tmp}/yaas-mut-src-$$"
mkdir -p "$SRC"
cp -R "$TRIAGE/." "$SRC/"
rm -rf "$SRC/tests"            # the harness itself is read from the real tree
export YAAS_TRIAGE_SRC="$SRC"

TARGET="$SRC/$ORCH"
PRISTINE="$SRC/.pristine-$ORCH"
[ -f "$TARGET" ] || { echo "no such orchestrator: $TARGET" >&2; exit 2; }
cp "$TARGET" "$PRISTINE"
restore() { cp "$PRISTINE" "$TARGET"; }
trap 'rm -rf "$SRC"' EXIT INT TERM

PASS=0; FAIL=0

# Each mutation: a name, the text to break, its replacement, and the scenario that MUST
# notice. Keeping the expected scenario explicit means a mutation caught only by
# accident (some unrelated scenario) still counts as a blind spot.
run_mutation() {
  # name  old  new  expect  [file]
  # `file` is a path relative to the copied source tree; defaults to the orchestrator.
  # It exists because the commit predicate lives in ledger/commit.py, not the orchestrator file:
  # a mutation must be able to break the code where the logic actually is.
  local name="$1" old="$2" new="$3" expect="$4" rel="${5:-$ORCH}"
  local mfile="$SRC/$rel"
  [ -f "$mfile" ] || { printf '  \033[31mMISSING\033[0m %s (no such file: %s)\n' "$name" "$rel"; FAIL=$((FAIL+1)); return 1; }
  local pristine="$mfile.pristine-mut"
  cp "$mfile" "$pristine"

  python3 - "$mfile" "$old" "$new" <<'PY' || { cp "$pristine" "$mfile"; rm -f "$pristine"; return 1; }
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
if old not in s:
    print(f"    STALE MUTATION: target text not found, mutation would be a no-op:\n"
          f"      {old}", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(s.replace(old, new))
PY

  local out failed
  out=$(bash "$HERE/run.sh" check "$ORCH" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
  failed=$(printf '%s\n' "$out" | awk '/^  FAIL/ {print $2}')
  cp "$pristine" "$mfile"; rm -f "$pristine"

  if printf '%s\n' "$failed" | grep -qx "$expect"; then
    printf '  \033[32mCAUGHT\033[0m   %s\n' "$name"
    printf '           by: %s\n' "$(printf '%s' "$failed" | tr '\n' ' ')"
    PASS=$((PASS+1))
  else
    printf '  \033[31mSURVIVED\033[0m %s\n' "$name"
    printf '           expected %s to fail; failures were: %s\n' \
      "$expect" "$(printf '%s' "${failed:-none}" | tr '\n' ' ')"
    printf '           → the harness is blind here. Add a scenario.\n'
    FAIL=$((FAIL+1))
  fi
}

printf '\033[1mmutation testing: %s\033[0m\n\n' "$ORCH"

# 1. The single most important line in the system. Without the ack condition we are back
#    to committing on exit code alone, which is the bug the whole ledger exists to kill.
#    Note a ZERO-ack dispatch cannot catch this: it takes a separate gate_dispatch_unacked
#    path and never reaches the predicate. It needs a partial ack.
# The commit predicate moved from inline jq in the original shell orchestrator into ledger/commit.py, so these two
# mutations target that file now. This is the whole point of the extraction: the most
# safety-critical logic is in one testable place. If it ever moves again, the STALE guard
# fails loudly rather than testing nothing.
run_mutation "commit without requiring an ack" \
  'w.get("quest_id") == qid and w.get("watch_id") in acked' \
  'w.get("quest_id") == qid' \
  "partial_ack_isolates_items" \
  "ledger/commit.py"

# 2. Advancing past a window the checker could not drain silently buries whatever was in
#    the uncovered part of the gap.
run_mutation "ignore the saturation flag" \
  'if w.get("complete") is False:' \
  'if False:' \
  "incomplete_window_holds" \
  "ledger/commit.py"

# 3. When a checker names the boundary it actually covered, using "now" instead skips the
#    tail of a forward slice. The advance-to-boundary logic lives in tick.py's advance_watches().
run_mutation "ignore the checker advance_to boundary" \
  'if adv is not None and str(adv) != "":' 'if False:' \
  "advance_to_exact_value"

# 4. Dispatching into a Slack outage burns an invocation and can look like "no activity".
#    The health gate is tick.py's slack_health_ok().
run_mutation "Slack health gate always passes" \
  '    return cp.returncode == 0' '    return True' \
  "slack_down_gates_dispatch"

# 4b. A Slack rate-limit must route as its own outcome (held + gate_watch_ratelimited), NOT
#     silently fall through — mishandling ratelimited is what makes a retry loop compound. The
#     routing lives in tick_check.classify().
run_mutation "ratelimited stops being handled as ratelimited" \
  '    if outcome == "ratelimited":' '    if False and outcome == "ratelimited":' \
  "watch_ratelimited_surfaces" \
  "tick_check.py"

# ── The rules C1a added coverage for ─────────────────────────────────────────
# These DELETE watch entries or decide dispatch ORDER. Before C1a no golden touched them at
# all, so a port could have dropped any of them and passed everything.

# 5. Never retiring a stale thread grows watch.json without bound.
run_mutation "stop retiring stale threads" \
  'return w.get("type") == "slack_thread" and _thread_epoch(w) < cutoff_epoch' \
  'return False' \
  "retire_stale_thread" \
  "ledger/housekeep.py"

# 6. Ignoring the per-quest window applies the 30-day default everywhere, which silently
#    stops tracking the long-running partner threads that set it to "never".
run_mutation "ignore the per-quest retire window" \
  'raw = meta.get("retire_slack_threads_after_days", default_days)' \
  'raw = default_days' \
  "retire_respects_never" \
  "ledger/housekeep.py"

# 7. An approval watch that is never retired fires forever after its draft is executed.
run_mutation "stop retiring completed approvals" \
  'return w.get("type") == "approval" and w.get("approval_id") in done_ids' \
  'return False' \
  "retire_completed_approval" \
  "ledger/housekeep.py"

# 8. A one-shot schedule that is never retired re-fires on every tick.
run_mutation "stop retiring fired one-shot schedules" \
  'return float(w.get("last_checked_ts") or 0) >= float(w["next_fire_ts"])' \
  'return False' \
  "retire_fired_one_shot_schedule" \
  "ledger/housekeep.py"

# 9. Dropping the fairness cursor lets one busy quest starve the others whenever a tick
#    cannot dispatch them all. Invisible on every single-target golden.
# The rotation moved from inline shell into dispatch/plan.py rotate(); break it there.
# offset=0 makes every tick start at the same target, so a dirty set larger than the fan-out
# starves the back of the list.
run_mutation "drop the fairness rotation" \
  'offset = c % n' \
  'offset = 0' \
  "fairness_rotation" \
  "dispatch/plan.py"

# 10. Reactions must jump the dispatch queue. Queued behind dirty quests, a reaction waits for
#     every one of them to finish before its worker even starts (measured 2026-08-08: 4.5
#     minutes behind three quest dispatches), and the emoji shows nothing meanwhile — the one
#     case where a human is actively watching and reads the delay as "it's broken".
run_mutation "reactions stop jumping the dispatch queue" \
  '    if "reactions" in rotated:' '    if False:' \
  "reactions_dispatch_first"

# 11. ...but reactions must not EAT a quest's slot. Jumping the queue while also consuming
#     the fan-out budget turns priority into starvation: at fan-out 1 with a reaction pending
#     across ticks, every dirty quest is deferred forever. Caught by Codex review, 2026-08-10.
run_mutation "reactions consume a quest fan-out slot" \
  '        over_fanout = target != "reactions" and quest_dispatched >= t.max_fanout' \
  '        over_fanout = quest_dispatched >= t.max_fanout or t.dispatched >= t.max_fanout' \
  "reactions_dont_starve_quests"

# ── NOT mutated here, deliberately: the CHECK-phase fairness rotation ────────
# tick_check.rotate_check_order() has no mutation in this file, and that is a considered
# omission rather than an oversight — verified empirically on 2026-08-10 by disabling it
# (offset always 0) and re-running: all 32 goldens still passed.
#
# It is invisible to this harness BY CONSTRUCTION. The rotation changes the order quests are
# EXECUTED in; run_tick then reassembles results in quest_dirs order before anything is
# logged, so watch movements, run-log events, ack manifests and exit code are all identical
# with or without it. There is nothing for a golden to diff.
#
# Contrast the dispatch rotation above, which IS caught: under the fan-out cap it changes
# WHICH targets get dispatched, and that shows up in the output. Same idea, different
# observability, so do not "fix" this by adding a mutation here — it would survive and be
# reported as a blind spot forever.
#
# The real guard is tests/unit/tick_check.test.sh, which asserts the property directly:
# given a budget that serves only the first 10 of 19 quests, every quest is still checked
# within a bounded number of ticks. That is the behaviour the rotation exists for, and it is
# testable at the unit level precisely because it is a property of the ORDER, not the output.

echo
printf '%s mutation(s) caught, %s survived\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
