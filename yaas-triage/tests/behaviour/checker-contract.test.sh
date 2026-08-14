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

# test-checker-contract.sh — every checker must honour checkers/result.py.
#
# Why this exists
# ───────────────
# Converting the checkers to the JSON result contract broke schedule.py: it had a
# local variable named `result` that shadowed the imported module, so EVERY schedule
# check would have returned an error. The suite passed anyway, because the failure
# surfaced as a perfectly well-formed result:
#
#   {"outcome":"error", ... "reason":"AttributeError: 'str' object has no attribute 'counted'"}
#
# Valid JSON, valid outcome. What caught it was reading the reason field by hand.
# So the assertion here is not merely "is it parseable" — it is "does the reason name
# a Python exception type that can only mean the checker's own code is broken".
# AttributeError / NameError / UnboundLocalError / ImportError can never be caused by
# bad input; they are always our bug. That is the whole class this test guards.
#
# Every checker is exercised twice: with a plausible minimal entry, and with a
# nonsense entry. Neither may produce unparseable output or an internal exception.

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
CHECKERS="$SCRIPT_DIR/checkers"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

VALID_OUTCOMES="clean dirty ratelimited error misconfig"
# Exception types that can only mean the checker's own code is wrong. A checker may
# legitimately report a ValueError or KeyError from malformed input; it may never
# report one of these.
CODE_BUGS='AttributeError|NameError|UnboundLocalError|ImportError|IndentationError|SyntaxError|ModuleNotFoundError'

# Minimal plausible entries per type. Network-backed checkers will fail to reach
# their source in a test environment, which is fine and expected — the point is that
# they fail as a well-formed `error`/`ratelimited`, not as a code bug.
entry_for() {
  jq -c '.checker_example' "$CHECKERS/$1.watch.json"
}

watch_types() {
  python3 - "$SCRIPT_DIR/tick_state.py" "$SCRIPT_DIR" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("ts", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("\n".join(m.load_watch_manifests(sys.argv[2]).keys()))
PY
}

loader_err() {
  python3 - "$SCRIPT_DIR/tick_state.py" "$1" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ts", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.load_watch_manifests(sys.argv[2])
except Exception as exc:
    print(exc)
    sys.exit(1)
sys.exit(0)
PY
}

check_output() {
  # $1 = label, $2 = raw stdout
  local label="$1" out="$2" outcome reason
  if [ -z "$out" ]; then
    bad "$label — produced NO output (triage would read this as a malformed result)"
    return
  fi
  if [ "$(printf '%s' "$out" | wc -l | tr -d ' ')" -gt 1 ]; then
    bad "$label — produced multiple lines; the contract is exactly one"
    return
  fi
  if ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
    bad "$label — not valid JSON: ${out:0:70}"
    return
  fi
  outcome=$(printf '%s' "$out" | jq -r '.outcome // ""')
  case " $VALID_OUTCOMES " in
    *" $outcome "*) ;;
    *) bad "$label — outcome '$outcome' is not in the contract enum"; return ;;
  esac
  if [ "$(printf '%s' "$out" | jq -r 'has("complete")')" != "true" ]; then
    bad "$label — missing the 'complete' field, so triage cannot tell if the window drained"
    return
  fi
  reason=$(printf '%s' "$out" | jq -r '.reason // ""')
  if printf '%s' "$reason" | grep -Eq "$CODE_BUGS"; then
    bad "$label — reason names an internal exception, which is always a code bug: $reason"
    return
  fi
  ok "$label — $outcome"
}

echo "── every checker honours the result contract ──────────────────────────────"
for name in $(watch_types); do
  path="$CHECKERS/$name.py"
  [ -x "$path" ] || bad "$name — not executable, so triage reports 'no executable checker'"
  check_output "$name (plausible entry)" "$(python3 "$path" "$(entry_for "$name")" 2>/dev/null)"
  check_output "$name (nonsense entry)"  "$(python3 "$path" '{"nope":true}' 2>/dev/null)"
done

echo
echo "── offline checkers must actually SUCCEED, not just fail cleanly ──────────"
# schedule and approval read only local state, so in a test environment they have no
# excuse to error. This is the assertion that would have caught the schedule.py
# module-shadowing bug directly.
for name in schedule approval; do
  out=$(python3 "$CHECKERS/$name.py" "$(entry_for "$name")" 2>/dev/null)
  outcome=$(printf '%s' "$out" | jq -r '.outcome // "?"' 2>/dev/null)
  case "$outcome" in
    clean|dirty) ok "$name — $outcome (reads local state, so it must not error)" ;;
    *) bad "$name — returned '$outcome' on a valid entry with no network needed: $out" ;;
  esac
done

