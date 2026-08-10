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

# plan.test.sh — the two pure dispatch-planning decisions.
#
# rotate(): the fairness rotation. It distributes dirty targets fairly across ticks ONLY if
# the input order is stable — with an unsorted input it shuffles instead, which is the exact
# bug that produced a flaky golden and revealed the rotation had been decorative. These cases
# pin the rotation to a known input order.
#
# breaker_open(): the per-target hourly circuit breaker, and its fail-CLOSED behaviour on bad
# input (withhold rather than risk running a loop).

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
. "$SCRIPT_DIR/tests/lib/harness.sh"
PLAN="$SCRIPT_DIR/dispatch/plan.py"

rot()   { python3 "$PLAN" rotate "$1" "$2"; }
order() { rot "$1" "$2" | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['order']))"; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }

echo "── rotate: cursor 0 leaves the order alone ────────────────────────────────"
eq "cursor 0 → unchanged" "$(order '["a","b","c"]' 0)" '["a", "b", "c"]'
eq "...offset 0" "$(rot '["a","b","c"]' 0 | field offset)" '0'

echo
echo "── rotate: the cursor walks the start position forward ────────────────────"
eq "cursor 1 → starts at second" "$(order '["a","b","c"]' 1)" '["b", "c", "a"]'
eq "cursor 2 → starts at third"  "$(order '["a","b","c"]' 2)" '["c", "a", "b"]'

echo
echo "── rotate: the cursor is unbounded, taken modulo the count ────────────────"
eq "cursor 3 wraps to 0"        "$(order '["a","b","c"]' 3)" '["a", "b", "c"]'
eq "cursor 4 wraps to offset 1" "$(order '["a","b","c"]' 4)" '["b", "c", "a"]'
eq "...offset is 1, not 4"      "$(rot '["a","b","c"]' 4 | field offset)" '1'

echo
echo "── rotate: every target still appears exactly once (nothing dropped) ──────"
ROT=$(order '["a","b","c","d","e"]' 3)
eq "all five present, rotated" "$ROT" '["d", "e", "a", "b", "c"]'

echo
echo "── rotate: degenerate inputs do not crash the tick ────────────────────────"
eq "empty list → empty"        "$(order '[]' 5)" '[]'
eq "single target → itself"    "$(order '["only"]' 7)" '["only"]'
# The dangerous outcome of a bad cursor is a crashed tick, not a slightly unfair order.
eq "non-integer cursor → treated as 0" "$(order '["a","b"]' "junk")" '["a", "b"]'
eq "negative cursor → treated as 0"    "$(order '["a","b"]' "-3")" '["a", "b"]'

echo
echo "── rotate: the fairness property — successive ticks cover everyone ────────"
# With a fan-out of 1 and three permanently-dirty targets, three ticks must dispatch all
# three, each starting one further along. This is the property the decorative version failed.
A=$(order '["a","b","c"]' 0 | python3 -c "import json,sys;print(json.load(sys.stdin)[0])")
B=$(order '["a","b","c"]' 1 | python3 -c "import json,sys;print(json.load(sys.stdin)[0])")
C=$(order '["a","b","c"]' 2 | python3 -c "import json,sys;print(json.load(sys.stdin)[0])")
eq "three ticks, three different heads" "$A$B$C" "abc"

echo
echo "── breaker_open: at or above the cap blocks ───────────────────────────────"
python3 "$PLAN" breaker-open 25 25 >/dev/null && ok "recent==cap → open (blocked)" || bad "at cap should block"
python3 "$PLAN" breaker-open 30 25 >/dev/null && ok "recent>cap → open" || bad "over cap should block"
python3 "$PLAN" breaker-open 24 25 >/dev/null && bad "under cap should NOT block" || ok "recent<cap → not open"
python3 "$PLAN" breaker-open 0 25 >/dev/null && bad "zero should not block" || ok "zero recent → not open"

echo
echo "── breaker_open: fails CLOSED on unreadable input ─────────────────────────"
# If the recent count cannot be read, withhold rather than risk running a loop.
python3 "$PLAN" breaker-open "junk" 25 >/dev/null && ok "garbage recent → open (fail closed)" || bad "garbage should block"
python3 "$PLAN" breaker-open 5 "junk" >/dev/null && ok "garbage cap → open (fail closed)" || bad "garbage cap should block"

echo
echo "── exit codes match the shell's usage (0 = blocked, 1 = proceed) ──────────"
python3 "$PLAN" breaker-open 30 25 >/dev/null; eq "open exits 0" "$?" "0"
python3 "$PLAN" breaker-open 1 25 >/dev/null;  eq "not-open exits 1" "$?" "1"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "plan: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
