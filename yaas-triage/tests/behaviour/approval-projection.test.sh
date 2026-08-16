#!/bin/bash
# approval-projection.test.sh — pin how each approval status is surfaced today.

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

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (got '$2', want '$3')"; }

projection() {
  python3 - "$1" <<'PY'
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone

case = sys.argv[1]
sys.argv = ["dashboard-server.py"]
spec = importlib.util.spec_from_file_location("dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

status = "executing" if case.startswith("executing_") else case
lease = None
if case == "executing_live":
    lease = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
elif case == "executing_stalled":
    lease = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

data = {
    "version": 1,
    "items": [
        {
            "id": f"msg-{case}",
            "quest_id": "q1",
            "quest_title": "Q1",
            "action_type": "slack_message",
            "status": status,
            "message_text": "hello",
            "target": {"channel_id": "C1", "thread_ts": "1.0"},
            **({"lease_expires_at": lease} if lease else {}),
        },
        {
            "id": f"other-{case}",
            "quest_id": "q2",
            "quest_title": "Q2",
            "action_type": "file_edit",
            "status": status,
            "message_text": "patch",
            "target": {"path": "README.md"},
            **({"lease_expires_at": lease} if lease else {}),
        },
    ],
}
module.STATE_DIR.mkdir(parents=True, exist_ok=True)
module.APPROVALS_FILE.write_text(json.dumps(data))
messages = module.build_messages()
out = {
    "msg": {
        "needs_you": any(i.get("id") == f"msg-{case}" for i in messages["needs_you"]),
        "other_actions": any(i.get("id") == f"msg-{case}" for i in messages["other_actions"]),
        "queued_items": any(i.get("id") == f"msg-{case}" for i in messages["queued_items"]),
    },
    "other": {
        "needs_you": any(i.get("id") == f"other-{case}" for i in messages["needs_you"]),
        "other_actions": any(i.get("id") == f"other-{case}" for i in messages["other_actions"]),
        "queued_items": any(i.get("id") == f"other-{case}" for i in messages["queued_items"]),
    },
}
print(json.dumps(out, sort_keys=True))
PY
}

expected_msg() {
  case "$1" in
    pending_review|needs_reply|executing_stalled) printf 'true,false,false' ;;
    reviewed|executing_live) printf 'false,false,true' ;;
    executed|cancelled) printf 'false,false,false' ;;
  esac
}

expected_other() {
  case "$1" in
    pending_review|needs_reply|executing_stalled) printf 'false,true,false' ;;
    reviewed|executing_live) printf 'false,false,true' ;;
    executed|cancelled) printf 'false,false,false' ;;
  esac
}

echo "── projection today ───────────────────────────────────────────────────────"
for status in pending_review needs_reply reviewed executing_live executing_stalled executed cancelled; do
  got="$(projection "$status")"
  msg="$(printf '%s' "$got" | jq -r '.msg | [.needs_you, .other_actions, .queued_items] | @csv')"
  other="$(printf '%s' "$got" | jq -r '.other | [.needs_you, .other_actions, .queued_items] | @csv')"
  eq "msg $status" "$msg" "$(expected_msg "$status")"
  eq "other $status" "$other" "$(expected_other "$status")"
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval projection: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
