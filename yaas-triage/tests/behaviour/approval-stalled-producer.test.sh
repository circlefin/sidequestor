#!/bin/bash
# approval-stalled-producer.test.sh — the dashboard emits stalled on executing approvals.

set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ops" "$REPO/state"
cp "$HERE/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$HERE/tick_state.py" "$REPO/yaas-triage/"
cp "$HERE/tick_check.py" "$REPO/yaas-triage/"
cp "$HERE/approval_state.py" "$HERE/approval_store.py" "$REPO/yaas-triage/"
mkdir -p "$REPO/yaas-triage/checkers"
cp "$HERE"/checkers/*.py "$HERE"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
printf '%s\n' '<!doctype html><title>fixture</title>' > "$REPO/dashboard.html"
cd "$REPO" || exit 1

python3 - <<'PY'
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone

sys.argv = ["dashboard-server.py"]
spec = importlib.util.spec_from_file_location("dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

now = datetime.now(timezone.utc)
items = [
    {
        "id": "expired",
        "quest_id": "q1",
        "quest_title": "Q1",
        "action_type": "slack_message",
        "status": "executing",
        "message_text": "hello",
        "lease_expires_at": (now - timedelta(minutes=1)).isoformat(),
        "target": {"channel_id": "C1", "thread_ts": "1.0"},
    },
    {
        "id": "live",
        "quest_id": "q2",
        "quest_title": "Q2",
        "action_type": "slack_message",
        "status": "executing",
        "message_text": "hello",
        "lease_expires_at": (now + timedelta(minutes=1)).isoformat(),
        "target": {"channel_id": "C2", "thread_ts": "2.0"},
    },
    {
        "id": "missing",
        "quest_id": "q3",
        "quest_title": "Q3",
        "action_type": "slack_message",
        "status": "executing",
        "message_text": "hello",
        "target": {"channel_id": "C3", "thread_ts": "3.0"},
    },
    {
        "id": "bad",
        "quest_id": "q4",
        "quest_title": "Q4",
        "action_type": "slack_message",
        "status": "executing",
        "message_text": "hello",
        "lease_expires_at": "not-a-timestamp",
        "target": {"channel_id": "C4", "thread_ts": "4.0"},
    },
    {
        "id": "reviewed",
        "quest_id": "q5",
        "quest_title": "Q5",
        "action_type": "slack_message",
        "status": "reviewed",
        "message_text": "hello",
        "lease_expires_at": (now - timedelta(minutes=1)).isoformat(),
        "target": {"channel_id": "C5", "thread_ts": "5.0"},
    },
]

module.STATE_DIR.mkdir(parents=True, exist_ok=True)
module.APPROVALS_FILE.write_text(json.dumps({"version": 1, "items": items}))
cards = {item["id"]: module._approval_card(item) for item in items}

assert cards["expired"]["stalled"] is True, cards["expired"]
assert cards["live"]["stalled"] is False, cards["live"]
assert cards["missing"]["stalled"] is False, cards["missing"]
assert cards["bad"]["stalled"] is False, cards["bad"]
assert cards["reviewed"]["stalled"] is False, cards["reviewed"]
PY
