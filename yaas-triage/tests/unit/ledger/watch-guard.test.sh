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

# test-watch-guard.sh — append-only on watch.json, enforced by checking the outcome.
#
# This replaces test-state-write-hook.sh. The old approach regexed the agent's command
# text to guess whether it was about to write, which produced five false positives in a
# day (including on a command that merely COUNTED write sites) and could never catch a
# write it had no pattern for.
#
# The invariant is unchanged: triage owns `last_checked_ts` on entries that already
# exist, and the worker may only APPEND. What changed is that we now verify the file
# afterwards instead of trying to predict the command.

set -u
# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
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
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

# The guard resolves paths from its own location, so it runs against a copy in a
# throwaway tree rather than the real repo.
REPO="$TMP/repo"; Q="$REPO/state/quests/active/q1"
mkdir -p "$REPO/yaas-triage/ledger" "$Q"
cp "$SCRIPT_DIR/ledger/watch-guard.py" "$REPO/yaas-triage/ledger/"
G() { python3 "$REPO/yaas-triage/ledger/watch-guard.py" "$@"; }
W="$Q/watch.json"

seed() {
  cat > "$W" <<'JSON'
{"watches":[
  {"type":"slack_thread","channel_id":"C1","thread_ts":"1.1","watch_id":"watch-aaaa","last_checked_ts":"100","reason":"a"},
  {"type":"slack_channel","channel_id":"C2","watch_id":"watch-bbbb","last_checked_ts":"200","reason":"b"}
]}
JSON
}

echo "── the honest worker: appends only ────────────────────────────────────────"
seed; G snapshot q1 >/dev/null
python3 - "$W" <<'PY2'
import json,sys
p=sys.argv[1]; d=json.load(open(p))
d["watches"].append({"type":"slack_thread","channel_id":"C9","thread_ts":"9.9",
                     "watch_id":"watch-cccc","last_checked_ts":"900","reason":"appended"})
json.dump(d,open(p,"w"),indent=2)
PY2
OUT=$(G verify q1); RC=$?
eq "an append is clean"                 "$(printf '%s' "$OUT" | jq -r .verify)" "clean"
eq "...and exits 0"                     "$RC" "0"
eq "...and the append survives"         "$(jq '.watches | length' "$W")" "3"
eq "...with existing watermarks intact" "$(jq -r '.watches[0].last_checked_ts' "$W")" "100"

echo
echo "── the misbehaving worker: edits an existing watermark ────────────────────"
seed; G snapshot q1 >/dev/null
python3 -c "
import json,sys
p='$W'; d=json.load(open(p)); d['watches'][0]['last_checked_ts']='999999'
json.dump(d,open(p,'w'),indent=2)"
OUT=$(G verify q1); RC=$?
eq "a modified existing entry is caught" "$(printf '%s' "$OUT" | jq -r .verify)" "violation_repaired"
eq "...exits 1 so triage can log it"     "$RC" "1"
eq "...and the watermark is restored"    "$(jq -r '.watches[0].last_checked_ts' "$W")" "100"

echo
echo "── the worse case: an entry is deleted ────────────────────────────────────"
seed; G snapshot q1 >/dev/null
python3 -c "
import json,sys
p='$W'; d=json.load(open(p)); d['watches']=[d['watches'][1]]
json.dump(d,open(p,'w'),indent=2)"
OUT=$(G verify q1)
eq "a removed entry is detected"      "$(printf '%s' "$OUT" | jq -r '.removed[0]')" "watch-aaaa"
eq "...and put back"                  "$(jq '.watches | length' "$W")" "2"
eq "...with its watermark"            "$(jq -r --arg i watch-aaaa '[.watches[]|select(.watch_id==$i)][0].last_checked_ts' "$W")" "100"

echo
echo "── both at once: edit one, delete another, append a third ─────────────────"
seed; G snapshot q1 >/dev/null
python3 -c "
import json
p='$W'; d=json.load(open(p))
d['watches'][0]['last_checked_ts']='555'
d['watches']=[d['watches'][0]]
d['watches'].append({'type':'slack_thread','channel_id':'C9','thread_ts':'9.9',
                     'watch_id':'watch-dddd','last_checked_ts':'900','reason':'new'})
json.dump(d,open(p,'w'),indent=2)"
G verify q1 >/dev/null
eq "the edit is undone"        "$(jq -r --arg i watch-aaaa '[.watches[]|select(.watch_id==$i)][0].last_checked_ts' "$W")" "100"
eq "the deletion is undone"    "$(jq -r --arg i watch-bbbb '[.watches[]|select(.watch_id==$i)]|length' "$W")" "1"
eq "the append is KEPT"        "$(jq -r --arg i watch-dddd '[.watches[]|select(.watch_id==$i)]|length' "$W")" "1"
ok "...so a worker doing the right thing is never punished for it"

echo
echo "── the catastrophic case: the file is left unparseable ────────────────────"
seed; G snapshot q1 >/dev/null
printf 'not json\n' > "$W"
OUT=$(G verify q1)
eq "an unreadable file is rebuilt" "$(printf '%s' "$OUT" | jq -r .verify)" "restored_unreadable"
eq "...from the snapshot"          "$(jq '.watches | length' "$W")" "2"
ok "...because losing every cursor is far worse than losing an append"

echo
echo "── it never invents work ──────────────────────────────────────────────────"
seed; G clear q1
OUT=$(G verify q1)
eq "verify with no snapshot is a no-op" "$(printf '%s' "$OUT" | jq -r .verify)" "skipped"
G snapshot nosuchquest >/dev/null 2>&1 && ok "snapshotting an unknown quest is harmless" \
  || bad "snapshotting an unknown quest errored"

echo
echo "── it is wired into the orchestrator, and the old hook is gone ────────────"
grep -q 'watch-guard.py"), "snapshot"' "$SCRIPT_DIR/tick.py" && ok "tick.py snapshots before dispatch" \
  || bad "tick.py never snapshots"
grep -q 'watch-guard.py"), "verify"' "$SCRIPT_DIR/tick.py" && ok "tick.py verifies after dispatch" \
  || bad "tick.py never verifies"
[ -f "$(dirname "$SCRIPT_DIR")/.claude/hooks/deny-state-writes.sh" ] \
  && bad "the old command-text hook still exists" || ok "the old command-text hook is deleted"
python3 -c "
import json,sys
d=json.load(open('$(dirname "$SCRIPT_DIR")/.claude/settings.json'))
sys.exit(0 if 'PreToolUse' not in d.get('hooks',{}) else 1)" \
  && ok "...and is no longer registered" || bad "PreToolUse is still registered"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "watch guard: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
