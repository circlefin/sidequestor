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

# tick_dispatch.test.sh — the two dispatch-phase gates of the tick.py orchestrator.
#
# slack_gate must drop ONLY Slack-needing targets during an outage (the ~183/day stall that a
# per-target gate replaced), and slice_plan must never grant a dispatch that runs past the tick
# budget nor start a target it cannot give MIN_SLICE seconds. Both are the money/data-loss
# edges, so most cases assert something is GATED or DEFERRED.

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
D="$SCRIPT_DIR/tick_dispatch.py"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

# Dirty watches: q-mail fired an email watch; q-chat fired a slack_thread; q-both fired both.
DW='[{"quest_id":"q-mail","type":"email"},{"quest_id":"q-chat","type":"slack_thread"},{"quest_id":"q-both","type":"email"},{"quest_id":"q-both","type":"slack_dm"}]'

echo "── needs_slack: judged on THIS tick's dirty watches, not the whole quest ──"
eq "reactions always needs slack" "$(python3 "$D" needs-slack reactions '[]')" "true"
eq "email-only dirty quest does NOT need slack" "$(python3 "$D" needs-slack q-mail "$DW")" "false"
eq "slack-dirty quest needs slack" "$(python3 "$D" needs-slack q-chat "$DW")" "true"
eq "mixed dirty quest needs slack (one slack watch is enough)" "$(python3 "$D" needs-slack q-both "$DW")" "true"

echo
echo "── slack_gate: Slack up → nothing gated, order preserved ──────────────────"
G=$(python3 "$D" slack-gate '["q-mail","q-chat","reactions"]' 1 "$DW")
eq "up: kept all" "$(echo "$G" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["kept"]))')" "q-mail,q-chat,reactions"
eq "up: gated none" "$(echo "$G" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["gated"]))')" "0"

echo
echo "── slack_gate: Slack DOWN → drop only Slack-needing, keep email-only ──────"
G=$(python3 "$D" slack-gate '["q-mail","q-chat","reactions"]' 0 "$DW")
eq "down: kept the email-only quest" "$(echo "$G" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["kept"]))')" "q-mail"
eq "down: gated chat + reactions" "$(echo "$G" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["gated"]))')" "q-chat,reactions"

echo
echo "── slice_plan: fanout cap defers the surplus targets ──────────────────────"
S=$(python3 "$D" slice '["a","b","c","d","e"]' 100000 0 300 3 1800)
eq "granted exactly max_fanout" "$(echo "$S" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["dispatch"]))')" "3"
eq "deferred the rest with a fanout reason" \
   "$(echo "$S" | python3 -c 'import json,sys;d=json.load(sys.stdin)["deferred"];print(len(d), d and "fanout" in d[0]["reason"])')" "2 True"

echo
echo "── slice_plan: watchdog capped to REMAINING budget, never past the ceiling ─"
# budget 2000, worker_timeout 1800: first target gets 1800; only 200 left, below min_slice → defer.
S=$(python3 "$D" slice '["a","b"]' 2000 0 300 4 1800)
eq "first target timeout = full worker_timeout" \
   "$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dispatch"][0]["timeout"])')" "1800"
eq "second deferred: remaining 200s < min_slice 300s" \
   "$(echo "$S" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d["dispatch"]), "min slice" in d["deferred"][0]["reason"])')" "1 True"

echo
echo "── slice_plan: a target near the ceiling gets a SHORTENED timeout, not full ─"
# spent 1000 of 2000; remaining 1000 >= min_slice → dispatch but timeout capped at 1000, not 1800.
S=$(python3 "$D" slice '["a"]' 2000 1000 300 4 1800)
eq "timeout shortened to remaining budget" \
   "$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dispatch"][0]["timeout"])')" "1000"

echo
echo "── slice_plan: empty target list is a no-op, not a crash ──────────────────"
S=$(python3 "$D" slice '[]' 3600 0 300 4 1800)
eq "nothing dispatched, nothing deferred" \
   "$(echo "$S" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d["dispatch"]),len(d["deferred"]))')" "0 0"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "tick_dispatch: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
