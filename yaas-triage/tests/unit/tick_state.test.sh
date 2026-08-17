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
# tick_state.py reproduces what the original shell orchestrator derives before it decides anything: repo root, paths,
# the numeric env knobs (with refuse-on-garbage validation), the per-type lag map, and the
# sorted active-quest list. It never writes state, so it is safe to exercise against a fixture.
# These cases pin the behaviours that matter: quests come back SORTED (the fairness rotation
# depends on it), a malformed gate knob REFUSES rather than reading as no-cap, and the lag map
# matches the .lag files.

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
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

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
if cmd == "quests":  print(json.dumps(m.gather_quests(c.quests_dir)))
elif cmd == "lags":  print(json.dumps(c.lag_map, sort_keys=True))
elif cmd == "root":  print(str(c.repo_root))
elif cmd == "knob":  print(c.knob(knob_name))
elif cmd == "manifests":
    print(json.dumps(m.load_watch_manifests(FIX + "/yaas-triage"), sort_keys=True))
PY
}

# A fixture repo: yaas-triage/checkers with a couple of .lag files, and some active quests.
FIX="$TMP/repo"
mkdir -p "$FIX/yaas-triage/checkers" "$FIX/state/quests/active"
printf '30\n'  > "$FIX/yaas-triage/checkers/slack_thread.lag"
printf ' 90 \n' > "$FIX/yaas-triage/checkers/email.lag"
printf 'notanumber\n' > "$FIX/yaas-triage/checkers/github_pr.lag"   # must be skipped
cat > "$FIX/yaas-triage/checkers/slack_thread.py" <<'PY'
#!/usr/bin/env python3
print("ok")
PY
chmod +x "$FIX/yaas-triage/checkers/slack_thread.py"
cat > "$FIX/yaas-triage/checkers/cron-due.py" <<'PY'
#!/usr/bin/env python3
print("ok")
PY
chmod +x "$FIX/yaas-triage/checkers/cron-due.py"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 1,
  "required": [["channel_id", "thread_ts"]],
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "thread_ts": "1.0",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
mk_quest() { mkdir -p "$FIX/state/quests/active/$1"; echo '{"watches":[]}' > "$FIX/state/quests/active/$1/watch.json"; }
mk_quest q-charlie; mk_quest q-alpha; mk_quest q-bravo
mkdir -p "$FIX/state/quests/active/q-nowatch"   # no watch.json → not a quest yet

echo "── quests come back SORTED (fairness rotation depends on it) ──────────────"
eq "sorted, and the watch-less dir is excluded" \
   "$(py "$FIX" quests)" '["q-alpha", "q-bravo", "q-charlie"]'

echo
echo "── the lag map reflects the .lag files; a non-integer one is skipped ──────"
eq "integer lags parsed (whitespace trimmed), garbage dropped" \
   "$(py "$FIX" lags)" '{"email": 90, "slack_thread": 30}'

echo
echo "── manifests load only when asked, and fail closed with the path ──────────"
printf '%s' "$(py "$FIX" manifests)" | grep -q '"slack_thread"' \
  && ok "load_watch_manifests is explicit and succeeds on a valid fixture" \
  || bad "valid manifest fixture was not loaded"
rm "$FIX/yaas-triage/checkers/slack_thread.watch.json"
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q 'slack_thread.py' \
  && ok "missing manifest names the executable checker path" \
  || bad "missing-manifest failure did not name the checker path"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 1,
  "required": [["channel_id", "thread_ts"]],
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "thread_ts": "1.0",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
printf '{bad json\n' > "$FIX/yaas-triage/checkers/slack_thread.watch.json"
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q 'slack_thread.watch.json' \
  && ok "invalid JSON names the manifest path" || bad "invalid JSON path not reported"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 99,
  "required": [["channel_id", "thread_ts"]],
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "thread_ts": "1.0",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q 'schema_version 99' \
  && ok "unsupported schema_version is rejected with the version" \
  || bad "bad schema_version was accepted or unnamed"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 1,
  "required": "channel_id",
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "thread_ts": "1.0",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q "field 'required'" \
  && ok "wrong field type is rejected by field name" \
  || bad "wrong field type was accepted or unnamed"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 1,
  "required": [["channel_id", "thread_ts"]],
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q 'checker_example' \
  && ok "checker_example must satisfy required fields" \
  || bad "bad checker_example was accepted"
cat > "$FIX/yaas-triage/checkers/slack_thread.watch.json" <<'JSON'
{
  "schema_version": 1,
  "required": [["channel_id", "thread_ts"]],
  "identity": ["channel_id", "thread_ts"],
  "checker_example": {
    "type": "slack_thread",
    "channel_id": "C1",
    "thread_ts": "1.0",
    "last_checked_ts": "1"
  },
  "open_loop": true,
  "user_creatable": true,
  "upstream": "slack"
}
JSON
chmod -x "$FIX/yaas-triage/checkers/slack_thread.py"
printf '%s' "$(py "$FIX" manifests 2>&1 || true)" | grep -q 'slack_thread.watch.json' \
  && ok "manifest with no executable checker names the manifest path" \
  || bad "missing executable checker path not reported"
