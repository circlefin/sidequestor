#!/bin/bash
# Unlinked approvals are ordinary, independently watched items in the permanent Inbox quest.

set -u
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ledger" "$REPO/yaas-triage/checkers" \
  "$REPO/yaas-triage/ops" "$REPO/state/quests/active"
cp "$SCRIPT_DIR/ledger/approval-helper.py" "$SCRIPT_DIR/ledger/add-watch.py" "$REPO/yaas-triage/ledger/"
cp "$SCRIPT_DIR/tick_state.py" "$SCRIPT_DIR/tick_check.py" "$SCRIPT_DIR/reaction_config.py" "$REPO/yaas-triage/"
cp "$SCRIPT_DIR"/checkers/*.py "$SCRIPT_DIR"/checkers/*.watch.json "$REPO/yaas-triage/checkers/"
cp "$SCRIPT_DIR/approval_state.py" "$SCRIPT_DIR/approval_store.py" "$REPO/yaas-triage/"
cp "$SCRIPT_DIR/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$SCRIPT_DIR/../dashboard.html" "$REPO/dashboard.html"

AH() { ( cd "$REPO" && python3 yaas-triage/ledger/approval-helper.py "$@" ); }
INBOX="$REPO/state/quests/active/quest-inbox"
APPROVALS="$REPO/state/pending-approvals.json"
CHK() { ( cd "$REPO" && python3 yaas-triage/checkers/approval.py "$1" ); }

echo "── setup creates one visible permanent Inbox quest ───────────────────────"
AH ensure-inbox >/dev/null
[ -d "$INBOX" ] && ok "Inbox was created" || bad "Inbox was not created"
eq "Inbox has the fixed ID" "$(python3 -c "import json;print(json.load(open('$INBOX/meta.json'))['id'])")" "quest-inbox"
eq "Inbox is visible" "$(python3 -c "import json;print(json.load(open('$INBOX/meta.json')).get('dashboard_hidden','visible'))")" "visible"
eq "Inbox opens no sends of its own" "$(python3 -c "import json;print(json.load(open('$INBOX/meta.json'))['allow_send'])")" "False"

echo
echo "── reaction and blank approvals normalize to Inbox ───────────────────────"
RID=$(AH write '{"quest_id":"reactions","quest_title":"reaction","action_type":"slack_message","target":{"channel_id":"D1","thread_ts":"1.0"},"message_text":"reaction","context":"c","risk_reason":"r"}')
BID=$(AH write '{"quest_id":"","source":"stale_reply_guard","action_type":"slack_message","target":{"channel_id":"D1","thread_ts":"2.0"},"message_text":"held","context":"c","risk_reason":"r"}')
eq "reaction review is owned by Inbox" "$(jq -r --arg id "$RID" '.items[]|select(.id==$id)|.quest_id' "$APPROVALS")" "quest-inbox"
eq "reaction provenance is retained" "$(jq -r --arg id "$RID" '.items[]|select(.id==$id)|.source' "$APPROVALS")" "reactions"
eq "blank review is owned by Inbox" "$(jq -r --arg id "$BID" '.items[]|select(.id==$id)|.quest_id' "$APPROVALS")" "quest-inbox"
eq "stale-guard provenance is retained" "$(jq -r --arg id "$BID" '.items[]|select(.id==$id)|.source' "$APPROVALS")" "stale_reply_guard"
eq "routing has one identity" "$(jq --arg id "$BID" '[.items[]|select(.id==$id)|has("executor_quest_id")]|any' "$APPROVALS")" "false"
eq "each approval has its own Inbox watch" "$(jq '[.watches[]|select(.type=="approval")]|length' "$INBOX/watch.json")" "2"

echo
echo "── frontend approval dispatches the Inbox backend watch ──────────────────"
REVIEW=$(cd "$REPO" && python3 - "$BID" <<'PY'
import importlib.util, sys
approval_id = sys.argv[1]
sys.argv = ["dashboard-server.py"]
spec = importlib.util.spec_from_file_location("dashboard_server", "yaas-triage/ops/dashboard-server.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
handler = object.__new__(module.Handler); response = {}
handler._send_json = lambda body, code=200: response.update(code=code, body=body)
module.Handler._handle_review(handler, approval_id, "review", {})
inbox = next(q for q in module.build_dashboard()["quests"] if q["id"] == "quest-inbox")
attention = next(x for x in module.build_control()["queued"] if x["id"] == approval_id)
print(f"{response['code']}:{response['body']['status']}:{response['body']['watch_armed']}:{inbox['title']}:{attention['quest_id']}")
PY
)
eq "review remains grouped and armed under Inbox" "$REVIEW" "200:reviewed:True:Inbox:quest-inbox"
BWATCH=$(jq -c --arg id "$BID" '.watches[]|select(.approval_id==$id)' "$INBOX/watch.json")
eq "reviewed Inbox approval is dirty" "$(CHK "$BWATCH" | jq -r .outcome)" "dirty"

echo
echo "── invalid explicit quests fail before ledger mutation ───────────────────"
BEFORE=$(jq '.items|length' "$APPROVALS")
AH write '{"quest_id":"typo-quest","action_type":"slack_message","target":{"channel_id":"C9","thread_ts":"9.0"},"message_text":"x"}' >/dev/null 2>&1
RC=$?
eq "invalid explicit quest is rejected" "$RC" "2"
eq "rejected approval is not persisted" "$(jq '.items|length' "$APPROVALS")" "$BEFORE"

echo
echo "── transient watch failures retry independently ──────────────────────────"
python3 - "$APPROVALS" "$INBOX/watch.json" "$RID" <<'PY'
import json, sys
approvals_path, watch_path, approval_id = sys.argv[1:]
data = json.load(open(approvals_path))
for item in data["items"]:
    if item["id"] == approval_id: item["watch_armed"] = False
json.dump(data, open(approvals_path, "w"))
watches = json.load(open(watch_path))
watches["watches"] = [w for w in watches["watches"] if w.get("approval_id") != approval_id]
json.dump(watches, open(watch_path, "w"))
PY
RETRY=$(AH arm-pending-instructions)
eq "tick arming retries the failed item" "$(printf '%s' "$RETRY" | jq -r .rearmed)" "1"
eq "retry restores exactly one watch" "$(jq --arg id "$RID" '[.watches[]|select(.approval_id==$id)]|length' "$INBOX/watch.json")" "1"

echo
echo "── real quest ownership remains unchanged ────────────────────────────────"
mkdir -p "$REPO/state/quests/active/q-real"
printf '%s\n' '{"watches":[]}' > "$REPO/state/quests/active/q-real/watch.json"
QID=$(AH write '{"quest_id":"q-real","quest_title":"Real","source":"stale_reply_guard","action_type":"slack_message","target":{"channel_id":"C1","thread_ts":"3.0"},"message_text":"x","context":"c","risk_reason":"r"}')
eq "quest-backed stale hold stays with its quest" "$(jq -r --arg id "$QID" '.items[]|select(.id==$id)|.quest_id' "$APPROVALS")" "q-real"

echo
echo "── one-time migration rewrites legacy rows and archives the old host ─────"
LEGACY="$REPO/state/quests/active/quest-reactions-approvals"
mkdir -p "$LEGACY"
printf '%s\n' '{"id":"quest-reactions-approvals","status":"active"}' > "$LEGACY/meta.json"
printf '%s\n' '{"watches":[]}' > "$LEGACY/watch.json"
: > "$LEGACY/context.md"; : > "$LEGACY/timeline.ndjson"
python3 - "$APPROVALS" <<'PY'
import json, sys
p = sys.argv[1]; data = json.load(open(p))
data["items"].append({"id":"legacy-blank","quest_id":"","quest_title":"","executor_quest_id":"quest-reactions-approvals","status":"pending_review","action_type":"slack_message","target":{"channel_id":"D9","thread_ts":"9.0"},"message_text":"legacy"})
json.dump(data, open(p, "w"))
PY
MIGRATE=$(AH migrate-inbox)
eq "legacy approval moves to Inbox" "$(jq -r '.items[]|select(.id=="legacy-blank")|.quest_id' "$APPROVALS")" "quest-inbox"
eq "legacy executor field is removed" "$(jq '[.items[]|select(.id=="legacy-blank")|has("executor_quest_id")]|any' "$APPROVALS")" "false"
eq "legacy live approval is armed in Inbox" "$(jq '[.watches[]|select(.approval_id=="legacy-blank")]|length' "$INBOX/watch.json")" "1"
eq "legacy host is archived" "$(printf '%s' "$MIGRATE" | jq -r .legacy_host_archived)" "true"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "Inbox approval routing: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