echo
echo "── slack prefix rule matches manifest upstream declarations ───────────────"
for manifest in "$CHECKERS"/*.watch.json; do
  type_name=$(basename "$manifest" .watch.json)
  upstream=$(jq -r '.upstream // ""' "$manifest")
  if [ "$upstream" = "slack" ] && [[ "$type_name" != slack_* ]]; then
    bad "$type_name broke the upstream=slack -> slack_* rule; fix by renaming the type or correcting upstream"
  else
    ok "$type_name satisfies the upstream=slack -> slack_* rule"
  fi
  if [[ "$type_name" = slack_* ]] && [ "$upstream" != "slack" ]; then
    bad "$type_name broke the slack_* -> upstream=slack rule; fix by renaming the type or correcting upstream"
  else
    ok "$type_name satisfies the slack_* -> upstream=slack rule"
  fi
done

echo
echo "── no checker may shadow the result module ────────────────────────────────"
# The direct guard for the bug that motivated this file.
shadow=$(grep -ln '^\s*result\s*=' "$CHECKERS"/*.py 2>/dev/null | grep -v '/result.py$' || true)
if [ -n "$shadow" ]; then
  bad "these files assign to a local named 'result', shadowing the module: $shadow"
else
  ok "no local variable shadows the imported result module"
fi

echo
echo "── helpers are NOT executable; plugins are ────────────────────────────────"
# checkers/ holds two different kinds of file: dispatchable PLUGINS, one per watch type,
# and shared HELPERS (result.py, slack_utils.py) that must never be run as a checker.
# tick.py resolves a checker as checkers/<watch_type>.py and gates on os.access(X_OK), so
# the executable bit — not the directory layout — is what separates them. That makes a
# helpers/ subfolder unnecessary, but it also means the invariant is invisible: `chmod +x
# slack_utils.py` would silently make a helper dispatchable, and a watch typed to match it
# would execute a module that never emits a checker result. Pin it here so the mode bit
# cannot drift unnoticed.
HELPERS="result.py slack_utils.py github.py"
for h in $HELPERS; do
  f="$SCRIPT_DIR/checkers/$h"
  [ -f "$f" ] || { bad "helper missing: $h"; continue; }
  [ -x "$f" ] && bad "$h is executable — it could be dispatched as a checker" \
               || ok "$h is not executable (cannot be dispatched)"
done
# ...and the converse: every real watch type must have an EXECUTABLE plugin, or tick.py
# silently classifies that watch as misconfig and holds its watermark forever.
for t in $(watch_types); do
  f="$SCRIPT_DIR/checkers/$t.py"
  [ -x "$f" ] && ok "plugin $t.py is executable" \
               || bad "plugin $t.py is missing or not executable — that watch type is dead"
done

echo
echo "── manifest inventory stays bijective with executable checkers ────────────"
eq_count() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }
DECLARED_COUNT=$(watch_types | wc -l | tr -d ' ')
MANIFEST_COUNT=$(find "$CHECKERS" -name '*.watch.json' | wc -l | tr -d ' ')
EXECUTABLE_TYPE_COUNT=$(find "$CHECKERS" -name '*.py' -perm -111 | while read -r f; do basename "$f" .py; done | grep -Ev '^(cron-due|reactions)$' | wc -l | tr -d ' ')
eq_count "every manifest is loaded as one declared watch type" "$DECLARED_COUNT" "$MANIFEST_COUNT"
eq_count "declared watch types match executable per-entry checkers" "$DECLARED_COUNT" "$EXECUTABLE_TYPE_COUNT"
for utility in cron-due reactions; do
  [ ! -e "$CHECKERS/$utility.watch.json" ] \
    && ok "$utility remains an executable non-watch utility" \
    || bad "$utility incorrectly has a watch manifest"
done

BROKEN="$TMP/repo"
mkdir -p "$BROKEN/yaas-triage"
cp -R "$CHECKERS" "$BROKEN/yaas-triage/"
rm "$BROKEN/yaas-triage/checkers/slack_thread.watch.json"
if loader_err "$BROKEN/yaas-triage" 2>&1 | grep -q 'slack_thread.py'; then
  ok "executable checker with no manifest is rejected by checker path"
else
  bad "missing-manifest checker path was not reported"
fi

BROKEN2="$TMP/repo2"
mkdir -p "$BROKEN2/yaas-triage"
cp -R "$CHECKERS" "$BROKEN2/yaas-triage/"
chmod -x "$BROKEN2/yaas-triage/checkers/slack_thread.py"
if loader_err "$BROKEN2/yaas-triage" 2>&1 | grep -q 'slack_thread.watch.json'; then
  ok "manifest with no executable checker is rejected by manifest path"
else
  bad "orphan-manifest path was not reported"
fi

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "checker contract: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
