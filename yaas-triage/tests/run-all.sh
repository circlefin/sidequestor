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

# Recurse: suites now live in unit/<mirror of the source tree>/ and behaviour/. The label
# keeps the directory, because "ledger/add-watch" tells you what broke and "add-watch" does
# not.
for t in $(find "$HERE/unit" "$HERE/behaviour" -name '*.test.sh' 2>/dev/null | sort); do
  name=${t#$HERE/}
  if [ "$VERBOSE" = "-v" ]; then
    printf '\n══ %s %s\n' "$name" "$(printf '═%.0s' $(seq 1 $((60 - ${#name}))))"
    bash "$t" && PASS=$((PASS+1)) || { FAIL=$((FAIL+1)); FAILED="$FAILED $name"; }
  else
    printf '  %-44s ' "$name"
    if bash "$t" >/dev/null 2>&1; then printf '\033[32mPASS\033[0m\n'; PASS=$((PASS+1))
    else printf '\033[31mFAIL\033[0m\n'; FAIL=$((FAIL+1)); FAILED="$FAILED $name"; fi
  fi
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "$PASS suite(s) passed, $FAIL failed"
[ -n "$FAILED" ] && echo "failed:$FAILED"

# The differential harness IS run here for tick.py — the LIVE orchestrator. It runs a real
# end-to-end tick per scenario (~2s each), which is the only coverage tick.py's top-level control
# flow (run_tick / dispatch_loop / commit_quest / _on_exit) gets — the unit suites above cover
# its extracted modules, not the wiring. Skipping it would let `run-all` go green without ever
# executing the code that actually runs in production. The mutation suite (differential/
# mutations.sh) stays a deliberate pre-merge step, since it takes ~2 min.
if [ -d "$HERE/differential" ]; then
  echo
  echo "── differential: tick.py (the live orchestrator) vs recorded goldens ──"
  if bash "$HERE/differential/run.sh" check tick.py >/tmp/yaas-diff-runall.$$ 2>&1; then
    tail -1 /tmp/yaas-diff-runall.$$
  else
    cat /tmp/yaas-diff-runall.$$; FAIL=$((FAIL+1)); FAILED="$FAILED differential/tick.py"
  fi
  rm -f /tmp/yaas-diff-runall.$$
  echo
  echo "coverage:  tests/coverage.sh   (source files with no unit test)"
  echo "pre-merge (not run above): differential/mutations.sh   (~2 min; proves the goldens still bite)"
fi

[ "$FAIL" -eq 0 ]
