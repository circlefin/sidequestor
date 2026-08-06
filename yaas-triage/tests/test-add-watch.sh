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

# test-add-watch.sh — the supported way to append a watch.
#
# add-watch.py is the escape hatch the PreToolUse lock points at, so it has to be both
# safe (it can never alter an existing entry, which is the invariant) and pleasant
# enough that nobody wants to route around it.

set -u
# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

REPO="$TMP/repo"; Q="$REPO/state/quests/active/q1"
mkdir -p "$REPO/yaas-triage" "$Q"
cp "$SCRIPT_DIR/add-watch.py" "$REPO/yaas-triage/"
printf '%s\n' '{"watches":[{"type":"slack_channel","channel_id":"C0","last_checked_ts":"100","watch_id":"watch-0000000000000000","reason":"pre-existing"}]}' > "$Q/watch.json"
cd "$REPO" || exit 1
W() { python3 yaas-triage/add-watch.py "$@" 2>&1 | tail -1; }
WJ="$Q/watch.json"

echo "── appending ──────────────────────────────────────────────────────────────"
ID=$(W q1 '{"type":"slack_thread","channel_id":"C1","thread_ts":"1.1","last_checked_ts":"2.2","reason":"tracking my reply"}')
printf '%s' "$ID" | grep -Eq '^watch-[0-9a-f]{16}$' && ok "returns a well-formed watch_id" \
  || bad "bad watch_id: $ID"
eq "the watch was appended"          "$(jq '.watches | length' "$WJ")" "2"
eq "the explicit response_ts is kept" "$(jq -r '.watches[1].last_checked_ts' "$WJ")" "2.2"

echo
echo "── the invariant: an existing entry is never altered ──────────────────────"
eq "pre-existing watermark untouched" "$(jq -r '.watches[0].last_checked_ts' "$WJ")" "100"
eq "pre-existing watch_id untouched"  "$(jq -r '.watches[0].watch_id' "$WJ")" "watch-0000000000000000"

echo
echo "── idempotent, so a retry is safe ─────────────────────────────────────────"
DUP=$(W q1 '{"type":"slack_thread","channel_id":"C1","thread_ts":"1.1","last_checked_ts":"9.9","reason":"again"}')
printf '%s' "$DUP" | grep -q '^skip:duplicate' && ok "a duplicate is skipped, not appended" \
  || bad "duplicate not detected: $DUP"
eq "and the count did not grow"      "$(jq '.watches | length' "$WJ")" "2"
eq "and it did not overwrite the ts" "$(jq -r '.watches[1].last_checked_ts' "$WJ")" "2.2"

echo
echo "── validation: a malformed watch is loud, not silently unchecked ──────────"
for spec in \
  'no reason|{"type":"slack_thread","channel_id":"C1","thread_ts":"3.3"}' \
  'missing thread_ts|{"type":"slack_thread","channel_id":"C1","reason":"r"}' \
  'unknown type|{"type":"nope","reason":"r"}' \
  'schedule with neither cron nor next_fire_ts|{"type":"schedule","reason":"r"}' \
  'bad watch_mode|{"type":"slack_thread","channel_id":"C9","thread_ts":"9.9","reason":"r","watch_mode":"write"}' \
  'malformed json|{not json' \
  ; do
  label="${spec%%|*}"; body="${spec#*|}"
  if python3 yaas-triage/add-watch.py q1 "$body" >/dev/null 2>&1; then
    bad "rejected: $label — was ACCEPTED"
  else
    ok "rejected: $label"
  fi
done
eq "no invalid watch made it in"     "$(jq '.watches | length' "$WJ")" "2"

echo
echo "── documented defaults and options ────────────────────────────────────────"
W q1 '{"type":"slack_thread","channel_id":"C3","thread_ts":"5.5","reason":"draft, no send ts"}' >/dev/null
eq "ts falls back to thread_ts for a draft" "$(jq -r '.watches[-1].last_checked_ts' "$WJ")" "5.5"
W q1 '{"type":"slack_thread","channel_id":"C2","thread_ts":"4.4","reason":"escalation","watch_mode":"read_only"}' >/dev/null
eq "read_only is preserved"          "$(jq -r '.watches[-1].watch_mode' "$WJ")" "read_only"
eq "every watch has a valid id"      "$(jq '[.watches[] | select(.watch_id | test("^watch-[0-9a-f]{16}"))] | length' "$WJ")" "4"
python3 -c "import json,sys; json.load(open('$WJ'))" && ok "the file is still valid JSON" \
  || bad "watch.json was corrupted"

echo
echo "── an unknown quest is refused rather than guessed at ────────────────────"
python3 yaas-triage/add-watch.py nosuchquest '{"type":"slack_channel","channel_id":"C1","reason":"r"}' >/dev/null 2>&1 \
  && bad "an unknown quest was accepted" || ok "an unknown quest is refused"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "add-watch: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
