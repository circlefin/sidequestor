#!/bin/bash
# approval-undo-reclaim.test.sh — undo and reclaim are distinct endpoints with distinct legality.

set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; wait "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ops" "$REPO/state"
cp "$HERE/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$HERE/tick_state.py" "$REPO/yaas-triage/"
cp "$HERE/tick_check.py" "$REPO/yaas-triage/"
cp "$HERE/approval_state.py" "$HERE/approval_store.py" "$REPO/yaas-triage/"
mkdir -p "$REPO/yaas-triage/checkers"
cp "$HERE"/checkers/*.py "$HERE"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
# "/" serves v2 now, so the shell needs both files present to issue a cookie.
cp "$HERE/../dashboard.html" "$REPO/dashboard.html"
cp "$HERE/../dashboard-v2.html" "$REPO/dashboard-v2.html"
cd "$REPO" || exit 1

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (got '$2', want '$3')"; }

PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

python3 - <<'PY' > state/pending-approvals.json
import json
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
data = {
    "version": 1,
    "items": [
        {
            "id": "undo-reviewed",
            "quest_id": "q1",
            "quest_title": "Q1",
            "action_type": "slack_message",
            "status": "reviewed",
            "message_text": "approved",
            "reviewed_at": now.isoformat(),
            "target": {"channel_id": "C1", "thread_ts": "1.0"},
        },
        {
            "id": "undo-cancelled",
            "quest_id": "q2",
            "quest_title": "Q2",
            "action_type": "slack_message",
            "status": "cancelled",
            "message_text": "cancelled",
            "cancelled_at": now.isoformat(),
            "target": {"channel_id": "C2", "thread_ts": "2.0"},
        },
        {
            "id": "undo-pending",
            "quest_id": "q3",
            "quest_title": "Q3",
            "action_type": "slack_message",
            "status": "pending_review",
            "message_text": "pending",
            "target": {"channel_id": "C3", "thread_ts": "3.0"},
        },
        {
            "id": "undo-needs-reply",
            "quest_id": "q4",
            "quest_title": "Q4",
            "action_type": "slack_message",
            "status": "needs_reply",
            "message_text": "needs reply",
            "target": {"channel_id": "C4", "thread_ts": "4.0"},
        },
        {
            "id": "undo-executing",
            "quest_id": "q5",
            "quest_title": "Q5",
            "action_type": "slack_message",
            "status": "executing",
            "message_text": "executing",
            "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            "target": {"channel_id": "C5", "thread_ts": "5.0"},
        },
        {
            "id": "undo-executed",
            "quest_id": "q6",
            "quest_title": "Q6",
            "action_type": "slack_message",
            "status": "executed",
            "message_text": "done",
            "target": {"channel_id": "C6", "thread_ts": "6.0"},
        },
        {
            "id": "reclaim-expired",
            "quest_id": "q7",
            "quest_title": "Q7",
            "action_type": "slack_message",
            "status": "executing",
            "message_text": "stuck",
            "lease_expires_at": (now - timedelta(minutes=5)).isoformat(),
            "target": {"channel_id": "C7", "thread_ts": "7.0"},
        },
        {
            "id": "reclaim-live",
            "quest_id": "q8",
            "quest_title": "Q8",
            "action_type": "slack_message",
            "status": "executing",
            "message_text": "live",
            "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            "target": {"channel_id": "C8", "thread_ts": "8.0"},
        },
        {
            "id": "reclaim-reviewed",
            "quest_id": "q9",
            "quest_title": "Q9",
            "action_type": "slack_message",
            "status": "reviewed",
            "message_text": "reviewed",
            "reviewed_at": now.isoformat(),
            "target": {"channel_id": "C9", "thread_ts": "9.0"},
        },
        {
            "id": "reclaim-bad-lease",
            "quest_id": "q10",
            "quest_title": "Q10",
            "action_type": "slack_message",
            "status": "executing",
            "message_text": "bad",
            "lease_expires_at": "not-a-timestamp",
            "target": {"channel_id": "C10", "thread_ts": "10.0"},
        },
    ],
}
print(json.dumps(data, indent=2))
PY

python3 yaas-triage/ops/dashboard-server.py "$PORT" >/dev/null 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 100); do
  if python3 - "$PORT" <<'PY' >/dev/null 2>&1
import http.client
import sys
conn = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=0.2)
conn.request("GET", "/", headers={"Host": f"127.0.0.1:{sys.argv[1]}"})
resp = conn.getresponse()
sys.exit(0 if resp.status == 200 else 1)
PY
  then
    break
  fi
  sleep 0.05
done

http_post() {
  python3 - "$PORT" "$1" <<'PY'
import http.client
import sys

port, path = int(sys.argv[1]), sys.argv[2]
host = f"127.0.0.1:{port}"

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/", headers={"Host": host})
resp = conn.getresponse()
cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
resp.read()
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("POST", path, body="{}", headers={
    "Host": host,
    "Cookie": cookie,
    "Content-Type": "application/json",
    "Content-Length": "2",
})
resp = conn.getresponse()
data = resp.read().decode()
print(resp.status)
print(data)
conn.close()
PY
}

echo "── undo ───────────────────────────────────────────────────────────────────"
RESP="$(http_post "/api/undo/undo-reviewed")"
eq "undo is legal from reviewed" "$(printf '%s\n' "$RESP" | sed -n '1p')" "200"
eq "undo reviewed restores pending_review" \
  "$(jq -r '.items[] | select(.id=="undo-reviewed") | .status' state/pending-approvals.json)" \
  "pending_review"
eq "undo reviewed clears reviewed_at" \
  "$(jq -r '.items[] | select(.id=="undo-reviewed") | (.reviewed_at == null)' state/pending-approvals.json)" \
  "true"

RESP="$(http_post "/api/undo/undo-cancelled")"
eq "undo is legal from cancelled" "$(printf '%s\n' "$RESP" | sed -n '1p')" "200"
eq "undo cancelled restores pending_review" \
  "$(jq -r '.items[] | select(.id=="undo-cancelled") | .status' state/pending-approvals.json)" \
  "pending_review"
eq "undo cancelled clears cancelled_at" \
  "$(jq -r '.items[] | select(.id=="undo-cancelled") | (.cancelled_at == null)' state/pending-approvals.json)" \
  "true"

for id in undo-pending undo-needs-reply undo-executing undo-executed; do
  RESP="$(http_post "/api/undo/$id")"
  eq "undo rejects $id" "$(printf '%s\n' "$RESP" | sed -n '1p')" "409"
done

echo
echo "── reclaim ────────────────────────────────────────────────────────────────"
RESP="$(http_post "/api/reclaim/reclaim-expired")"
eq "reclaim is legal from executing with an expired lease" "$(printf '%s\n' "$RESP" | sed -n '1p')" "200"
eq "reclaim restores pending_review" \
  "$(jq -r '.items[] | select(.id=="reclaim-expired") | .status' state/pending-approvals.json)" \
  "pending_review"
eq "reclaim clears the lease" \
  "$(jq -r '.items[] | select(.id=="reclaim-expired") | (.lease_expires_at == null)' state/pending-approvals.json)" \
  "true"
eq "reclaim flags needs_reconcile" \
  "$(jq -r '.items[] | select(.id=="reclaim-expired") | .needs_reconcile' state/pending-approvals.json)" \
  "true"

RESP="$(http_post "/api/review/reclaim-expired")"
eq "a reclaimed item can return to reviewed" "$(printf '%s\n' "$RESP" | sed -n '1p')" "200"
eq "re-review preserves needs_reconcile for the worker" \
  "$(jq -r '.items[] | select(.id=="reclaim-expired") | .needs_reconcile' state/pending-approvals.json)" \
  "true"

for id in reclaim-live reclaim-reviewed reclaim-bad-lease undo-pending; do
  RESP="$(http_post "/api/reclaim/$id")"
  eq "reclaim rejects $id" "$(printf '%s\n' "$RESP" | sed -n '1p')" "409"
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval undo/reclaim: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
