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

# source-evidence.test.sh — per-backend read attribution. No network.
#
# WHY THIS EXISTS. source-evidence.py answers "which channels did this worker actually
# read", and the ack ledger HOLDS a watermark for any channel it cannot confirm. That makes
# a missing backend parser indistinguishable from a worker that read nothing: on Cursor,
# every Slack read was invisible, so with YAAS_ACK_EVIDENCE_ENFORCE=1 every nothing_to_do
# ack would be vetoed and the watch parked as misconfig after YAAS_UNACKED_PROMOTE tries.
# The backend would look broken with nothing in the logs to say why.
#
# The fixtures are REAL events, captured 2026-08-10 from `cursor-agent -p --output-format
# stream-json` and `codex exec --json` running one mcp-call.sh read, then reduced to the
# single relevant event and scrubbed of paths/channel ids. Hand-written fixtures would only
# prove the parser matches my guess at the schema; these prove it matches the product.

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
SE="$SCRIPT_DIR/dispatch/source-evidence.py"
FIX="$SCRIPT_DIR/tests/fixtures"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$2')"; }

srcs() { python3 "$SE" sources "$1" | tr '\n' ' ' | sed 's/ $//'; }
eviq() { python3 "$SE" slack "$1" >/dev/null 2>&1 && echo yes || echo no; }

echo "── a SUCCESSFUL read is attributed to its channel, on every backend ───────"
eq "cursor: shellToolCall success credits the channel" "$(srcs "$FIX/cursor_ok.ndjson")" "C0TESTCHAN"
eq "codex:  command_execution exit 0 credits the channel" "$(srcs "$FIX/codex_ok.ndjson")" "C0TESTCHAN"

echo
echo "── a FAILED read credits NOTHING ─────────────────────────────────────────"
# The dangerous direction. Crediting a failed read is a false PRESENCE, which lets a
# watermark advance over messages nobody saw — the burial this file exists to prevent.
eq "cursor: non-zero exitCode credits nothing" "$(srcs "$FIX/cursor_bad.ndjson")" ""
eq "codex:  non-zero exit_code credits nothing" "$(srcs "$FIX/codex_bad.ndjson")" ""

echo
echo "── the outage guard agrees with the attribution ──────────────────────────"
# evidence() and read_sources() parse the same events for different questions; if they
# disagree, one of them is wrong. Both now share cursor_shell_read().
eq "cursor success is evidence of working Slack" "$(eviq "$FIX/cursor_ok.ndjson")" "yes"
eq "codex success is evidence of working Slack"  "$(eviq "$FIX/codex_ok.ndjson")"  "yes"
eq "cursor failure is NOT evidence"              "$(eviq "$FIX/cursor_bad.ndjson")" "no"
eq "codex failure is NOT evidence"               "$(eviq "$FIX/codex_bad.ndjson")"  "no"

echo
echo "── FALSE PRESENCE attacks: a command that only LOOKS like a read ──────────"
# The dangerous direction, and the one a naive parser gets wrong: SHELL_SLACK_READ is a
# substring match, so any successful command whose TEXT contains the pattern used to be
# credited. Crediting a read that never happened lets the watermark advance over unseen
# messages — permanent burial. Each case below exits 0 and matches the pattern; none of
# them actually read Slack. (Raised by adversarial review, 2026-08-10.)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkev() {  # mkev <file> <exitCode> <command> <stdout> [isBackground]
  python3 - "$1" "$2" "$3" "$4" "${5:-false}" <<'PYEOF'
import json, sys
path, code, cmd, out, bg = sys.argv[1:6]
ev = {"type": "tool_call", "subtype": "completed", "tool_call": {"shellToolCall": {
        "args": {"command": cmd, "isBackground": bg == "true"},
        "result": {"success": {"exitCode": int(code), "command": cmd, "stdout": out},
                   "isBackground": bg == "true"}}}}
open(path, "w").write(json.dumps(ev) + "\n")
PYEOF
}
READCMD='./yaas-triage/surfaces/mcp-call.sh slack_read_channel {"channel_id":"C0TESTCHAN"}'
GOODOUT='{"messages":"Channel: #example (C0TESTCHAN)"}'

mkev "$TMP/echo.ndjson" 0 "echo \"$READCMD\"" "$READCMD"
eq "an echo of the read command credits nothing" "$(srcs "$TMP/echo.ndjson")" ""

mkev "$TMP/empty.ndjson" 0 "$READCMD" ""
eq "exit 0 with EMPTY output credits nothing" "$(srcs "$TMP/empty.ndjson")" ""

mkev "$TMP/bg.ndjson" 0 "$READCMD" "$GOODOUT" true
eq "a BACKGROUNDED command credits nothing (it has not read yet)" "$(srcs "$TMP/bg.ndjson")" ""

mkev "$TMP/comment.ndjson" 0 "# $READCMD" "not json at all"
eq "non-JSON output credits nothing" "$(srcs "$TMP/comment.ndjson")" ""

# ...and the control: the same shape, done properly, MUST still be credited. Without this
# the tests above could all be satisfied by a parser that simply never credits anything.
mkev "$TMP/real.ndjson" 0 "$READCMD" "$GOODOUT"
eq "the genuine read is still credited (not over-tightened)" "$(srcs "$TMP/real.ndjson")" "C0TESTCHAN"

# A result carrying both success and failure is contradictory; prefer the pessimistic read.
python3 - "$TMP/dual.ndjson" "$READCMD" "$GOODOUT" <<'PYEOF'
import json, sys
path, cmd, out = sys.argv[1:4]
ev = {"type": "tool_call", "subtype": "completed", "tool_call": {"shellToolCall": {
        "args": {"command": cmd},
        "result": {"success": {"exitCode": 0, "command": cmd, "stdout": out},
                   "failure": {"exitCode": 1, "command": cmd, "stderr": "boom"}}}}}
open(path, "w").write(json.dumps(ev) + "\n")
PYEOF
eq "a result with BOTH success and failure credits nothing" "$(srcs "$TMP/dual.ndjson")" ""

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "source evidence: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
