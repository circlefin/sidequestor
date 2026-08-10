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

# commit.test.sh — the commit predicate, tested in isolation.
#
# This is the single most safety-critical decision in the system: which watermarks move.
# It used to be inline jq inside the original shell orchestrator where it could not be tested and where two
# silent-loss bugs hid until production. commit.py extracts it as a pure function; this
# proves every branch of it directly, which the differential goldens can only do indirectly.
#
# The advancing direction is the dangerous one, so most cases here assert that something is
# HELD rather than that something advances.

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
COMMIT="$SCRIPT_DIR/ledger/commit.py"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

# decide <snapshot-json> ; jq-extract <field> compares against expected
decide() { python3 "$COMMIT" "$1"; }
jqf()    { printf '%s' "$1" | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))" "$2"; }
eq()     { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

# A dirty record; args: watch_id complete advance_to
dw() { printf '{"quest_id":"q","watch_id":"%s","type":"slack_thread","complete":%s,"advance_to":%s}' \
         "$1" "$2" "$3"; }

echo "── the happy path: acked + complete + dispatched → advances ───────────────"
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true '"100"')"'],"watch_entries":[]}'
OUT=$(decide "$SNAP")
eq "one watch commits" "$(jqf "$OUT" committed_ids)" '["w1"]'
eq "...carrying its checker advance_to" "$(jqf "$OUT" moves)" '[{"watch_id": "w1", "advance_to": "100"}]'
eq "...nothing truncated" "$(jqf "$OUT" truncated)" '0'

echo
echo "── condition 2: an UNacked watch never commits ────────────────────────────"
SNAP='{"quest_id":"q","acked":[],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true '"100"')"'],"watch_entries":[]}'
eq "unacked → no commit" "$(jqf "$(decide "$SNAP")" committed_ids)" '[]'

echo
echo "── condition 3: complete=false HOLDS even when acked ──────────────────────"
# The saturation case. Advancing here would skip unseen older items.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 false '"100"')"'],"watch_entries":[]}'
OUT=$(decide "$SNAP")
eq "saturated window does NOT commit" "$(jqf "$OUT" committed_ids)" '[]'
eq "...and is counted as truncated" "$(jqf "$OUT" truncated)" '1'

echo
echo "── a null advance_to is preserved (shell resolves now-lag at write time) ──"
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true null)"'],"watch_entries":[]}'
eq "null advance_to passes through untouched" \
   "$(jqf "$(decide "$SNAP")" moves)" '[{"watch_id": "w1", "advance_to": null}]'

echo
echo "── cross-quest isolation: another quest's watch is ignored ────────────────"
SNAP='{"quest_id":"q","acked":["w1","wx"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true '"100"')"',
                        {"quest_id":"other","watch_id":"wx","type":"slack_thread","complete":true,"advance_to":"9"}],
       "watch_entries":[]}'
eq "only this quest's watch commits" "$(jqf "$(decide "$SNAP")" committed_ids)" '["w1"]'

echo
echo "── mixed batch: handled advances, saturated holds, unacked ignored ────────"
SNAP='{"quest_id":"q","acked":["w1","w2"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true '"100"')"','"$(dw w2 false '"200"')"','"$(dw w3 true '"300"')"'],
       "watch_entries":[]}'
OUT=$(decide "$SNAP")
eq "only the acked+complete one commits" "$(jqf "$OUT" committed_ids)" '["w1"]'
eq "the acked+saturated one is truncated" "$(jqf "$OUT" truncated)" '1'

echo
echo "── evidence veto: OBSERVE ONLY by default ─────────────────────────────────"
# nothing_to_do on a channel the worker did not read. Flagged, but still commits.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C2"],"enforce":false}'
OUT=$(decide "$SNAP")
eq "unverified is flagged" "$(jqf "$OUT" unverified)" '["w1"]'
eq "...but still commits (observe only)" "$(jqf "$OUT" committed_ids)" '["w1"]'
eq "...and reports it did not enforce" "$(jqf "$OUT" unverified_enforced)" 'false'

echo
echo "── evidence veto: ENFORCE holds the unverified watch ──────────────────────"
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C2"],"enforce":true}'
OUT=$(decide "$SNAP")
eq "enforced → does NOT commit" "$(jqf "$OUT" committed_ids)" '[]'
eq "...still flagged" "$(jqf "$OUT" unverified)" '["w1"]'
eq "...and reports it enforced" "$(jqf "$OUT" unverified_enforced)" 'true'

echo
echo "── evidence veto: a READ channel is not flagged ───────────────────────────"
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C1"],"enforce":true}'
OUT=$(decide "$SNAP")
eq "a read channel is never flagged" "$(jqf "$OUT" unverified)" '[]'
eq "...and commits normally even under enforce" "$(jqf "$OUT" committed_ids)" '["w1"]'

echo
echo "── evidence veto: handled is NOT checked, only nothing_to_do ──────────────"
# The documented bypass: handled skips the evidence check entirely.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":[],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C2"],"enforce":true}'
OUT=$(decide "$SNAP")
eq "handled with no read is not flagged" "$(jqf "$OUT" unverified)" '[]'
eq "...and commits even under enforce" "$(jqf "$OUT" committed_ids)" '["w1"]'

echo
echo "── evidence veto: a watch with no channel_id cannot be vetoed ─────────────"
# slack_mention has no channel to attribute a read to.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":[{"quest_id":"q","watch_id":"w1","type":"slack_mention","complete":true,"advance_to":"100"}],
       "watch_entries":[{"watch_id":"w1","type":"slack_mention","channel_id":""}],
       "read_channels":[],"enforce":true}'
eq "no channel_id → never vetoed" "$(jqf "$(decide "$SNAP")" committed_ids)" '["w1"]'

echo

echo "── Codex hardening: no evidence stream → veto is SKIPPED ───────────────────"
# The shell only runs the evidence check when the worker stream exists. Without it,
# read_channels is empty for lack of DATA, not because nothing was read. Vetoing then would
# hold legitimate work.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":[],"evidence_available":false,"enforce":true}'
OUT=$(decide "$SNAP")
eq "no stream → nothing flagged" "$(jqf "$OUT" unverified)" '[]'
eq "...and commits even under enforce" "$(jqf "$OUT" committed_ids)" '["w1"]'

echo
echo "── Codex hardening: a stringy enforce fails to OBSERVE, not enforce ────────"
# A caller passing "0" must not accidentally hold work. The dangerous direction is holding.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C2"],"enforce":"0"}'
OUT=$(decide "$SNAP")
eq "enforce=\"0\" is treated as observe" "$(jqf "$OUT" committed_ids)" '["w1"]'
eq "...but still flags" "$(jqf "$OUT" unverified)" '["w1"]'
# And the affirmative: "1" as a string DOES enforce, since the shell passes strings.
SNAP='{"quest_id":"q","acked":["w1"],"acked_ntd":["w1"],
       "dirty_watches":['"$(dw w1 true '"100"')"'],
       "watch_entries":[{"watch_id":"w1","type":"slack_thread","channel_id":"C1"}],
       "read_channels":["C2"],"enforce":"1"}'
eq "enforce=\"1\" (string) does enforce" "$(jqf "$(decide "$SNAP")" committed_ids)" '[]'

echo "── nothing acked at all → empty everything ────────────────────────────────"
SNAP='{"quest_id":"q","acked":[],"acked_ntd":[],"dirty_watches":[],"watch_entries":[]}'
OUT=$(decide "$SNAP")
eq "no moves" "$(jqf "$OUT" moves)" '[]'
eq "no truncation" "$(jqf "$OUT" truncated)" '0'

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "commit predicate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
