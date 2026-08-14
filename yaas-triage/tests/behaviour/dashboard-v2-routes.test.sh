#!/bin/bash
# dashboard-v2-routes.test.sh -- v1 and v2 remain independently reachable.

set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; wait "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
REPO="$TMP/repo"

mkdir -p "$REPO/yaas-triage/ops" "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/checkers" "$REPO/yaas-triage/skills/yaas-quest-creation" "$REPO/state/quests/active/q-control"
cp "$HERE/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$HERE/tick_state.py" "$HERE/tick_check.py" "$HERE/approval_state.py" "$HERE/approval_store.py" "$REPO/yaas-triage/"
cp "$HERE/ledger/approval-helper.py" "$REPO/yaas-triage/ledger/"
cp "$HERE/skills/yaas-quest-creation/new-quest.py" "$REPO/yaas-triage/skills/yaas-quest-creation/"
cp "$HERE"/checkers/*.py "$HERE"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
cp "$HERE/../dashboard.html" "$HERE/../dashboard-v2.html" "$REPO/"
printf '%s\n' '{"id":"q-control","title":"Control fixture","status":"active","priority":"high"}' > "$REPO/state/quests/active/q-control/meta.json"
printf '%s\n' '{"watches":[]}' > "$REPO/state/quests/active/q-control/watch.json"
printf '%s\n' '# Control fixture' '' '## Current state' '' '- **Watch** the source thread' '- Read `context.md` as Markdown' > "$REPO/state/quests/active/q-control/context.md"
printf '%s\n' '{"ts":"2026-08-15T12:00:00+00:00","event":"message_sent","note":"Sent status update"}' > "$REPO/state/quests/active/q-control/timeline.ndjson"
printf '%s\n' '{"version":1,"items":[{"id":"appr-review","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:00:00+00:00","status":"pending_review","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"1.0"},"message_text":"Draft needing review","context":"Test approval","risk_reason":"Manual review required"},{"id":"appr-revise","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:01:00+00:00","status":"pending_review","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"2.0"},"message_text":"Draft needing changes"},{"id":"appr-cancel","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:02:00+00:00","status":"pending_review","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"3.0"},"message_text":"Draft to cancel"},{"id":"appr-edit","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:03:00+00:00","status":"reviewed","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"4.0"},"message_text":"Approved original","reviewed_at":"2026-08-15T12:04:00+00:00"},{"id":"appr-undo","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:05:00+00:00","status":"reviewed","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"5.0"},"message_text":"Approved to undo","reviewed_at":"2026-08-15T12:06:00+00:00"},{"id":"appr-reclaim","quest_id":"q-control","quest_title":"Control fixture","created_at":"2026-08-15T12:07:00+00:00","status":"executing","action_type":"slack_message","target":{"channel_id":"D-control","thread_ts":"6.0"},"message_text":"Stalled send","executing_at":"2026-08-15T12:08:00+00:00","lease_expires_at":"2020-01-01T00:00:00+00:00"}]}' > "$REPO/state/pending-approvals.json"
cd "$REPO"

PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
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

python3 - "$PORT" <<'PY'
import http.client
import json
import sys

port = int(sys.argv[1])
host = f"127.0.0.1:{port}"

def request(path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = cookie
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode()
    cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
    conn.close()
    return resp.status, body, cookie

def post(path, payload, cookie):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    body = json.dumps(payload)
    conn.request("POST", path, body=body, headers={
        "Host": host,
        "Cookie": cookie,
        "Content-Type": "application/json",
    })
    resp = conn.getresponse()
    result = resp.read().decode()
    conn.close()
    return resp.status, result

for path, marker in (("/", "Sidequestor"), ("/v2", "Quest Control")):
    status, body, cookie = request(path)
    assert status == 200, (path, status, body)
    assert marker in body, (path, marker)

assert "Create quest" in body, body
assert 'id="quest-focus"' in body, body
assert 'id="approval-history"' in body, body
assert "Prompt this quest to do something" in body, body
assert 'id="review-dialog"' in body, body

status, body, _ = request("/api/control", cookie)
assert status == 200, (status, body)
control = json.loads(body)
assert control["api_version"] == 1, control
assert control["capabilities"]["control_snapshot"] is True, control
assert control["quests"][0]["id"] == "q-control", control
sent = next(item for item in control["activity"] if item["event"] == "message_sent")
assert sent["actor"] == "agent", sent
assert sent["action"] == "sent message", sent
assert any(item["approval"]["id"] == "appr-review" for item in control["attention"]), control
assert "history" in control, control

status, body = post("/api/review/appr-review", {}, cookie)
assert status == 200, (status, body)
assert json.loads(body)["status"] == "reviewed", body
status, body, _ = request("/state/pending-approvals.json", cookie)
assert status == 200, (status, body)
assert json.loads(body)["items"][0]["status"] == "reviewed", body
status, body, _ = request("/api/control", cookie)
assert status == 200, (status, body)
queued = json.loads(body)["queued"]
assert queued[0]["id"] == "appr-review", queued
assert queued[0]["target"]["channel_id"] == "D-control", queued
assert queued[0]["risk_reason"] == "Manual review required", queued
assert "edit" in queued[0]["available_actions"], queued

status, body = post("/api/revise/appr-revise", {"review_note": "Make the answer shorter."}, cookie)
assert status == 200 and json.loads(body)["status"] == "needs_reply", (status, body)
status, body, _ = request("/api/control", cookie)
control = json.loads(body)
revision = next(item["approval"] for item in control["attention"] if item.get("approval", {}).get("id") == "appr-revise")
assert revision["review_note"] == "Make the answer shorter.", revision
assert set(revision["available_actions"]) == {"review", "revise", "cancel"}, revision

status, body = post("/api/edit/appr-edit", {"message_text": "Approved with an inline edit"}, cookie)
assert status == 200 and json.loads(body)["status"] == "reviewed", (status, body)
status, body, _ = request("/api/control", cookie)
edited = next(item for item in json.loads(body)["queued"] if item["id"] == "appr-edit")
assert edited["message_text"] == "Approved with an inline edit", edited
assert edited["human_edited"] is True, edited

status, body = post("/api/cancel/appr-cancel", {}, cookie)
assert status == 200 and json.loads(body)["status"] == "cancelled", (status, body)
status, body, _ = request("/api/control", cookie)
cancelled = next(item for item in json.loads(body)["history"]["approvals"] if item["id"] == "appr-cancel")
assert cancelled["status"] == "cancelled" and "undo" in cancelled["available_actions"], cancelled
status, body = post("/api/undo/appr-cancel", {}, cookie)
assert status == 200 and json.loads(body)["status"] == "pending_review", (status, body)

status, body = post("/api/undo/appr-undo", {}, cookie)
assert status == 200 and json.loads(body)["status"] == "pending_review", (status, body)
status, body = post("/api/reclaim/appr-reclaim", {}, cookie)
assert status == 200 and json.loads(body)["status"] == "pending_review", (status, body)
status, body, _ = request("/api/control", cookie)
reclaimed = next(item["approval"] for item in json.loads(body)["attention"] if item.get("approval", {}).get("id") == "appr-reclaim")
assert reclaimed["stalled"] is False and reclaimed["lease_expires_at"] is None, reclaimed

status, body, _ = request("/api/quest/q-control", cookie)
assert status == 200, (status, body)
assert json.loads(body)["context_md"].startswith("# Control fixture"), body

status, body = post("/api/quests", {
    "title": "Review customer feedback",
    "prompt": "Review the latest customer feedback and prepare a draft response.",
    "priority": "high",
}, cookie)
assert status == 201, (status, body)
created = json.loads(body)
assert created["allow_send"] is False, created
quest_id = created["quest_id"]

status, body, _ = request("/api/quest/" + quest_id, cookie)
assert status == 200, (status, body)
detail = json.loads(body)
assert detail["meta"]["allow_send"] is False, detail
assert detail["watches"][0]["type"] == "schedule", detail
assert float(detail["watches"][0]["next_fire_ts"]) > float(detail["watches"][0]["last_checked_ts"]), detail
assert "Review the latest customer feedback" in detail["context_md"], detail
PY
