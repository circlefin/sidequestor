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

# tick_dispatch.test.sh — the Slack dependency gates of the tick.py orchestrator.
#
# slack_gate must drop ONLY Slack-needing targets during an outage (the ~183/day stall that a
# per-target gate replaced), and needs_slack must classify a target's Slack dependency correctly.
# These are the data-loss edges, so most cases assert something is GATED.

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
echo "────────────────────────────────────────────────────────────────────────────"
echo "tick_dispatch: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
