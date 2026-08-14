#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# test-stale-reply-guard.sh — never auto-answer a conversation that already moved on.
#
# THE SCENARIO. Triage is stopped for a week. On resume, drain() hands the worker the
# OLDEST unread slice first, because a watermark can only cross a prefix of the gap.
# Without a guard the worker answers a seven-day-old question, then marches forward
# through the backlog one slice per tick, every reply blind to the hundreds of messages
# that followed it. In live customer threads.
#
# The guard measures staleness from the TARGET THREAD, at the send site, rather than
# from how the dispatch was triggered. A reply is stale when the conversation it answers
# has gone quiet, which is a property of the thread. Measuring it in slack-send.py means
# quest sends, manual dispatches and reaction replies are all covered by one rule with
# nothing for the model to remember. A rule in CLAUDE.md can be forgotten; this cannot.
#
# It fails CLOSED: an unreadable thread cannot be shown to be live, so the reply is
# queued rather than sent.

set -u
# yaas-triage/, found by walking up rather than by counting "..": these suites live at
# varying depths under tests/, and counting is the bug A1 removed from the scripts.
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

REPO="$TMP/repo"; TRI="$REPO/yaas-triage"
mkdir -p "$TRI/surfaces" "$TRI/ledger" "$TRI/checkers" "$REPO/state/quests/active/q-demo"
cp "$SCRIPT_DIR/surfaces/slack-send.py" "$TRI/surfaces/"
cp "$SCRIPT_DIR/ledger/approval-helper.py" "$TRI/ledger/"
cp "$SCRIPT_DIR/ledger/add-watch.py" "$TRI/ledger/"
cp "$SCRIPT_DIR/tick_state.py" "$TRI/"
cp "$SCRIPT_DIR"/checkers/*.py "$SCRIPT_DIR"/checkers/*.watch.json "$TRI/checkers/"
cp "$SCRIPT_DIR/approval_state.py" "$SCRIPT_DIR/approval_store.py" "$TRI/"
[ -f "$SCRIPT_DIR/approval.py" ] && cp "$SCRIPT_DIR/approval.py" "$TRI/"
for f in meta.json watch.json context.md timeline.ndjson; do : > "$REPO/state/quests/active/q-demo/$f"; done
printf '{"id":"q-demo","title":"Demo","status":"active","allow_send":true}\n' \
  > "$REPO/state/quests/active/q-demo/meta.json"
printf '{"watches":[]}\n' > "$REPO/state/quests/active/q-demo/watch.json"

# Stub Slack. `newest_ts` in the thread response is what the guard reads; the send/draft
# calls return the shapes slack-send.py parses.
mk_slack() {  # mk_slack <newest_ts | FAIL>
  cat > "$TRI/surfaces/mcp-call.sh" <<EOF
#!/bin/bash
tool="\$1"
case "\$tool" in
  slack_read_thread)
    if [ "$1" = "FAIL" ]; then echo "boom" >&2; exit 1; fi
    echo "Alice at $1: here is the question"
    ;;
  slack_send_message)
    echo '{"message_link":"https://x/p1","message_context":{"message_ts":"1786000000.000100"}}'
    echo "SENT" >> "$TMP/sent.log"
    ;;
  slack_send_message_draft) echo '{"channel_link":"https://x/c"}' ;;
esac
EOF
  chmod +x "$TRI/surfaces/mcp-call.sh"
  : > "$TMP/sent.log"
}

send() {  # send <extra_json_fields>
  ( cd "$REPO" && REPO_ROOT="$REPO" python3 "$TRI/surfaces/slack-send.py" \
      "{\"quest_id\":\"q-demo\",\"channel_id\":\"C1\",\"message\":\"hello\",$1}" 2>&1 )
}

NOW=$(date +%s)
FRESH="$NOW.000100"                      # right now
OLD=$(( NOW - 7 * 24 * 3600 )).000100    # seven days ago
EDGE_UNDER=$(( NOW - 23 * 3600 )).000100 # 23h — inside the 24h limit
EDGE_OVER=$(( NOW - 25 * 3600 )).000100  # 25h — outside it

echo "── a live conversation still gets a real reply ─────────────────────────────"
mk_slack "$FRESH"
OUT=$(send '"thread_ts":"1785000000.000100"')
printf '%s' "$OUT" | grep -q '"held": true' \
  && bad "a fresh thread was wrongly held" || ok "a fresh thread is replied to normally"
grep -q SENT "$TMP/sent.log" && ok "...and the message actually went out" \
  || bad "nothing was sent to a live thread"

echo
echo "── a week-old conversation is held for review ──────────────────────────────"
mk_slack "$OLD"
OUT=$(send '"thread_ts":"1785000000.000100"')
printf '%s' "$OUT" | grep -q '"held": true' \
  && ok "a 7-day-old thread is held" || bad "a 7-day-old thread was auto-answered"
grep -q SENT "$TMP/sent.log" \
  && bad "it sent anyway despite reporting held" || ok "...and nothing was sent"
printf '%s' "$OUT" | grep -q 'appr-' \
  && ok "...and it landed in the approval queue with an id" \
  || bad "no approval id returned, so the draft is invisible"

echo
echo "── the 24h boundary ───────────────────────────────────────────────────────"
mk_slack "$EDGE_UNDER"
printf '%s' "$(send '"thread_ts":"1785000000.000100"')" | grep -q '"held": true' \
  && bad "23h was held (should be inside the limit)" || ok "23h old still sends"
mk_slack "$EDGE_OVER"
printf '%s' "$(send '"thread_ts":"1785000000.000100"')" | grep -q '"held": true' \
  && ok "25h old is held" || bad "25h old was auto-answered"

echo
echo "── configurable, so a quieter policy is possible ──────────────────────────"
mk_slack "$EDGE_UNDER"
OUT=$( cd "$REPO" && REPO_ROOT="$REPO" YAAS_STALE_REPLY_HOURS=1 python3 "$TRI/surfaces/slack-send.py" \
       '{"quest_id":"q-demo","channel_id":"C1","message":"hi","thread_ts":"1785000000.000100"}' 2>&1 )
printf '%s' "$OUT" | grep -q '"held": true' \
  && ok "YAAS_STALE_REPLY_HOURS=1 holds a 23h-old thread" || bad "the threshold is not configurable"

echo
echo "── flag-style invocation matches the JSON form ───────────────────────────"
mk_slack "$FRESH"
OUT=$( cd "$REPO" && REPO_ROOT="$REPO" python3 "$TRI/surfaces/slack-send.py" \
       --channel-id C1 --message hi --thread-ts 1785000000.000100 --quest-id q-demo 2>&1 )
printf '%s' "$OUT" | grep -q '"channel_id": "C1"' \
  && ok "flag-style invocation sends successfully" || bad "flag-style invocation failed"
grep -q SENT "$TMP/sent.log" && ok "...and it still goes through Slack once" \
  || bad "flag-style invocation did not send"

echo
echo "── --help is real help, not a JSON parse error ────────────────────────────"
OUT=$( cd "$REPO" && python3 "$TRI/surfaces/slack-send.py" --help 2>&1 )
printf '%s' "$OUT" | grep -q 'invalid JSON argument' \
  && bad "--help still trips the JSON parser" || ok "--help bypasses the JSON parser"
printf '%s' "$OUT" | grep -q 'Usage' \
  && ok "...and prints the usage text" || bad "help text missing"

echo
echo "── fails CLOSED when the thread cannot be read ────────────────────────────"
# An unreadable thread cannot be shown to be live. Sending into unknown state is the
# exact failure mode this guard exists to prevent, so the safe direction is to queue.
mk_slack FAIL
OUT=$(send '"thread_ts":"1785000000.000100"')
printf '%s' "$OUT" | grep -q '"held": true' \
  && ok "an unreadable thread is held, not sent" || bad "it sent into an unreadable thread"

echo
echo "── force-draft mode forces every reply to review ──────────────────────────"
mk_slack "$FRESH"
OUT=$( cd "$REPO" && REPO_ROOT="$REPO" YAAS_FORCE_DRAFT=1 python3 "$TRI/surfaces/slack-send.py" \
       '{"quest_id":"q-demo","channel_id":"C1","message":"hi","thread_ts":"1785000000.000100"}' 2>&1 )
printf '%s' "$OUT" | grep -q '"held": true' \
  && ok "YAAS_FORCE_DRAFT=1 holds even a live thread" || bad "force-draft mode did not force a hold"

echo
echo "── things the guard must NOT interfere with ────────────────────────────────"
# A new top-level message is not a reply, so it has no conversation to be stale against.
# Gating it would block opening a DM or posting a fresh ask.
mk_slack "$OLD"
OUT=$(send '"note":"top-level"')
printf '%s' "$OUT" | grep -q '"held": true' \
  && bad "a new top-level message was held" || ok "a new top-level message is not gated"

# An explicit draft is already going to review; re-queueing it would double up.
mk_slack "$OLD"
OUT=$(send '"thread_ts":"1785000000.000100","draft":true')
printf '%s' "$OUT" | grep -q '"held": true' \
  && bad "an explicit draft was re-held" || ok "an explicit draft passes through untouched"

echo
echo "── the hold is recorded where the dashboard will see it ───────────────────"
mk_slack "$OLD"
send '"thread_ts":"1785000000.000100","note":"my note"' >/dev/null
TL="$REPO/state/quests/active/q-demo/timeline.ndjson"
grep -q '"event": *"draft_posted"' "$TL" && ok "a draft_posted event was logged" \
  || bad "no timeline entry, so the hold is invisible"
grep -q 'held_reason' "$TL" && ok "...with the reason it was held" || bad "no held_reason logged"
grep -q 'approval_id' "$TL" && ok "...and the approval id for the dashboard" \
  || bad "no approval_id in the timeline entry"

echo
echo "── regression: the guard reads the thread with the RIGHT param name ───────"
# slack_read_thread requires the parent ts under `message_ts`, not `thread_ts` (see
# checkers/slack_thread.py, the canonical caller). The guard once passed `thread_ts`, so the
# read raised and the guard failed CLOSED, silently holding every threaded reply in
# production. The functional cases above cannot catch this because the stub ignores the param
# name, so this pins it statically.
if grep -q 'slack_read_thread' "$SCRIPT_DIR/surfaces/slack-send.py" \
   && grep -A2 '_call_slack("slack_read_thread"' "$SCRIPT_DIR/surfaces/slack-send.py" | grep -q 'message_ts' \
   && ! grep -A2 '_call_slack("slack_read_thread"' "$SCRIPT_DIR/surfaces/slack-send.py" | grep -q '"thread_ts": thread_ts'; then
  ok "the guard reads slack_read_thread with message_ts, not thread_ts"
else
  bad "the guard's slack_read_thread call uses the wrong param name (must be message_ts)"
fi

echo "────────────────────────────────────────────────────────────────────────────"
echo "stale-reply guard: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
