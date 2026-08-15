#!/bin/bash
# dashboard-routes.test.sh — every routed POST action has a live endpoint contract.

set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; wait "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ops" "$REPO/yaas-triage/ledger" \
  "$REPO/yaas-triage/skills/yaas-quest-creation" "$REPO/state/quests/active/q-prompt"
cp "$HERE/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$HERE/tick_state.py" "$REPO/yaas-triage/"
cp "$HERE/tick_check.py" "$REPO/yaas-triage/"
cp "$HERE/reaction_config.py" "$REPO/yaas-triage/"
cp "$HERE/approval_state.py" "$HERE/approval_store.py" "$REPO/yaas-triage/"
cp "$HERE/ledger/approval-helper.py" "$HERE/ledger/add-watch.py" "$REPO/yaas-triage/ledger/"
cp "$HERE/skills/yaas-quest-creation/new-quest.py" "$REPO/yaas-triage/skills/yaas-quest-creation/"
mkdir -p "$REPO/yaas-triage/checkers"
cp "$HERE"/checkers/*.py "$HERE"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
cp "$HERE/../dashboard.html" "$REPO/dashboard.html"
printf '%s\n' '{"id":"q-prompt","title":"Prompt quest"}' > "$REPO/state/quests/active/q-prompt/meta.json"
printf '%s\n' '{"watches":[{"type":"slack_thread","channel_id":"C1","thread_ts":"1.0","last_checked_ts":"1","reason":"route fixture"}]}' > "$REPO/state/quests/active/q-prompt/watch.json"
cd "$REPO" || exit 1

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
print(json.dumps({
    "version": 1,
    "items": [
        {
            "id": "route-review",
            "quest_id": "q1",
            "quest_title": "Q1",
            "action_type": "slack_message",
            "status": "pending_review",
            "message_text": "review me",
            "target": {"channel_id": "C1", "thread_ts": "1.0"},
        },
        {
            "id": "route-revise",
            "quest_id": "q2",
            "quest_title": "Q2",
            "action_type": "slack_message",
            "status": "pending_review",
            "message_text": "revise me",
            "target": {"channel_id": "C2", "thread_ts": "2.0"},
        },
        {
            "id": "route-edit",
            "quest_id": "q3",
            "quest_title": "Q3",
            "action_type": "slack_message",
            "status": "reviewed",
            "message_text": "edit me",
            "context": "Answer the deployment question in the release thread",
            "risk_reason": "allow_send is false",
            "reviewed_at": now.isoformat(),
            "target": {"channel_id": "C3", "thread_ts": "3.0"},
        },
        {
            "id": "route-cancel",
            "quest_id": "q4",
            "quest_title": "Q4",
            "action_type": "slack_message",
            "status": "pending_review",
            "message_text": "cancel me",
            "target": {"channel_id": "C4", "thread_ts": "4.0"},
        },
        {
            "id": "route-undo",
            "quest_id": "q5",
            "quest_title": "Q5",
            "action_type": "slack_message",
            "status": "reviewed",
            "message_text": "undo me",
            "reviewed_at": now.isoformat(),
            "target": {"channel_id": "C5", "thread_ts": "5.0"},
        },
        {
            "id": "route-reclaim",
            "quest_id": "q6",
            "quest_title": "Q6",
            "action_type": "slack_message",
            "status": "executing",
            "message_text": "reclaim me",
            "lease_expires_at": (now - timedelta(minutes=5)).isoformat(),
            "target": {"channel_id": "C6", "thread_ts": "6.0"},
        },
    ],
}, indent=2))
PY

YAAS_REACTION_PROCESS_EMOJI=route_process \
YAAS_REACTION_DRAFT_EMOJI=route_draft \
YAAS_REACTION_SAVE_EMOJI=route_save \
YAAS_REACTION_ADOPT_EMOJI=route_adopt \
YAAS_REACTION_LOADING_EMOJI=route_loading \
YAAS_REACTION_DONE_EMOJI=route_done \
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
import importlib.util
import json
import sys
from pathlib import Path

port = int(sys.argv[1])
host = f"127.0.0.1:{port}"

sys.argv = ["dashboard-server.py", str(port)]
spec = importlib.util.spec_from_file_location("dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
route_actions = sorted(set(module.approval_state.HTTP_ACTIONS) | {"prompt"})

html = open("dashboard.html").read()
client_actions = set()
client_actions.update(["review", "revise", "cancel", "reclaim"])
if "(a.available_actions||[]).includes('undo')" not in html:
    raise AssertionError("client offers Undo without checking the server's available actions")
if 'class="queued-save"' in html and '/api/edit/' in html:
    client_actions.add("edit")
if 'class="ut-undo"' in html and '/api/undo/' in html:
    client_actions.add("undo")
if 'data-action="run-prompt"' in html and '/api/prompt/' in html:
    client_actions.add("prompt")

fixtures = {
    "review": ("/api/review/route-review", {"message_text": "approved copy"}),
    "revise": ("/api/revise/route-revise", {"review_note": "make it shorter"}),
    "edit": ("/api/edit/route-edit", {"message_text": "edited copy"}),
    "cancel": ("/api/cancel/route-cancel", {}),
    "undo": ("/api/undo/route-undo", {}),
    "reclaim": ("/api/reclaim/route-reclaim", {}),
    "prompt": ("/api/prompt/q-prompt", {"instruction": "do the thing"}),
}

missing = sorted(set(route_actions) - set(fixtures))
extra = sorted(set(fixtures) - set(route_actions))
assert not missing, f"routed actions without contract fixture: {missing}"
assert not extra, f"contract fixtures for unrouted actions: {extra}"
assert client_actions.issubset(set(route_actions)), (
    f"client POST actions not routed by do_POST: {sorted(client_actions - set(route_actions))}"
)
worker_only = set(module.approval_state.WORKER_ONLY_ACTIONS)
assert not (worker_only & set(route_actions)), (
    f"worker-only actions became HTTP-routable: {sorted(worker_only & set(route_actions))}"
)

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/", headers={"Host": host})
resp = conn.getresponse()
cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
resp.read()
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/dashboard", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
dashboard_body = resp.read().decode()
assert resp.status == 200, f"dashboard returned {resp.status}: {dashboard_body}"
dashboard = json.loads(dashboard_body)
config_items = {
    item["key"]: item
    for group in dashboard["config"]["groups"]
    for item in group["items"]
}
expected_defaults = {
    "YAAS_TRIAGE_MAX_PARALLEL": "3",
    "YAAS_TICK_DISPATCH_BUDGET": "3600",
    "YAAS_MIN_DISPATCH_SLICE": "300",
}
for key, expected in expected_defaults.items():
    assert config_items[key]["default"] == expected, (key, config_items[key])
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/control", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
control_body = resp.read().decode()
assert resp.status == 200, f"control returned {resp.status}: {control_body}"
control = json.loads(control_body)
assert control["reaction_emojis"] == {
    "roles": {
        "process": "route_process", "draft": "route_draft", "save": "route_save",
        "adopt": "route_adopt", "loading": "route_loading", "done": "route_done",
    },
    "error": None,
}, control["reaction_emojis"]
queued = {item["id"]: item for item in control["queued"]}
assert queued["route-edit"]["quest_title"] == "Q3", queued["route-edit"]
assert queued["route-edit"]["context"] == "Answer the deployment question in the release thread", queued["route-edit"]
assert queued["route-edit"]["message_text"] == "edit me", queued["route-edit"]
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/quest/q-prompt", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
detail_body = resp.read().decode()
assert resp.status == 200, f"quest detail returned {resp.status}: {detail_body}"
detail = json.loads(detail_body)
assert len(detail["open_items"]["threads"]) == 1, detail
assert detail["open_items"]["threads"][0]["type"] == "slack_thread", detail
conn.close()

manifest_path = Path("yaas-triage/checkers/slack_thread.watch.json")
valid_manifest = manifest_path.read_text()
manifest_path.write_text("{bad json\n")
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/quest/q-prompt", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
degraded_body = resp.read().decode()
assert resp.status == 200, f"malformed manifest took down quest route: {resp.status}: {degraded_body}"
degraded = json.loads(degraded_body)
assert degraded["open_items"]["threads"] == [], degraded
assert "slack_thread.watch.json" in degraded["open_items"]["registry_error"], degraded
conn.close()
manifest_path.write_text(valid_manifest)

for action in route_actions:
    path, payload = fixtures[action]
    body = json.dumps(payload)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("POST", path, body=body, headers={
        "Host": host,
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Content-Length": str(len(body.encode())),
    })
    resp = conn.getresponse()
    resp_body = resp.read().decode()
    assert resp.status != 404, f"{action} returned 404 for {path}: {resp_body}"
    assert resp.status in (200, 202), f"{action} returned {resp.status} for {path}: {resp_body}"
    conn.close()

def post(path, payload):
    body = json.dumps(payload)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("POST", path, body=body, headers={
        "Host": host,
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Content-Length": str(len(body.encode())),
    })
    resp = conn.getresponse()
    response_body = resp.read().decode()
    conn.close()
    return resp.status, response_body

status, response_body = post("/api/quests", {
    "title": "Review customer feedback",
    "prompt": "Review the latest customer feedback and prepare a draft response.",
    "priority": "high",
})
assert status == 201, (status, response_body)
created = json.loads(response_body)
assert created["allow_send"] is False, created
quest_dir = Path("state/quests/active") / created["quest_id"]
meta = json.loads((quest_dir / "meta.json").read_text())
watch = json.loads((quest_dir / "watch.json").read_text())["watches"][0]
assert meta["allow_send"] is False, meta
assert watch["type"] == "schedule", watch
assert float(watch["next_fire_ts"]) > float(watch["last_checked_ts"]), watch
assert "Review the latest customer feedback" in (quest_dir / "context.md").read_text()

for payload, expected_error in (
    ({"prompt": 7}, "prompt must be a string"),
    ({"prompt": "work", "priority": "urgent"}, "priority must be high, normal, or low"),
):
    status, response_body = post("/api/quests", payload)
    assert status == 400, (status, response_body)
    assert json.loads(response_body)["error"] == expected_error, response_body
PY
