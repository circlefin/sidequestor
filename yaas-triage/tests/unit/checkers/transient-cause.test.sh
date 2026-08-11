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

# transient-cause.test.sh — result.transient_cause(), the exit-4 demultiplexer. No network.
#
# WHY THIS EXISTS. client.py collapses HTTP 429, 5xx, socket timeouts and connection
# failures into one exit code (4), and every Slack checker reported all of them as
# "rate limit or network". That phrasing reads as a rate limit, so on 2026-08-08 a day's
# 2,286 transient events were taken as evidence Slack was throttling us — when only 50 of
# them actually said `ratelimited`. Concurrency was nearly retuned against a number that
# mostly measured something else.
#
# So the categories here are load-bearing for diagnosis, and the inputs below are real
# client.py stderr shapes (it writes `ERROR: <detail>` before exiting). If client.py ever
# changes its wording, this test fails and the log silently stops being countable —
# which is exactly the failure we want loud.

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

check() {  # check <expected> <stderr text>
  local want="$1" input="$2" got
  got=$(python3 - "$SCRIPT_DIR" "$input" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/checkers")
import result
print(result.transient_cause(sys.argv[2], "tool"))
PY
)
  [ "$got" = "$want" ] && ok "$want  <- ${input:0:54}" \
                       || bad "expected '$want', got '$got'  <- ${input:0:54}"
}

echo "── each transient cause is named, not lumped ──────────────────────────────"
check "slack ratelimited" "ERROR: slack mcp slack_read_channel (HTTP 429): ratelimited"
check "slack ratelimited" "ERROR: slack mcp returned error: ratelimited"
check "slack 5xx"         "ERROR: slack mcp slack_read_channel (HTTP 503): service unavailable"
check "slack 5xx"         "ERROR: slack mcp slack_read_channel (HTTP 502): bad gateway"
check "timeout"           "ERROR: TimeoutError: The read operation timed out"
check "network"           "ERROR: URLError: <urlopen error [Errno 61] Connection refused>"
check "network"           "ERROR: ConnectionError: connection reset by peer"

echo
echo "── an unknown cause stays unknown ─────────────────────────────────────────"
# Forcing unrecognised text into a category would recreate the original bug in a new
# place: a confident wrong label is worse than an honest "transient", because it is the
# label people then count and act on.
check "transient" ""
check "transient" "ERROR: something nobody predicted"

echo
echo "── a rate limit is not mistaken for a timeout when both words appear ──────"
# Slack's 429 body has said "timeout" in passing before; 429 must win, because the
# remedy (back off) differs from a timeout's (retry sooner / raise the deadline).
check "slack ratelimited" "ERROR: slack mcp (HTTP 429): ratelimited, retry after timeout window"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "transient cause: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
