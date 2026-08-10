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

# test-rotate-logs.sh — rotation never loses a record.
#
# Rotation is the only thing in the system that DELETES state, so the property that
# matters is conservation: every line either stays in the live file or appears in the
# archive. Losing a timeline entry loses the record that a message was sent.

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
. "$SCRIPT_DIR/tests/lib/harness.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/repo"; STATE="$REPO/state"
mkdir -p "$STATE/quests/active/q1" "$STATE/quests/completed/q2"
export YAAS_ROTATE_REPO_ROOT="$REPO" YAAS_ROTATE_FORCE=1
R() { python3 "$SCRIPT_DIR/ops/rotate-logs.py" 2>/dev/null; }
old() { date -u -v-"$1"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "$1 days ago" +%Y-%m-%dT%H:%M:%SZ; }

echo "── run-log: old lines archived, recent kept, nothing lost ─────────────────"
{
  for i in 1 2 3;   do printf '{"ts":"%s","event":"gate_idle","n":%s}\n' "$(old 30)" "$i"; done
  for i in 4 5;     do printf '{"ts":"%s","event":"gate_idle","n":%s}\n' "$(old 1)"  "$i"; done
  printf 'this line is not json\n'
} > "$STATE/run-log.ndjson"
R
LIVE=$(grep -c '' "$STATE/run-log.ndjson")
ARCH=$(cat "$STATE"/run-log-archive-*.ndjson 2>/dev/null | grep -c '' || echo 0)
eq "3 old entries archived"                "$ARCH" "3"
eq "2 recent + 1 unparseable kept live"    "$LIVE" "3"
eq "conservation: nothing vanished"        "$((LIVE + ARCH))" "6"
grep -q "not json" "$STATE/run-log.ndjson" && ok "an unparseable line is KEPT, never dropped" \
  || bad "an unparseable line was discarded"

echo
echo "── timelines: trimmed to the newest 100, overflow archived ────────────────"
for i in $(seq 1 250); do printf '{"ts":"2026-01-01T00:00:00Z","event":"note","n":%s}\n' "$i"; done \
  > "$STATE/quests/active/q1/timeline.ndjson"
printf '{"ts":"2026-01-01T00:00:00Z","event":"note","n":1}\n' > "$STATE/quests/completed/q2/timeline.ndjson"
R
eq "active quest trimmed to 100"    "$(grep -c '' "$STATE/quests/active/q1/timeline.ndjson")" "100"
eq "the other 150 archived"         "$(grep -c '' "$STATE/quests/active/q1/timeline.archive.ndjson")" "150"
eq "the NEWEST are the ones kept"   "$(tail -1 "$STATE/quests/active/q1/timeline.ndjson" | jq -r .n)" "250"
eq "a short timeline is untouched"  "$(grep -c '' "$STATE/quests/completed/q2/timeline.ndjson")" "1"
[ -f "$STATE/quests/completed/q2/timeline.archive.ndjson" ] \
  && bad "archived a timeline that did not need it" || ok "no archive created when under the limit"

echo
echo "── approvals: only OLD and FINISHED items are pruned ──────────────────────"
python3 - "$STATE/pending-approvals.json" <<PY
import json, sys
from datetime import datetime, timezone, timedelta
def ago(d): return (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
json.dump({"version": 1, "items": [
  {"id":"a","status":"executed",      "created_at": ago(40)},   # old + finished -> prune
  {"id":"b","status":"cancelled",     "created_at": ago(40)},   # old + finished -> prune
  {"id":"c","status":"executed",      "created_at": ago(2)},    # finished but recent -> keep
  {"id":"d","status":"pending_review","created_at": ago(400)},  # old but LIVE -> keep
  {"id":"e","status":"needs_reply",   "created_at": ago(400)},  # old but LIVE -> keep
]}, open(sys.argv[1], "w"))
PY
R
KEPT=$(jq -r '[.items[].id] | sort | join(",")' "$STATE/pending-approvals.json")
eq "prunes only old finished items" "$KEPT" "c,d,e"
ok "...an old but still-pending approval is never pruned, however stale"

echo
echo "── the 23-hour gate ───────────────────────────────────────────────────────"
[ -f "$STATE/last_rotation.ts" ] && ok "sentinel written after a run" || bad "no sentinel written"
unset YAAS_ROTATE_FORCE
for i in $(seq 1 250); do printf '{"ts":"2026-01-01T00:00:00Z","event":"note","n":%s}\n' "$i"; done \
  > "$STATE/quests/active/q1/timeline.ndjson"
python3 "$SCRIPT_DIR/ops/rotate-logs.py" 2>/dev/null
eq "a second run inside 23h does nothing" \
   "$(grep -c '' "$STATE/quests/active/q1/timeline.ndjson")" "250"
export YAAS_ROTATE_FORCE=1

echo
echo "── one bad file does not block the rest ───────────────────────────────────"
printf 'not json at all\n' > "$STATE/pending-approvals.json"
for i in $(seq 1 250); do printf '{"ts":"2026-01-01T00:00:00Z","event":"note","n":%s}\n' "$i"; done \
  > "$STATE/quests/active/q1/timeline.ndjson"
rm -f "$STATE/last_rotation.ts"
R
eq "timelines still rotated despite a corrupt approvals file" \
   "$(grep -c '' "$STATE/quests/active/q1/timeline.ndjson")" "100"
grep -q "not json at all" "$STATE/pending-approvals.json" \
  && ok "the corrupt file is left alone rather than truncated" \
  || bad "a corrupt file was overwritten"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "rotate-logs: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
