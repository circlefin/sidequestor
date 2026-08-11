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

# slack-read-health.test.sh - detect a successful Slack read for blocker recovery.

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
HEALTH="$SCRIPT_DIR/dispatch/slack-read-health.py"
FIX="$SCRIPT_DIR/tests/fixtures"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check() {
  if python3 "$HEALTH" "$2" >/dev/null 2>&1; then got=yes; else got=no; fi
  [ "$got" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$got')"
}

check "cursor success proves Slack is readable" "$FIX/cursor_ok.ndjson" yes
check "codex success proves Slack is readable"  "$FIX/codex_ok.ndjson" yes
check "cursor failure proves nothing"           "$FIX/cursor_bad.ndjson" no
check "codex failure proves nothing"            "$FIX/codex_bad.ndjson" no

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
python3 - "$TMP/claude-ok.ndjson" "$TMP/claude-bad.ndjson" <<'PYEOF'
import json, sys
ok_path, bad_path = sys.argv[1:]
call = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "read-1", "name": "mcp__slack__slack_read_thread"}]}}
ok = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "read-1", "content": "message"}]}}
bad = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "read-1", "is_error": True,
     "content": "ratelimited"}]}}
open(ok_path, "w").write(json.dumps(call) + "\n" + json.dumps(ok) + "\n")
open(bad_path, "w").write(json.dumps(call) + "\n" + json.dumps(bad) + "\n")
PYEOF
check "claude success proves Slack is readable" "$TMP/claude-ok.ndjson" yes
check "claude failure proves nothing"           "$TMP/claude-bad.ndjson" no

# mcp-call.sh emits the Slack text payload directly, not a JSON response envelope.
python3 - "$TMP/codex-prose.ndjson" <<'PYEOF'
import json, sys
event = {"type": "item.completed", "item": {
    "type": "command_execution", "status": "completed", "exit_code": 0,
    "command": "./yaas-triage/surfaces/mcp-call.sh slack_read_channel '{}'",
    "aggregated_output": "Channel: #example\n\nAlice: hello"}}
open(sys.argv[1], "w").write(json.dumps(event) + "\n")
PYEOF
check "shell bridge prose proves Slack is readable" "$TMP/codex-prose.ndjson" yes

echo "slack read health: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