chmod +x "$FIX/yaas-triage/checkers/slack_thread.py"

echo
echo "── repo root is the fixture, found by marker not by counting ──────────────"
FIXP=$(cd "$FIX" && pwd -P)
eq "root resolves to the fixture" "$(py "$FIX" root)" "$FIXP"

echo
echo "── numeric knobs: default when unset, honoured when set ───────────────────"
eq "default fanout" "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT)" "4"
eq "overridden fanout" "$(py "$FIX" knob YAAS_MAX_DISPATCH_FANOUT YAAS_MAX_DISPATCH_FANOUT=9)" "9"
eq "default checker concurrency" "$(py "$FIX" knob YAAS_TRIAGE_MAX_PARALLEL)" "3"
eq "default tick dispatch budget" "$(py "$FIX" knob YAAS_TICK_DISPATCH_BUDGET)" "3600"
eq "default minimum dispatch slice" "$(py "$FIX" knob YAAS_MIN_DISPATCH_SLICE)" "300"

echo
echo "── a garbage gate knob REFUSES (never silently reads as no-cap) ───────────"
printf '%s' "$(py "$FIX" quests YAAS_TICK_DISPATCH_BUDGET=twenty)" | grep -q "BAD_ENV_KNOB" \
  && ok "non-numeric budget is rejected" || bad "garbage budget was accepted"
printf '%s' "$(py "$FIX" quests YAAS_MAX_SPEND_6H=.)" | grep -q "BAD_ENV_KNOB" \
  && ok "a lone '.' is rejected (reads as zero in arithmetic otherwise)" || bad "'.' accepted"
printf '%s' "$(py "$FIX" quests YAAS_MAX_SPEND_1H=40)" | grep -q "BAD_ENV_KNOB" \
  && bad "a valid spend cap was wrongly rejected" || ok "a valid spend cap passes"
printf '%s' "$(py "$FIX" quests YAAS_MAX_DISPATCH_FANOUT=)" | grep -q "BAD_ENV_KNOB" \
  && bad "an empty knob was rejected" || ok "an empty knob is fine (means default)"
# A FRACTION is numeric but unhonourable: knob() returns int(float(v)), which floors, so
# 0.5 would arrive as 0 and a cap the operator meant to tighten would read as disabled —
# the exact silent-zero this validator exists to refuse. Count knobs reject it; spend caps
# must still accept decimals, because money is fractional.
printf '%s' "$(py "$FIX" quests YAAS_MAX_DISPATCH_FANOUT=0.5)" | grep -q "BAD_ENV_KNOB" \
  && ok "a fractional count knob is rejected (would floor to 0)" || bad "0.5 fanout accepted — floors to 0"
printf '%s' "$(py "$FIX" quests YAAS_UNACKED_PROMOTE=.9)" | grep -q "BAD_ENV_KNOB" \
  && ok "a bare-decimal count knob is rejected" || bad ".9 promote accepted — floors to 0"
printf '%s' "$(py "$FIX" quests YAAS_MAX_DISPATCH_FANOUT=4.0)" | grep -q "BAD_ENV_KNOB" \
  && bad "a whole number written as 4.0 was rejected" || ok "a whole number with a decimal point passes"
printf '%s' "$(py "$FIX" quests YAAS_MAX_SPEND_6H=12.50)" | grep -q "BAD_ENV_KNOB" \
  && bad "a fractional SPEND cap was rejected — money is fractional" || ok "a fractional spend cap still passes"
# The whole-number rule follows the READER, not the dict. YAAS_STALE_REPLY_HOURS is read by
# slack-send.py as float() and never through Config.knob(), so 1.5 is a real 90-minute window
# and must be accepted. YAAS_RETIRE_DEFAULT_DAYS is read by a bare int(), which raises on
# "0.5", so a fraction there must still be refused before it can crash housekeep.
printf '%s' "$(py "$FIX" quests YAAS_STALE_REPLY_HOURS=1.5)" | grep -q "BAD_ENV_KNOB" \
  && bad "a fractional stale-reply window was rejected — its reader uses float()" \
  || ok "a fractional stale-reply window is accepted (float reader)"
printf '%s' "$(py "$FIX" quests YAAS_STALE_REPLY_HOURS=abc)" | grep -q "BAD_ENV_KNOB" \
  && ok "...but a non-numeric stale-reply window is still rejected" || bad "abc accepted as hours"
printf '%s' "$(py "$FIX" quests YAAS_RETIRE_DEFAULT_DAYS=0.5)" | grep -q "BAD_ENV_KNOB" \
  && ok "a fractional retire window is rejected (int() reader would raise)" \
  || bad "0.5 retire days accepted — housekeep raises on it"

# A digits-only value long enough to overflow to inf: float() succeeds, int() raises. The
# validator must name it as an offender, not blow up with OverflowError.
printf '%s' "$(py "$FIX" quests YAAS_MAX_DISPATCH_FANOUT=$(python3 -c 'print("9"*400)'))" \
  | grep -q "BAD_ENV_KNOB" \
  && ok "an overflowing knob is an offender, not a crash" || bad "overflowing knob did not report BAD_ENV_KNOB"

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
