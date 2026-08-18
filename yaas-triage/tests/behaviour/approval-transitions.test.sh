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

# Approve is terminal even when the reviewer's instruction reads like a question.
# Both buttons are prompts to the worker; only `revise` reopens the item.
eq "a question-style review still approves (terminal)" \
  "$(run_case pending_review review question)" "200:reviewed:reviewed"

# ── the reviewer's instruction must survive every button ────────────────────────
# Both buttons are prompts to the worker, so the reviewer's instruction has to be
# carried on either path. If Approve drops it, the stored draft goes out verbatim
# and a retarget or a suppression instruction is silently lost.
load_state='
import importlib.util
from datetime import datetime, timezone
spec = importlib.util.spec_from_file_location("st", "yaas-triage/approval_state.py")
st = importlib.util.module_from_spec(spec); spec.loader.exec_module(st)
NOW = datetime.now(timezone.utc)
'

note_case() {
  python3 -c "$load_state"'
import sys
action, field = sys.argv[1:3]
item = {"status": "pending_review", "message_text": "hello", "review_history": []}
up = st.apply_transition(item, action, {"review_note": "send it to the other reviewer instead"}, NOW)
item.update({k: v for k, v in up.items() if v is not None})
print(item.get(field) or "-")
' "$1" "$2"
}

eq "approve carries the instruction to the worker" \
  "$(note_case review review_note)" "send it to the other reviewer instead"
eq "request-change carries the instruction to the worker" \
  "$(note_case revise review_note)" "send it to the other reviewer instead"
eq "approve with an instruction is still terminal" \
  "$(note_case review status)" "reviewed"

# The instruction is consumed at close time, so it has to land in the trail.
fold_case() {
  python3 -c "$load_state"'
item = {"status": "executing", "review_note": "send it to the other reviewer instead",
        "asked_at": "2026-08-18T04:21:00Z", "review_history": []}
up = st.apply_transition(item, "done", {"response_ts": "1.0"}, NOW)
hist = up.get("review_history") or []
print("%d:%s:%s" % (len(hist), hist[0]["note"] if hist else "-", up.get("review_note", "kept")))
'
}

eq "closing folds the approve-time instruction into the trail" \
  "$(fold_case)" "1:send it to the other reviewer instead:None"

# Undo must not leave a stale reviewer turn attached to a re-opened item.
undo_case() {
  python3 -c "$load_state"'
item = {"status": "reviewed", "review_note": "stale", "asked_at": "x"}
up = st.apply_transition(item, "undo", {}, NOW)
print("%s:%s" % (up["review_note"], up["asked_at"]))
'
}

# A bare Approve after a Request change must not inherit the old instruction.
stale_case() {
  python3 -c "$load_state"'
import importlib.util, json, pathlib, tempfile
spec = importlib.util.spec_from_file_location("store", "yaas-triage/approval_store.py")
store = importlib.util.module_from_spec(spec); spec.loader.exec_module(store)

tmp = pathlib.Path(tempfile.mkdtemp())
store.APPROVALS_FILE = tmp / "pending-approvals.json"
store.STATE_DIR = tmp
store.APPROVALS_FILE.write_text(json.dumps({"version": 1, "items": [
    {"id": "a1", "status": "needs_reply", "message_text": "draft",
     "review_note": "make it shorter", "asked_at": "x"}]}))
store.mutate_item("a1", lambda cur: st.apply_transition(cur, "review", {}, NOW))
saved = json.loads(store.APPROVALS_FILE.read_text())["items"][0]
print("%s:%s:%s" % (saved["status"], "review_note" in saved, "asked_at" in saved))
'
}

eq "a bare approve clears the earlier revision instruction from stored state" \
  "$(stale_case)" "reviewed:False:False"

eq "undo clears the stale reviewer instruction" "$(undo_case)" "None:None"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "approval transitions: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
