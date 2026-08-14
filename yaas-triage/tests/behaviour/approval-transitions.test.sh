#!/bin/bash
# approval-transitions.test.sh — exercise the handler's approval transition matrix.

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

run_case() {
  python3 - "$1" "$2" "${3:-normal}" <<'PY'
import importlib.util
import json
import sys

status, action, variant = sys.argv[1:4]
sys.argv = ["dashboard-server.py"]
spec = importlib.util.spec_from_file_location(
    "dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

payloads = {
    "review": {"message_text": "approved copy"},
    "revise": {"review_note": "make it shorter"},
    "cancel": {},
    "edit": {"message_text": "edited copy"},
}
payload = ({"review_note": "why?"}
           if action == "review" and variant == "question"
           else payloads[action])

data = {
    "version": 1,
    "items": [{
        "id": "appr-1",
        "quest_id": "q1",
        "quest_title": "Q1",
        "action_type": "slack_message",
        "status": status,
        "message_text": "hello",
        "target": {"channel_id": "C1", "thread_ts": "1.0"},
    }],
}
module.STATE_DIR.mkdir(parents=True, exist_ok=True)
module.APPROVALS_FILE.write_text(json.dumps(data))

handler = object.__new__(module.Handler)
response = {}
handler._send_json = lambda body, code=200: response.update(code=code, body=body)
module.Handler._handle_review(handler, "appr-1", action, payload)

persisted = json.loads(module.APPROVALS_FILE.read_text())["items"][0]["status"]
print(f"{response['code']}:{response['body'].get('status', '-')}:{persisted}")
PY
}

expected_for() {
  case "$1:$2" in
    pending_review:review|needs_reply:review) printf '200:reviewed:reviewed' ;;
    pending_review:revise|needs_reply:revise) printf '200:needs_reply:needs_reply' ;;
    pending_review:cancel|needs_reply:cancel|reviewed:cancel) printf '200:cancelled:cancelled' ;;
    reviewed:edit) printf '200:reviewed:reviewed' ;;
    *) printf '409:-:%s' "$1" ;;
  esac
}

echo "── handler transition acceptance ─────────────────────────────────────────"
for status in pending_review needs_reply reviewed executing executed cancelled; do
  for action in review revise cancel edit; do
    got="$(run_case "$status" "$action")"
    want="$(expected_for "$status" "$action")"
    eq "$status + $action" "$got" "$want"
  done
done

eq "a question-style review requests a worker reply" \
  "$(run_case pending_review review question)" "200:needs_reply:needs_reply"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval transitions: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
