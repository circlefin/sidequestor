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

# run.sh — the regression net for the triage port.
#
# Every scenario is run through an orchestrator end to end, and the resulting DECISIONS
# are compared against a golden recorded from triage.sh. The golden is the contract: if
# tick.py produces the same decisions on every scenario, the port is behaviour-preserving
# by construction rather than by review.
#
#   ./run.sh record            record goldens from the current orchestrator
#   ./run.sh check             compare the current orchestrator against the goldens
#   ./run.sh check tick.py     compare a DIFFERENT orchestrator against the same goldens
#   ./run.sh check -k slack    only scenarios whose name matches "slack"
#   ./run.sh keep              leave the fixture trees behind for inspection
#
# Record once, from the shell, while the shell is still the source of truth. After that
# `record` should be reached for only when a behaviour change is INTENDED, and the
# golden diff in code review is the proof of what changed.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TESTS="$(cd "$HERE/.." && pwd)"
TRIAGE="$(cd "$TESTS/.." && pwd)"
SCENARIOS="$HERE/scenarios"
GOLDENS="$HERE/goldens"
WORK="${TMPDIR:-/tmp}/yaas-diff-$$"

MODE="${1:-check}"
shift || true
ORCH="triage.sh"
FILTER=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    -k) FILTER="${2:-}"; shift 2 ;;
    keep) KEEP=1; shift ;;
    *) ORCH="$1"; shift ;;
  esac
done
[ "$MODE" = "keep" ] && { MODE="check"; KEEP=1; }

mkdir -p "$WORK" "$GOLDENS"
[ "$KEEP" = "1" ] || trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0; RECORDED=0
green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }

printf '\033[1mdifferential: %s (%s)\033[0m\n\n' "$MODE" "$ORCH"

for sc in "$SCENARIOS"/*.json; do
  name=$(basename "$sc" .json)
  [ -n "$FILTER" ] && case "$name" in *"$FILTER"*) ;; *) continue ;; esac

  fixture="$WORK/$name"
  python3 "$TESTS/lib/scenario.py" build "$sc" "$fixture" >/dev/null || {
    printf '  %s %s (fixture build failed)\n' "$(red FAIL)" "$name"; FAIL=$((FAIL+1)); continue; }

  # The orchestrator runs against the fixture as if it were the repo. YAAS_SCENARIO is
  # what the stubs read; YAAS_TRIAGE_DIR is how the stub agent finds the real
  # ack-watch.py. Nothing else about the environment is special, which is the point:
  # this is the real tick, not a simulation of one. The runner is chosen by extension so the
  # rewritten Python orchestrator (tick.py) is held to the identical goldens as triage.sh.
  case "$ORCH" in *.py) RUNNER=python3 ;; *) RUNNER=bash ;; esac
  ( cd "$fixture" \
      && YAAS_SCENARIO="$fixture/scenario.json" \
         YAAS_TRIAGE_DIR="$fixture/yaas-triage" \
         REPO_ROOT="$fixture" \
         "$RUNNER" "$fixture/yaas-triage/$ORCH" \
      >"$fixture/tick.out" 2>"$fixture/tick.err" )
  rc=$?

  snap="$fixture/snapshot.json"
  python3 "$TESTS/lib/snapshot.py" "$fixture" --exit "$rc" > "$snap" || {
    printf '  %s %s (snapshot failed)\n' "$(red FAIL)" "$name"; FAIL=$((FAIL+1)); continue; }

  golden="$GOLDENS/$name.json"
  if [ "$MODE" = "record" ]; then
    cp "$snap" "$golden"
    printf '  %s %s\n' "$(green REC)" "$name"
    RECORDED=$((RECORDED+1))
  elif [ ! -f "$golden" ]; then
    printf '  %s %s (no golden — run ./run.sh record)\n' "$(red FAIL)" "$name"
    FAIL=$((FAIL+1))
  else
    problems=$(python3 "$TESTS/lib/snapshot.py" --diff "$golden" "$snap")
    if [ -z "$problems" ]; then
      printf '  %s %s\n' "$(green PASS)" "$name"; PASS=$((PASS+1))
    else
      printf '  %s %s\n%s\n' "$(red FAIL)" "$name" "$problems"
      # The scenario says in its own words why it exists, so a failure explains the
      # stake rather than just the delta.
      printf '       why: %s\n' "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("why",""))' "$sc")"
      FAIL=$((FAIL+1))
    fi
  fi
done

echo
if [ "$MODE" = "record" ]; then
  echo "recorded $RECORDED golden(s) into differential/goldens/"
  echo "commit them: they are the behavioural contract the port must satisfy."
  exit 0
fi
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$KEEP" = "1" ] && echo "fixtures kept at $WORK"
[ "$FAIL" -eq 0 ]
