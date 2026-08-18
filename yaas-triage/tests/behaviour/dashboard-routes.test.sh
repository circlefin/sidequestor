#!/bin/bash
# dashboard-routes.test.sh — every routed POST action has a live endpoint contract.

set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; wait "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ops" "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/assets" \
  "$REPO/yaas-triage/skills/yaas-quest-creation" "$REPO/state/quests/active/q-prompt" \
  "$REPO/state/briefs"
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
cp "$HERE/assets/sidequestor-mark.png" "$REPO/yaas-triage/assets/"
printf '%s\n' '{"id":"q-prompt","title":"Prompt quest"}' > "$REPO/state/quests/active/q-prompt/meta.json"
printf '%s\n' '{"watches":[{"type":"slack_thread","channel_id":"C1","thread_ts":"1.0","last_checked_ts":"1","reason":"route fixture"}]}' > "$REPO/state/quests/active/q-prompt/watch.json"
printf '%s\n' '# Route Brief' '' 'A canonical briefing fixture.' > "$REPO/state/briefs/2026-08-17_0830_morning.md"
sleep 0.05
# Briefing names are user-owned labels, not a schema. A plain name must be served too.
printf '%s\n' '# Stray Note' '' 'Not a briefing.' > "$REPO/state/briefs/notes.md"
sleep 0.05
printf '%s\n' '# Free-form Weekly' '' 'A freely named briefing.' > "$REPO/state/briefs/anything goes weekly!.md"
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

# THE BITING ASSERTION for the poll-cost fix. build_control() (polled every 2s) calls
# build_dashboard() with the default, so the default must not BUILD briefings: doing so
# re-reads every file in state/briefs/ on every poll and the control payload then throws
# them away. Checking the response body instead would prove nothing, because
# build_control() always dropped the key. This fails if the default flips back on.
assert module.build_dashboard()["briefs"] == [], "build_dashboard() builds briefings by default"
assert module.build_dashboard(include_briefs=True)["briefs"], "opt-in briefings are missing"

html = open("dashboard.html").read()
assert 'data-mode="briefings"' in html, "Briefings desktop mode is missing"
assert 'id="mode-menu-trigger"' in html, "compact mode menu is missing"
assert 'data-mode-option="briefings"' in html
# The mode nav collapses into the menu on width alone. Both halves are asserted
# because the <=620px rule must come last: it re-shows the wrapped full-width nav,
# and a reordering that let the menu rule win would hide the nav on phones.
assert '@media(max-width:840px){.navs{display:none}.mode-menu{display:block}}' in html
assert html.index('@media(max-width:840px){.navs{display:none}') < html.index('@media(max-width:620px){.navs{display:grid}'), \
    "the phone-width nav rule no longer overrides the mode-menu rule"
assert 'ResizeObserver' not in html, "mode nav is measuring again instead of using a media query"
assert "matchMedia('(max-width:840px)').addEventListener('change',closeModeMenu)" in html, \
    "nothing closes the mode menu when its breakpoint is crossed"
assert '.mode-option{display:block' in html and 'font:900 12px var(--sans)' in html
assert '.navs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}' in html
assert 'id="brief-picker-trigger"' in html, "compact briefing picker is missing"
assert '.brief-index-head,.brief-list{display:none}.brief-picker{display:block}' in html
assert 'data-brief="${esc(brief.file)}" aria-selected="${selected}"' in html
assert "fetch('/api/briefs'" in html, "Briefings UI is not wired to its API"
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
conn.request("GET", "/api/briefs", headers={"Host": host})
resp = conn.getresponse()
resp.read()
assert resp.status == 403, f"unauthenticated briefs returned {resp.status}"
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/", headers={"Host": host})
resp = conn.getresponse()
cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
resp.read()
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request(
    "GET",
    "/yaas-triage/assets/sidequestor-mark.png",
    headers={"Host": host},
)
resp = conn.getresponse()
logo_body = resp.read()
assert resp.status == 200, f"dashboard logo returned {resp.status}"
assert resp.getheader("Content-Type") == "image/png"
assert resp.getheader("Cache-Control") == "no-store"
assert logo_body.startswith(b"\x89PNG\r\n\x1a\n")
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/briefs", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
briefs_body = resp.read().decode()
assert resp.status == 200, f"briefs returned {resp.status}: {briefs_body}"
briefs = json.loads(briefs_body)["briefs"]
assert len(briefs) == 3, briefs
assert [b["file"] for b in briefs] == [
    "anything goes weekly!.md",
    "notes.md",
    "2026-08-17_0830_morning.md",
], briefs
by_file = {b["file"]: b for b in briefs}
assert by_file["notes.md"]["title"] == "Stray Note", by_file["notes.md"]
assert by_file["notes.md"]["type"] == "brief", by_file["notes.md"]
assert by_file["anything goes weekly!.md"]["type"] == "weekly", by_file["anything goes weekly!.md"]
assert "canonical briefing fixture" in by_file["2026-08-17_0830_morning.md"]["markdown"]
# `at` is the canonical file-creation time and carries an explicit UTC offset.
assert all(b["at"][-6] in "+-" for b in briefs), briefs
assert [b["at"] for b in briefs] == sorted((b["at"] for b in briefs), reverse=True), briefs
conn.close()

# /api/control is what the dashboard POLLS every 2s, and briefings are a
# read-on-demand surface. Asserting the briefs are ABSENT from the response body is
# not enough — build_control() always dropped the key, so that assertion passes even
# with the waste present. The biting assertion is at the builder: build_dashboard()
# must not BUILD briefings unless asked, because building them re-reads every file in
# state/briefs/ on every poll. That is checked in the builder block further down.
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/control", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
control_body = resp.read().decode()
assert resp.status == 200, f"control returned {resp.status}: {control_body}"
assert "canonical briefing fixture" not in control_body, "poll payload carries briefing markdown"
assert "briefs" not in json.loads(control_body), "control payload still has a briefs key"
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/dashboard", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
dashboard_body = resp.read().decode()
assert resp.status == 200, f"dashboard returned {resp.status}: {dashboard_body}"
dashboard = json.loads(dashboard_body)
# ...while /api/dashboard, whose payload contract still includes them, opts in.
assert dashboard["briefs"], "dashboard payload lost its briefs"
assert dashboard["briefs"][0]["file"] == "anything goes weekly!.md", dashboard["briefs"][0]
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
assert meta["requires_initial_run"] is True, meta
assert watch["type"] == "schedule", watch
assert float(watch["next_fire_ts"]) > float(watch["last_checked_ts"]), watch
assert "Review the latest customer feedback" in (quest_dir / "context.md").read_text()

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
conn.request("GET", "/api/control", headers={"Host": host, "Cookie": cookie})
resp = conn.getresponse()
control = json.loads(resp.read().decode())
conn.close()
created_quest = next(q for q in control["quests"] if q["id"] == created["quest_id"])
ordinary_quest = next(q for q in control["quests"] if q["id"] == "q-prompt")
assert created_quest["requires_initial_run"] is True, created_quest
assert ordinary_quest["requires_initial_run"] is False, ordinary_quest

for payload, expected_error in (
    ({"prompt": 7}, "prompt must be a string"),
    ({"prompt": "work", "priority": "urgent"}, "priority must be high, normal, or low"),
):
    status, response_body = post("/api/quests", payload)
    assert status == 400, (status, response_body)
    assert json.loads(response_body)["error"] == expected_error, response_body
PY
