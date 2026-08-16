#!/bin/bash
# approval-edit-route.test.sh — the queued-draft edit endpoint is routed and durable.

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
cp "$HERE/../dashboard.html" "$REPO/dashboard.html"
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

cat > state/pending-approvals.json <<'JSON'
{
  "version": 1,
  "items": [
    {
      "id": "appr-reviewed",
      "quest_id": "q1",
      "quest_title": "Q1",
      "action_type": "slack_message",
      "status": "reviewed",
      "message_text": "before edit",
      "target": {"channel_id": "C1", "thread_ts": "1.0"}
    },
    {
      "id": "appr-pending",
      "quest_id": "q2",
      "quest_title": "Q2",
      "action_type": "slack_message",
      "status": "pending_review",
      "message_text": "still pending",
      "target": {"channel_id": "C2", "thread_ts": "2.0"}
    }
  ]
}
JSON

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
  python3 - "$PORT" "$1" "$2" <<'PY'
import http.client
import json
import sys

port, path, payload = int(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
host = f"127.0.0.1:{port}"

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/", headers={"Host": host})
resp = conn.getresponse()
cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
resp.read()
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
body = json.dumps(payload)
conn.request(
    "POST",
    path,
    body=body,
    headers={
        "Host": host,
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Content-Length": str(len(body.encode())),
    },
)
resp = conn.getresponse()
data = resp.read().decode()
print(resp.status)
print(data)
conn.close()
PY
}

echo "── edit route ─────────────────────────────────────────────────────────────"
RESP="$(http_post "/api/edit/appr-reviewed" '{"message_text":"after edit"}')"
STATUS="$(printf '%s\n' "$RESP" | sed -n '1p')"
BODY="$(printf '%s\n' "$RESP" | sed -n '2p')"
eq "POST /api/edit/<id> succeeds for a reviewed draft" "$STATUS" "200"
eq "the edited text is persisted" \
  "$(jq -r '.items[] | select(.id=="appr-reviewed") | .message_text' state/pending-approvals.json)" \
  "after edit"
eq "the endpoint marks the draft as human edited" \
  "$(jq -r '.items[] | select(.id=="appr-reviewed") | .human_edited' state/pending-approvals.json)" \
  "true"

RESP="$(http_post "/api/edit/appr-pending" '{"message_text":"should fail"}')"
STATUS="$(printf '%s\n' "$RESP" | sed -n '1p')"
BODY="$(printf '%s\n' "$RESP" | sed -n '2p')"
eq "pending_review is rejected by the current edit endpoint behavior" "$STATUS" "409"
eq "the rejection explains the legal source state" \
  "$(printf '%s' "$BODY" | jq -r '.error')" \
  "item is no longer in reviewed state"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval edit route: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
