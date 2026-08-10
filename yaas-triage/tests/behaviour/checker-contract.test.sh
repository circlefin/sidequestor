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
  case "$1" in
    slack_thread)   echo '{"type":"slack_thread","channel_id":"C0","thread_ts":"1.000001","last_checked_ts":"1"}' ;;
    slack_channel)  echo '{"type":"slack_channel","channel_id":"C0","last_checked_ts":"1"}' ;;
    slack_dm)       echo '{"type":"slack_dm","channel_id":"D0","user_id":"U0","last_checked_ts":"1"}' ;;
    slack_mention)  echo '{"type":"slack_mention","user_id":"U0","last_checked_ts":"1"}' ;;
    email)          echo '{"type":"email","query":"from:nobody@example.invalid","last_checked_ts":"1"}' ;;
    jira)           echo '{"type":"jira","jql":"project = NOPE","last_checked_ts":"1"}' ;;
    github_pr)      echo '{"type":"github_pr","repo":"nope/nope","last_checked_ts":"1"}' ;;
    schedule)       echo '{"type":"schedule","cron":"* * * * *","tz":"UTC","last_checked_ts":"1"}' ;;
    approval)       echo '{"type":"approval","approval_id":"appr-does-not-exist","last_checked_ts":"1"}' ;;
    *)              echo '{"type":"'"$1"'","last_checked_ts":"1"}' ;;
  esac
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
for path in "$CHECKERS"/*.py; do
  name=$(basename "$path" .py)
  case "$name" in
    result|slack_utils|cron_due|reactions) continue ;;   # not per-entry checkers
  esac
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
HELPERS="result.py slack_utils.py"
for h in $HELPERS; do
  f="$SCRIPT_DIR/checkers/$h"
  [ -f "$f" ] || { bad "helper missing: $h"; continue; }
  [ -x "$f" ] && bad "$h is executable — it could be dispatched as a checker" \
               || ok "$h is not executable (cannot be dispatched)"
done
# ...and the converse: every real watch type must have an EXECUTABLE plugin, or tick.py
# silently classifies that watch as misconfig and holds its watermark forever.
for t in slack_thread slack_channel slack_dm slack_mention email jira github_pr schedule approval; do
  f="$SCRIPT_DIR/checkers/$t.py"
  [ -x "$f" ] && ok "plugin $t.py is executable" \
               || bad "plugin $t.py is missing or not executable — that watch type is dead"
done

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "checker contract: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
