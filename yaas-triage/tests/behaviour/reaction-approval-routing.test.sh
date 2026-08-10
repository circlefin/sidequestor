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

# reaction-approval-routing.test.sh — a reaction draft's approval can actually self-execute.
#
# THE BUG. A :writing_hand: draft went to the review queue with quest_id="reactions", which has
# no quest folder, so the approval watch never armed (watch_armed=false). When the human marked
# it reviewed, nothing fired and it stranded at "reviewed" forever. A real self-DM draft sat
# stuck this way.
#
# THE FIX (reviewed as the safest option). approval-helper.py routes reaction-sourced approvals
# to a durable executor-only host quest, quest-reactions-approvals, so the ORDINARY reviewed-
# approval path (checkers/approval.py fires -> triage dispatches -> worker executes) works
# unchanged. It is a SPECIFIC map for the one known non-quest target, not a generic
# missing-quest fallback (which would hide typos), and it fails toward the visible
# watch_armed=false state rather than silently orphaning.
#
# This spans approval-helper.py (routing + bootstrap) and checkers/approval.py (firing), hence
# behaviour/. Lives entirely in a temp tree; touches no real state.

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
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# Isolated repo tree with the real ledger + checkers.
REPO="$TMP/repo"; mkdir -p "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/checkers" "$REPO/state/quests/active"
cp "$SCRIPT_DIR/ledger/approval-helper.py" "$REPO/yaas-triage/ledger/"
[ -f "$SCRIPT_DIR/ledger/approval.py" ] && cp "$SCRIPT_DIR/ledger/approval.py" "$REPO/yaas-triage/ledger/" 2>/dev/null
cp "$SCRIPT_DIR/checkers/approval.py" "$SCRIPT_DIR/checkers/result.py" "$REPO/yaas-triage/checkers/"

AH() { ( cd "$REPO" && python3 yaas-triage/ledger/approval-helper.py "$@" ); }
HOST="$REPO/state/quests/active/quest-reactions-approvals"
APPROVALS="$REPO/state/pending-approvals.json"

echo "── a reaction approval is routed to the durable host quest ────────────────"
ID=$(AH write '{"quest_id":"reactions","quest_title":"react","action_type":"slack_message","target":{"channel_id":"D1","thread_ts":"1.0"},"message_text":"hi","context":"c","risk_reason":"r"}')
[ -n "$ID" ] && ok "write returned an id ($ID)" || bad "write returned nothing"
[ -d "$HOST" ] && ok "the host quest was bootstrapped" || bad "host quest not created"
eq "the item is re-homed to the host quest" \
   "$(python3 -c "import json;print([i['quest_id'] for i in json.load(open('$APPROVALS'))['items'] if i['id']=='$ID'][0])")" \
   "quest-reactions-approvals"
eq "...with source=reactions kept for provenance" \
   "$(python3 -c "import json;print([i.get('source') for i in json.load(open('$APPROVALS'))['items'] if i['id']=='$ID'][0])")" \
   "reactions"

echo
echo "── the approval watch armed IN THE HOST (this is what was broken) ──────────"
eq "the host watch.json carries the approval watch" \
   "$(python3 -c "import json;print([w['approval_id'] for w in json.load(open('$HOST/watch.json'))['watches'] if w.get('type')=='approval'])")" \
   "['$ID']"
eq "...and the item is NOT flagged unarmed" \
   "$(python3 -c "import json;i=[x for x in json.load(open('$APPROVALS'))['items'] if x['id']=='$ID'][0];print(i.get('watch_armed','armed'))")" \
   "armed"

echo
echo "── the host quest is an executor-only, permanent quest ────────────────────"
grep -q "executor" "$HOST/context.md" && ok "context.md marks it an executor" || bad "context not executor-only"
grep -q "Do NOT append" "$HOST/context.md" && ok "...and forbids accruing follow-up watches" || bad "no no-follow-up rule"
eq "never retires its threads" \
   "$(python3 -c "import json;print(json.load(open('$HOST/meta.json')).get('retire_slack_threads_after_days'))")" "never"
eq "opens no sends of its own (allow_send false)" \
   "$(python3 -c "import json;print(json.load(open('$HOST/meta.json')).get('allow_send'))")" "False"

echo
echo "── the normal reviewed-approval path now fires (the fix's whole point) ─────"
CHK() { ( cd "$REPO" && python3 yaas-triage/checkers/approval.py "$1" ); }
WATCH=$(python3 -c "import json;print(json.dumps([w for w in json.load(open('$HOST/watch.json'))['watches'] if w.get('type')=='approval'][0]))")
# pending_review → not dirty (still waiting on the human)
eq "pending_review does not dispatch" "$(CHK "$WATCH" | python3 -c "import json,sys;print(json.load(sys.stdin)['outcome'])")" "clean"
# mark reviewed → the checker must report dirty so triage dispatches the host to execute it
AH start "$ID" >/dev/null 2>&1 || true   # noop; just ensure helper is callable
python3 -c "
import json
d=json.load(open('$APPROVALS'))
for i in d['items']:
    if i['id']=='$ID': i['status']='reviewed'
json.dump(d,open('$APPROVALS','w'))"
eq "reviewed DOES dispatch (was orphaned before)" \
   "$(CHK "$WATCH" | python3 -c "import json,sys;print(json.load(sys.stdin)['outcome'])")" "dirty"

echo
echo "── once executed, it does not re-dispatch (no double-send) ────────────────"
python3 -c "
import json
d=json.load(open('$APPROVALS'))
for i in d['items']:
    if i['id']=='$ID': i['status']='executed'
json.dump(d,open('$APPROVALS','w'))"
eq "executed is clean (no second dispatch)" \
   "$(CHK "$WATCH" | python3 -c "import json,sys;print(json.load(sys.stdin)['outcome'])")" "clean"

echo
echo "── a real quest is NOT rerouted (routing is specific to 'reactions') ──────"
mkdir -p "$REPO/state/quests/active/q-real"; echo '{"watches":[]}' > "$REPO/state/quests/active/q-real/watch.json"
RID=$(AH write '{"quest_id":"q-real","quest_title":"real","action_type":"slack_message","target":{"channel_id":"C9","thread_ts":"2.0"},"message_text":"x","context":"c","risk_reason":"r"}')
eq "a real quest keeps its own quest_id" \
   "$(python3 -c "import json;print([i['quest_id'] for i in json.load(open('$APPROVALS'))['items'] if i['id']=='$RID'][0])")" \
   "q-real"
eq "...and is not given a source tag" \
   "$(python3 -c "import json;print([i.get('source','none') for i in json.load(open('$APPROVALS'))['items'] if i['id']=='$RID'][0])")" \
   "none"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "reaction approval routing: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
