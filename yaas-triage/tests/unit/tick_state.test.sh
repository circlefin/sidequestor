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

# tick_state.test.sh — the config/loading foundation of the tick.py orchestrator.
#
# tick_state.py reproduces what the original shell orchestrator derives before it decides anything:
# repo root, paths, the numeric env knobs (with refuse-on-garbage validation) and the per-type lag
# map. It never writes state, so it is safe to exercise against a fixture. These cases pin the
# behaviours that matter: a malformed gate knob REFUSES rather than reading as no-cap, and the lag
# map matches the .lag files.

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
. "$SCRIPT_DIR/tests/lib/harness.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# Import tick_state from the real module against a fixture repo built under $TMP.
py() {
  python3 - "$SCRIPT_DIR/tick_state.py" "$@" <<'PY'
import importlib.util, sys, json, os
spec = importlib.util.spec_from_file_location("ts", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
FIX = sys.argv[2]           # fixture repo root
cmd = sys.argv[3]
# For `knob`, argv[4] is the knob NAME; env overrides (if any) start at argv[5]. For every
# other command, overrides start at argv[4]. Separating them fixes the earlier bug where the
# knob name was consumed as an empty override and shadowed .env.
knob_name = sys.argv[4] if cmd == "knob" and len(sys.argv) > 4 else None
overrides = sys.argv[(5 if cmd == "knob" else 4):]
env = {k: v for k, v in os.environ.items() if not k.startswith("YAAS_")}
for kv in overrides:
    k, _, v = kv.partition("="); env[k] = v
try:
    c = m.Config(FIX + "/yaas-triage", environ=env)
except m.BadEnvKnob as e:
    print("BAD_ENV_KNOB:" + str(e)); sys.exit(0)
if cmd == "lags":  print(json.dumps(c.lag_map, sort_keys=True))
elif cmd == "root":  print(str(c.repo_root))
elif cmd == "knob":  print(c.knob(knob_name))
PY
}

# A fixture repo: yaas-triage/checkers with a couple of .lag files, and some active quests.
FIX="$TMP/repo"
mkdir -p "$FIX/yaas-triage/checkers" "$FIX/state/quests/active"
printf '30\n'  > "$FIX/yaas-triage/checkers/slack_thread.lag"
printf ' 90 \n' > "$FIX/yaas-triage/checkers/email.lag"
printf 'notanumber\n' > "$FIX/yaas-triage/checkers/github_pr.lag"   # must be skipped
echo "── the lag map reflects the .lag files; a non-integer one is skipped ──────"
eq "integer lags parsed (whitespace trimmed), garbage dropped" \
   "$(py "$FIX" lags)" '{"email": 90, "slack_thread": 30}'

echo
echo "── repo root is the fixture, found by marker not by counting ──────────────"
FIXP=$(cd "$FIX" && pwd -P)
eq "root resolves to the fixture" "$(py "$FIX" root)" "$FIXP"

echo
echo "── numeric knobs: default when unset, honoured when set ───────────────────"
eq "default fanout" "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT)" "4"
eq "overridden fanout" "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT YAAS_MAX_DISPATCH_FANOUT=9)" "9"

echo
echo "── a garbage gate knob REFUSES (never silently reads as no-cap) ───────────"
printf '%s' "$(py "$FIX" root YAAS_TICK_DISPATCH_BUDGET=twenty)" | grep -q "BAD_ENV_KNOB" \
  && ok "non-numeric budget is rejected" || bad "garbage budget was accepted"
printf '%s' "$(py "$FIX" root YAAS_MAX_SPEND_6H=.)" | grep -q "BAD_ENV_KNOB" \
  && ok "a lone '.' is rejected (reads as zero in arithmetic otherwise)" || bad "'.' accepted"
printf '%s' "$(py "$FIX" root YAAS_MAX_SPEND_1H=40)" | grep -q "BAD_ENV_KNOB" \
  && bad "a valid spend cap was wrongly rejected" || ok "a valid spend cap passes"
printf '%s' "$(py "$FIX" root YAAS_MAX_DISPATCH_FANOUT=)" | grep -q "BAD_ENV_KNOB" \
  && bad "an empty knob was rejected" || ok "an empty knob is fine (means default)"

echo
echo "── .env is merged without overriding the real environment ─────────────────"
printf 'YAAS_MAX_DISPATCH_FANOUT=7\n' > "$FIX/.env"
eq ".env supplies a value when the env does not" "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT)" "7"
eq "the real environment wins over .env" \
   "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT YAAS_MAX_DISPATCH_FANOUT=3)" "3"
# A malformed .env LINE is skipped, not executed (the shell-injection hazard).
printf 'this is not valid shell $(rm -rf /)\nYAAS_MAX_DISPATCH_FANOUT=5\n' > "$FIX/.env"
eq "a malformed .env line is skipped, the valid one still read" \
   "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT)" "5"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "tick_state: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
