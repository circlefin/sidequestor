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
#   ./mutations.sh              run every mutation against triage.sh
#   ./mutations.sh tick.py      run them against the ported orchestrator instead
#
# The live tree is NEVER modified: mutations are applied to a throwaway copy, so this is
# safe to run alongside other suites and safe to kill at any moment.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TRIAGE="$(cd "$HERE/../.." && pwd)"
ORCH="${1:-triage.sh}"

# Work on a COPY of the whole tree. Mutating the live triage.sh meant any concurrently
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
  local name="$1" old="$2" new="$3" expect="$4"

  python3 - "$TARGET" "$old" "$new" <<'PY' || return 1
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
  restore

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
run_mutation "commit without requiring an ack" \
  'and (.watch_id as $i | any($acked[]; . == $i))' 'and true' \
  "partial_ack_isolates_items"

# 2. Advancing past a window the checker could not drain silently buries whatever was in
#    the uncovered part of the gap.
run_mutation "ignore the saturation flag" \
  '.complete != false' 'true' \
  "incomplete_window_holds"

# 3. When a checker names the boundary it actually covered, using "now" instead skips the
#    tail of a forward slice.
run_mutation "ignore the checker advance_to boundary" \
  'if ($m.advance_to // "") != ""' 'if false' \
  "advance_to_exact_value"

# 4. Dispatching into a Slack outage burns an invocation and can look like "no activity".
run_mutation "Slack health gate always passes" \
  '  "$MCP_CALL" slack_search_public_and_private' \
  '  true "$MCP_CALL" slack_search_public_and_private' \
  "slack_down_gates_dispatch"

echo
printf '%s mutation(s) caught, %s survived\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
