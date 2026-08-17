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

# test-approval-lease.sh — an approved action can no longer be silently lost.
#
# The failure being fixed: if a worker died between `approval-helper.py start` and
# `done`, the item sat in `executing` where approval.py read it as clean, triage kept
# its watch, and the dashboard did not render it at all. A message the human had
# personally approved was lost with no surface anywhere.

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
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/checkers" "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/ops" "$REPO/state/quests/active/q1"
cp "$SCRIPT_DIR/ledger/approval-helper.py" "$REPO/yaas-triage/ledger/"
cp "$SCRIPT_DIR/ledger/add-watch.py" "$REPO/yaas-triage/ledger/"
cp "$SCRIPT_DIR/tick_state.py" "$REPO/yaas-triage/"
cp "$SCRIPT_DIR"/checkers/*.py "$SCRIPT_DIR"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
cp "$SCRIPT_DIR/approval_state.py" "$SCRIPT_DIR/approval_store.py" "$REPO/yaas-triage/"
printf '{"watches":[]}\n' > "$REPO/state/quests/active/q1/watch.json"
cd "$REPO" || exit 1

APPROVALS="$REPO/state/pending-approvals.json"
A() { python3 yaas-triage/ledger/approval-helper.py "$@"; }
# Outcome of the approval checker for $ID. The entry JSON is built in a variable so
# nested quoting cannot mangle it.
outcome() {
  local entry
  entry=$(jq -nc --arg id "$1" '{type:"approval",approval_id:$id}')
  python3 yaas-triage/checkers/approval.py "$entry" 2>/dev/null | jq -r '.outcome'
}
set_status() { python3 -c "
import json,sys
p='$APPROVALS'; d=json.load(open(p)); d['items'][0]['status']=sys.argv[1]; json.dump(d,open(p,'w'))" "$1"; }
expire_lease() { python3 -c "
import json
from datetime import datetime, timezone, timedelta
p='$APPROVALS'; d=json.load(open(p))
d['items'][0]['lease_expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
json.dump(d,open(p,'w'))"; }

echo "── the approval lifecycle ─────────────────────────────────────────────────"
ID=$(A write '{"quest_id":"q1","quest_title":"Q","action_type":"slack_message","target":{"channel_id":"C1","thread_ts":null},"message_text":"hi","context":"c","risk_reason":"r"}')
[ -n "$ID" ] && ok "write returns an id" || bad "write returned nothing"
eq "write arms the tracking watch"        "$(jq -r '[.watches[] | select(.type=="approval")] | length' state/quests/active/q1/watch.json)" "1"
eq "pending_review does not dispatch"     "$(outcome "$ID")" "clean"
eq "pending_review cannot be claimed"     "$(A start "$ID")" "skip:pending_review"
eq "a rejected claim leaves review pending" "$(jq -r '.items[0].status' "$APPROVALS")" "pending_review"
set_status reviewed
eq "reviewed dispatches"                  "$(outcome "$ID")" "dirty"

echo
echo "── the lease ──────────────────────────────────────────────────────────────"
A start "$ID" >/dev/null
eq "start records a lease"                "$(jq -r '.items[0].lease_expires_at != null' "$APPROVALS")" "true"
eq "a LIVE claim does not re-dispatch"    "$(outcome "$ID")" "clean"
expire_lease
eq "an EXPIRED claim re-dispatches"       "$(outcome "$ID")" "dirty"
python3 yaas-triage/checkers/approval.py "$(jq -nc --arg id "$ID" '{type:"approval",approval_id:$id}')" 2>/dev/null \
  | jq -r '.preview' | grep -q "outcome unknown" \
  && ok "the re-dispatch says the outcome is UNKNOWN, so the worker reconciles rather than resending" \
  || bad "the expired-lease preview does not flag the outcome as unknown"
python3 -c "
import json
p='$APPROVALS'; d=json.load(open(p)); d['items'][0]['needs_reconcile']=True; json.dump(d,open(p,'w'))"
A done "$ID" 1234.5678 >/dev/null
eq "done closes it for good"              "$(outcome "$ID")" "clean"
eq "done clears the reconciliation flag"  "$(jq -r '.items[0] | has("needs_reconcile")' "$APPROVALS")" "false"
eq "and it stays closed even though the lease is long past" "$(outcome "$ID")" "clean"

grep -q 'Reclaimed item (`needs_reconcile: true`)' "$SCRIPT_DIR/skills/yaas-quest-dispatch/SKILL.md" \
  && ok "the worker contract requires reclaimed items to reconcile before execution" \
  || bad "the worker contract does not consume needs_reconcile"

echo
echo "-- reclaimed items close directly from reviewed ----------------------------"
IDR=$(A write '{"quest_id":"q1","quest_title":"Q","action_type":"slack_message","target":{"channel_id":"C4","thread_ts":null},"message_text":"reconcile","context":"c","risk_reason":"r"}')
jq --arg id "$IDR" '(.items[] | select(.id==$id)) += {status:"reviewed",needs_reconcile:true}' \
  "$APPROVALS" > "$APPROVALS.tmp" && mv "$APPROVALS.tmp" "$APPROVALS"
eq "reconciled external outcome closes without another claim" "$(A done "$IDR" 9999.1)" "ok"
eq "direct reconciliation records execution" \
   "$(jq -r --arg id "$IDR" '.items[] | select(.id==$id) | .status' "$APPROVALS")" "executed"

IDM=$(A enqueue-instruction '{"quest_id":"q1","quest_title":"Q","instruction":"uncertain work"}' | jq -r '.approval_id')
jq --arg id "$IDM" '(.items[] | select(.id==$id)) += {needs_reconcile:true}' \
  "$APPROVALS" > "$APPROVALS.tmp" && mv "$APPROVALS.tmp" "$APPROVALS"
eq "uncertain reclaimed manual work can be abandoned before another claim" \
   "$(A abandon "$IDM" "outcome cannot be proven")" "ok"
eq "direct abandon is terminal" \
   "$(jq -r --arg id "$IDM" '.items[] | select(.id==$id) | .status' "$APPROVALS")" "cancelled"

IDP=$(A write '{"quest_id":"q1","quest_title":"Q","action_type":"slack_message","target":{"channel_id":"C5","thread_ts":null},"message_text":"pending","context":"c","risk_reason":"r"}')
eq "done on an ordinary pending item reports a skip, never false success" \
   "$(A done "$IDP")" "skip:pending_review"
eq "illegal done leaves the item pending" \
   "$(jq -r --arg id "$IDP" '.items[] | select(.id==$id) | .status' "$APPROVALS")" "pending_review"

echo
echo "── a malformed lease must not resurrect a live claim ─────────────────────"
ID2=$(A write '{"quest_id":"q1","quest_title":"Q","action_type":"slack_message","target":{"channel_id":"C2","thread_ts":null},"message_text":"x","context":"c","risk_reason":"r"}')
python3 -c "
import json
p='$APPROVALS'; d=json.load(open(p))
i=[x for x in d['items'] if x['id']=='$ID2'][0]
i['status']='executing'; i['lease_expires_at']='not-a-timestamp'
json.dump(d,open(p,'w'))"
eq "an unparseable lease is treated as live, not expired" "$(outcome "$ID2")" "clean"
python3 - "$SCRIPT_DIR/approval_state.py" <<'PY'
import importlib.util, sys
from datetime import datetime
spec = importlib.util.spec_from_file_location("approval_state", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
item = {"status": "executing", "lease_expires_at": "2020-01-01T00:00:00+00:00"}
assert m._lease_expired(item, datetime(2026, 1, 1, 0, 0, 0)) is True
assert "reclaim" in m.available_actions(item, datetime(2026, 1, 1, 0, 0, 0))
PY
if [ "$?" -eq 0 ]; then
  ok "a naive now still detects an expired aware lease"
else
  bad "a naive now failed to detect an expired aware lease"
fi

echo
echo "── the dashboard shows everything that is not finished ───────────────────"
# The bug was an ALLOWLIST: executing and reviewed were invisible by construction.
grep -q 'TERMINAL_APPROVAL_STATUSES = ("executed", "cancelled")' "$SCRIPT_DIR/ops/dashboard-server.py" \
  && ok "terminal statuses are named in one place" || bad "no TERMINAL_APPROVAL_STATUSES constant"
if grep -q 'status") in ("pending_review", "needs_reply")' "$SCRIPT_DIR/ops/dashboard-server.py"; then
  bad "an allowlist filter survives; executing/reviewed would still be invisible"
else
  ok "no allowlist filters remain — anything non-terminal renders"
fi
eq "every filter site uses the shared constant" \
   "$(grep -c 'TERMINAL_APPROVAL_STATUSES' "$SCRIPT_DIR/ops/dashboard-server.py")" "6"

echo
echo "── an approval whose watch could not be armed is not silent ───────────────"
# triage cannot see an unarmed approval at all, so the item itself must carry the flag.
rm -rf state/quests/active/q1
ID3=$(A write '{"quest_id":"q1","quest_title":"Q","action_type":"slack_message","target":{"channel_id":"C3","thread_ts":null},"message_text":"y","context":"c","risk_reason":"r"}' 2>/dev/null)
if [ -n "$ID3" ]; then
  ok "the approval is still written when arming is impossible (losing it would be worse)"
  # NOT `.watch_armed // "absent"` — jq's `//` treats false as empty, so a correctly
  # flagged item would read as absent and this assertion would pass on broken code.
  ARMED=$(jq -r --arg id "$ID3" '.items[] | select(.id==$id) | if has("watch_armed") then (.watch_armed | tostring) else "absent" end' "$APPROVALS")
  eq "and it is flagged watch_armed=false for the dashboard" "$ARMED" "false"
else
  bad "the approval was dropped when its watch could not be armed"
fi

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval lease: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
