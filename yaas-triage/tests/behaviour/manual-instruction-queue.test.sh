#!/bin/bash
# A dashboard instruction must survive the global worker lock, remain distinct
# from every other button press, and stop dispatching after an uncertain outcome.

set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${LOCK_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
Q="$REPO/state/quests/active/q1"
mkdir -p "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/checkers" \
  "$REPO/yaas-triage/ops" "$Q" "$REPO/logs"
cp "$HERE/ledger/approval-helper.py" "$REPO/yaas-triage/ledger/"
cp "$HERE/ledger/add-watch.py" "$REPO/yaas-triage/ledger/"
cp "$HERE/checkers/approval.py" "$HERE/checkers/result.py" "$REPO/yaas-triage/checkers/"
cp "$HERE/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$HERE/../dashboard.html" "$REPO/dashboard.html"
printf '%s\n' '{"watches":[]}' > "$Q/watch.json"
printf '%s\n' '{"id":"q1","title":"Q one"}' > "$Q/meta.json"
cd "$REPO" || exit 1

PASS=0; FAIL=0
ok()  { echo "ok - $1"; PASS=$((PASS+1)); }
bad() { echo "not ok - $1"; FAIL=$((FAIL+1)); }
eq()  { [ "$2" = "$3" ] && ok "$1" || { bad "$1 (got '$2', want '$3')"; }; }
A() { python3 yaas-triage/ledger/approval-helper.py "$@"; }
outcome() {
  python3 yaas-triage/checkers/approval.py \
    "$(printf '{\"type\":\"approval\",\"approval_id\":\"%s\",\"last_checked_ts\":\"0\"}' "$1")" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["outcome"])'
}

echo "-- queueing ignores the global triage lock --"
python3 -c 'import fcntl,time; f=open("logs/triage.lock","a"); fcntl.flock(f,fcntl.LOCK_EX); open("logs/test-lock-ready","w").close(); time.sleep(20)' &
LOCK_PID=$!
for _ in $(seq 1 50); do [ -f logs/test-lock-ready ] && break; sleep 0.02; done

R1=$(A enqueue-instruction '{"quest_id":"q1","quest_title":"Q one","instruction":"retire it"}')
R2=$(A enqueue-instruction '{"quest_id":"q1","quest_title":"Q one","instruction":"retire it"}')
ID1=$(printf '%s' "$R1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_id"])')
ID2=$(printf '%s' "$R2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_id"])')
[ -n "$ID1" ] && [ "$ID1" != "$ID2" ] && ok "identical submissions receive distinct ids" || bad "ids were missing or deduplicated"
eq "both instructions are durable" "$(jq '[.items[] | select(.action_type=="manual_instruction")] | length' state/pending-approvals.json)" "2"
eq "manual instructions begin reviewed" "$(jq -r --arg id "$ID1" '.items[] | select(.id==$id) | .status' state/pending-approvals.json)" "reviewed"
eq "no watch file is touched while another tick owns the lock" "$(jq '[.watches[] | select(.type=="approval")] | length' "$Q/watch.json")" "0"
kill "$LOCK_PID" 2>/dev/null || true
wait "$LOCK_PID" 2>/dev/null || true
LOCK_PID=""
eq "the next locked tick arms pending instructions" "$(A arm-pending-instructions | jq -r '.armed')" "2"
eq "each instruction gets its own approval watch" "$(jq '[.watches[] | select(.type=="approval")] | length' "$Q/watch.json")" "2"
eq "approval watches receive ids immediately" "$(jq '[.watches[] | select(.type=="approval" and (.watch_id|startswith("watch-")))] | length' "$Q/watch.json")" "2"
eq "a reviewed instruction is dirty" "$(outcome "$ID1")" "dirty"

echo "-- ordinary Slack dedup remains unchanged --"
S1=$(A write '{"quest_id":"q1","quest_title":"Q one","action_type":"slack_message","target":{"channel_id":"C1","thread_ts":"1.0"},"message_text":"hi","context":"c","risk_reason":"r"}')
S2=$(A write '{"quest_id":"q1","quest_title":"Q one","action_type":"slack_message","target":{"channel_id":"C1","thread_ts":"1.0"},"message_text":"again","context":"c","risk_reason":"r"}')
[ -n "$S1" ] && [ -z "$S2" ] && ok "existing Slack target dedup is unchanged" || bad "Slack dedup changed"

echo "-- uncertain manual work terminates instead of looping --"
eq "claim succeeds" "$(YAAS_APPROVAL_LEASE_MIN=-1 python3 yaas-triage/ledger/approval-helper.py start "$ID1")" "ok"
eq "an expired manual lease re-surfaces as uncertain" "$(outcome "$ID1")" "dirty"
eq "abandon succeeds" "$(A abandon "$ID1" "outcome uncertain")" "ok"
eq "abandon is terminal" "$(jq -r --arg id "$ID1" '.items[] | select(.id==$id) | .status' state/pending-approvals.json)" "cancelled"
eq "a cancelled instruction is clean" "$(outcome "$ID1")" "clean"

echo "-- dashboard endpoint queues and does not render a review card --"
python3 - <<'PY'
import importlib.util
import sys

sys.argv = ["dashboard-server.py"]
spec = importlib.util.spec_from_file_location("dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Response:
    def _send_json(self, body, status=200):
        self.body = body
        self.status = status

response = Response()
module.Handler._handle_prompt(response, "q1", {"instruction": "another task"})
assert response.status == 202, (response.status, response.body)
assert response.body["queued"] is True
messages = module.build_messages()
queued_id = response.body["approval_id"]
assert queued_id in {x["id"] for x in messages["queued_items"]}
assert queued_id not in {x["id"] for x in messages["needs_you"]}
assert queued_id not in {x["id"] for x in messages["other_actions"]}
module.Handler._handle_review(response, queued_id, "cancel", {})
assert response.status == 200, (response.status, response.body)
assert response.body["status"] == "cancelled"
PY
[ "$?" -eq 0 ] && ok "dashboard queues and can cancel instructions outside the review carousel" \
  || bad "dashboard queue/cancel projection failed"

echo "-- arming failure is reported and terminal --"
BAD=$(A enqueue-instruction '{"quest_id":"missing","instruction":"do work"}' 2>/dev/null)
BAD_ID=$(printf '%s' "$BAD" | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_id"])')
ARM_RESULT=$(A arm-pending-instructions 2>/dev/null)
RC=$?
eq "unarmed tick reports failure" "$RC" "3"
eq "failed arm is counted" "$(printf '%s' "$ARM_RESULT" | jq -r '.cancelled')" "1"
eq "unarmed item is retained as cancelled audit" "$(jq -r --arg id "$BAD_ID" '.items[] | select(.id==$id) | .status' state/pending-approvals.json)" "cancelled"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
