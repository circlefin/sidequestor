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

# watermark-precision.test.sh — every stored watermark carries exactly 6 decimals.
#
# Slack's `oldest`/`latest` return ZERO messages for a timestamp with more precision than
# that, and return them normally with exactly 6. The checkers normalize on the way out, but
# a dispatched worker reads last_checked_ts straight from watch.json and passes it through,
# so an over-precise stored value makes the worker blind while the checker sees fine. That
# asymmetry burned a real message on 2026-08-17: DIRTY at 04:08:44Z, worker read the channel
# with oldest=1786939623.4141629, got nothing, acked nothing_to_do, watermark advanced past
# an unanswered request.
#
# The other half of the rule is direction: truncate, never round. Rounding can move the
# watermark FORWARD by up to half a microsecond and step over a message sitting exactly on
# the rounded value. Re-showing a message is cheap; skipping one is silent.

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

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

# ── tick.py's formatter, the single writer every watermark move goes through ──
ts() { python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
sys.argv = ['tick.py']
import importlib.util
spec = importlib.util.spec_from_file_location('tickmod', '$SCRIPT_DIR/tick.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.slack_ts(sys.argv[1] if False else '$1'))
" 2>/dev/null; }

echo "── tick.slack_ts: exactly 6 decimals, truncating ──────────────────────────"
# Both cut DOWN, never up: .4141629 → .414162, not the .414163 rounding would give.
eq "raw float repr is cut to 6dp"      "$(ts 1786939623.4141629)" "1786939623.414162"
eq "the incident's own watermark"      "$(ts 1786120954.8022919)" "1786120954.802291"
eq "an already-6dp Slack ts is intact" "$(ts 1786939695.038339)"  "1786939695.038339"
eq "a short ts is padded, not moved"   "$(ts 555.5)"              "555.500000"
eq "an integer ts gains decimals"      "$(ts 100)"                "100.000000"
# Truncation, not rounding: .1234567 must NOT become .123457 (forward = skippable).
eq "never rounds forward"              "$(ts 1786939623.1234567)" "1786939623.123456"
eq "non-numeric passes through"        "$(ts abc)"                "abc"

# ── add-watch.py: a caller-supplied watermark is normalized too ───────────────
echo "── add-watch.py: caller-supplied precision is normalized ──────────────────"
# add-watch.py anchors QUESTS_DIR to its own repo root, so the fixture has to be a
# repo-shaped tree with the script copied into it — same pattern as the behaviour suites.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
Q="$TMP/state/quests/active/quest-precision"
mkdir -p "$Q" "$TMP/yaas-triage/ledger" "$TMP/yaas-triage/checkers"
cp "$SCRIPT_DIR/ledger/add-watch.py" "$TMP/yaas-triage/ledger/"
cp "$SCRIPT_DIR/tick_state.py" "$TMP/yaas-triage/"
# Manifests are validated against their checker, so both halves have to come along.
cp "$SCRIPT_DIR"/checkers/*.watch.json "$SCRIPT_DIR"/checkers/*.py "$TMP/yaas-triage/checkers/"
cp "$SCRIPT_DIR"/checkers/*.lag "$TMP/yaas-triage/checkers/" 2>/dev/null || true
printf '%s\n' '{"watches":[]}' > "$Q/watch.json"
printf '%s\n' '{"id":"quest-precision"}' > "$Q/meta.json"
python3 "$TMP/yaas-triage/ledger/add-watch.py" quest-precision \
  '{"type":"slack_channel","channel_id":"C0PRECISION","last_checked_ts":"1786939623.4141629","reason":"precision fixture"}' \
  >/dev/null 2>&1
GOT="$(python3 -c "
import json; print(json.load(open('$Q/watch.json'))['watches'][0]['last_checked_ts'])
" 2>/dev/null || echo MISSING)"
eq "explicit over-precise ts is truncated" "$GOT" "1786939623.414162"

echo
printf 'watermark-precision: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
