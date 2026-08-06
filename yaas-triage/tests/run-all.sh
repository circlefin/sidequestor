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

# run-all.sh — every suite. Each runs against a throwaway fixture tree and touches no
# real state, so this is safe to run at any time, including while triage is live.
#
#   yaas-triage/tests/run-all.sh          one line per suite
#   yaas-triage/tests/run-all.sh -v       full output from each

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
VERBOSE="${1:-}"
PASS=0; FAIL=0; FAILED=""

for t in "$HERE"/test-*.sh; do
  name=$(basename "$t")
  if [ "$VERBOSE" = "-v" ]; then
    printf '\n══ %s %s\n' "$name" "$(printf '═%.0s' $(seq 1 $((60 - ${#name}))))"
    bash "$t" && PASS=$((PASS+1)) || { FAIL=$((FAIL+1)); FAILED="$FAILED $name"; }
  else
    printf '  %-40s ' "$name"
    if bash "$t" >/dev/null 2>&1; then printf '\033[32mPASS\033[0m\n'; PASS=$((PASS+1))
    else printf '\033[31mFAIL\033[0m\n'; FAIL=$((FAIL+1)); FAILED="$FAILED $name"; fi
  fi
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "$PASS suite(s) passed, $FAIL failed"
[ -n "$FAILED" ] && echo "failed:$FAILED"

# The differential harness is NOT run here. It runs a real end-to-end tick per scenario
# (~4s x 17), so it belongs to a deliberate pre-merge check rather than the fast loop.
# It is the regression net for the triage.sh -> Python port: see differential/README.md.
if [ -d "$HERE/differential" ]; then
  echo
  echo "differential harness (not run above, ~70s):"
  echo "  differential/run.sh check              orchestrator vs recorded goldens"
  echo "  differential/mutations.sh              prove the harness still catches breakage"
fi

[ "$FAIL" -eq 0 ]
