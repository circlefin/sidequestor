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

# housekeep.test.sh — the three retire predicates, in isolation.
#
# Retiring a watch DELETES it. Drop one that should stay → a live thread stops being tracked;
# keep one that should go → watch.json grows without bound and a fired backstop haunts the
# dashboard forever. Both have happened. The differential goldens exercise these end to end;
# this pins each predicate directly, especially the boundaries and the "never retire" cases,
# where the dangerous direction is retiring something that should have stayed.

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
HK="$SCRIPT_DIR/ledger/housekeep.py"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

# retire_raw <watch-json> <meta-json> <approvals-json> <now> ; prints stdout from housekeep
retire_raw() {
  printf '%s' "$1" > "$TMP/watch.json"
  printf '%s' "$2" > "$TMP/meta.json"
  printf '%s' "$3" > "$TMP/appr.json"
  python3 "$HK" retire "$TMP/watch.json" "$TMP/meta.json" "$TMP/appr.json" --now "$4" 2>&1
}
# retire <watch-json> <meta-json> <approvals-json> <now> ; echoes surviving watch_ids
retire() {
  retire_raw "$@" >/dev/null
  jq -c '[.watches[].watch_id]' "$TMP/watch.json"
}
# resolve <meta-json> ; prints the resolved window ("None" or an int) via a tiny python shim
days() {
  python3 - "$HK" "$1" <<'PY'
import importlib.util, sys, json
spec = importlib.util.spec_from_file_location("hk", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.resolve_retire_days(json.loads(sys.argv[2]), 14))
PY
}

NOW=1786000000  # a fixed "now"
DAY=86400

echo "── slack_thread: retired past the window, kept inside it ──────────────────"
OLD=$(( NOW - 40*DAY )); NEW=$(( NOW - 5*DAY ))
W='{"watches":[{"watch_id":"old","type":"slack_thread","thread_ts":"'$OLD'.0"},
               {"watch_id":"new","type":"slack_thread","thread_ts":"'$NEW'.0"}]}'
eq "40-day-old thread dropped, 5-day-old kept (14d default)" \
   "$(retire "$W" '{}' '{"items":[]}' "$NOW")" '["new"]'

echo
echo "── slack_thread: the window is per-quest, and 'never' means never ─────────"
ANCIENT='{"watches":[{"watch_id":"a","type":"slack_thread","thread_ts":"1.0"}]}'
eq "never keeps an ancient thread" \
   "$(retire "$ANCIENT" '{"retire_slack_threads_after_days":"never"}' '{"items":[]}' "$NOW")" '["a"]'
eq "0 keeps it too" \
   "$(retire "$ANCIENT" '{"retire_slack_threads_after_days":0}' '{"items":[]}' "$NOW")" '["a"]'
eq "a custom 90-day window keeps a 40-day thread" \
   "$(retire "$W" '{"retire_slack_threads_after_days":90}' '{"items":[]}' "$NOW")" '["old","new"]'

echo
echo "── the non-integer gate (a safety property, not a nicety) ─────────────────"
eq "a plain int resolves to itself" "$(days '{"retire_slack_threads_after_days":45}')" "45"
eq "never → None"        "$(days '{"retire_slack_threads_after_days":"never"}')" "None"
eq "missing → default"   "$(days '{}')" "14"
eq "a float → None (not an integer)" "$(days '{"retire_slack_threads_after_days":"1.5"}')" "None"
eq "an injection-shaped value → None" "$(days '{"retire_slack_threads_after_days":"1[$(rm -rf /)]"}')" "None"
# Because that resolves to None, the ancient thread must SURVIVE rather than be retired.
eq "...and such a value retires nothing" \
   "$(retire "$ANCIENT" '{"retire_slack_threads_after_days":"1[$(x)]"}' '{"items":[]}' "$NOW")" '["a"]'

echo
echo "── slack_thread: only that type is retired by age ─────────────────────────"
MIX='{"watches":[{"watch_id":"t","type":"slack_thread","thread_ts":"1.0"},
                 {"watch_id":"c","type":"slack_channel","channel_id":"C1"},
                 {"watch_id":"d","type":"slack_dm","channel_id":"D1"}]}'
eq "channel and dm survive; only the old thread goes" \
   "$(retire "$MIX" '{}' '{"items":[]}' "$NOW")" '["c","d"]'

echo
echo "── approval: retired only at a terminal status ────────────────────────────"
AW='{"watches":[{"watch_id":"done","type":"approval","approval_id":"a1"},
                {"watch_id":"pending","type":"approval","approval_id":"a2"}]}'
AP='{"items":[{"id":"a1","status":"executed"},{"id":"a2","status":"pending_review"}]}'
eq "executed approval dropped, pending kept" \
   "$(retire "$AW" '{}' "$AP" "$NOW")" '["pending"]'
AP2='{"items":[{"id":"a1","status":"cancelled"},{"id":"a2","status":"needs_reply"}]}'
eq "cancelled dropped, needs_reply kept" \
   "$(retire "$AW" '{}' "$AP2" "$NOW")" '["pending"]'

echo
echo "── schedule: fired one-shot goes, recurring cron stays ────────────────────"
SW='{"watches":[
  {"watch_id":"fired","type":"schedule","next_fire_ts":"100","last_checked_ts":"200"},
  {"watch_id":"pending","type":"schedule","next_fire_ts":"999999999999","last_checked_ts":"200"},
  {"watch_id":"cron","type":"schedule","cron":"0 9 * * 1","next_fire_ts":"100","last_checked_ts":"200"}]}'
eq "fired one-shot dropped; unfired one-shot and cron kept" \
   "$(retire "$SW" '{}' '{"items":[]}' "$NOW")" '["pending","cron"]'

echo
echo "── boundaries ─────────────────────────────────────────────────────────────"
# thread exactly at the cutoff: strictly-older is retired, so exactly-at survives.
AT=$(( NOW - 14*DAY ))
BW='{"watches":[{"watch_id":"at","type":"slack_thread","thread_ts":"'$AT'.0"}]}'
eq "a thread exactly at the 14d cutoff survives (strict <)" \
   "$(retire "$BW" '{}' '{"items":[]}' "$NOW")" '["at"]'
MAL='{"watches":[{"watch_id":"bad","type":"slack_thread","thread_ts":"garbage","reason":"bad ts"}]}'
eq "a malformed slack_thread is kept, not retired on a guess" \
   "$(retire "$MAL" '{}' '{"items":[]}' "$NOW")" '["bad"]'
case "$(retire_raw "$MAL" '{}' '{"items":[]}' "$NOW")" in
  *"Kept 1 malformed slack_thread watch(es) (missing or invalid thread_ts)"*)
    ok "...and it is surfaced as malformed"
    ;;
  *)
    bad "...and it should be surfaced as malformed"
    ;;
esac
eq "...without auto-repairing thread_ts" \
   "$(jq -r '.watches[0].thread_ts' "$TMP/watch.json")" "garbage"
# schedule exactly at next_fire_ts: watermark >= fire → fired.
SB='{"watches":[{"watch_id":"eq","type":"schedule","next_fire_ts":"500","last_checked_ts":"500"}]}'
eq "a schedule with watermark == next_fire_ts is fired (>=)" \
   "$(retire "$SB" '{}' '{"items":[]}' "$NOW")" '[]'

echo
echo "── an untouched file is not rewritten (nothing to retire) ─────────────────"
KEEP='{"watches":[{"watch_id":"k","type":"slack_channel","channel_id":"C1"}]}'
printf '%s' "$KEEP" > "$TMP/w.json"; printf '{}' > "$TMP/m.json"; printf '{"items":[]}' > "$TMP/a.json"
OUT=$(python3 "$HK" retire "$TMP/w.json" "$TMP/m.json" "$TMP/a.json" --now "$NOW" 2>&1)
eq "no output when nothing retired" "$OUT" ""
eq "...and the watch is intact" "$(jq -c '[.watches[].watch_id]' "$TMP/w.json")" '["k"]'

echo
echo "── ephemeral: the reply-catcher expires, the standing subscription does not "
# The 2026-08-08 bug: a slack_channel watch opened on a DM to catch one top-level reply
# never retired, so two quests kept waking on every unrelated DM for 3 and 12 days, and one
# of them acted on a message the reactions path was also handling — a double-send.
HR=3600
EPH_OLD=$(( NOW - 240*HR )); EPH_NEW=$(( NOW - 5*HR ))   # 10 days vs 5 hours, around a 168h window
DW='{"watches":[{"watch_id":"stale","type":"slack_channel","channel_id":"D1","ephemeral":true,"created_ts":"'$EPH_OLD'.0"},
                {"watch_id":"fresh","type":"slack_channel","channel_id":"D1","ephemeral":true,"created_ts":"'$EPH_NEW'.0"},
                {"watch_id":"standing","type":"slack_channel","channel_id":"D1","created_ts":"'$EPH_OLD'.0"}]}'
eq "10-day-old ephemeral dropped; 5h-old kept; UNMARKED standing DM watch kept" \
   "$(retire "$DW" '{}' '{"items":[]}' "$NOW")" '["fresh","standing"]'
# THE REGRESSION THAT MATTERS. An earlier draft inferred "reply-catcher" from channel_id
# starting with "D" and would have deleted `standing` above — in real state that was
# two live quests whose whole job is watching a DM (one a bot's, one a colleague's).
# Permanence must be the DEFAULT.
eq "an unmarked watch NEVER expires, however old, whatever the channel type" \
   "$(retire '{"watches":[{"watch_id":"old","type":"slack_channel","channel_id":"D9","created_ts":"1.0"}]}' \
             '{}' '{"items":[]}' "$NOW")" '["old"]'
# ...and conversely the rule is not DM-only: a reply-catcher on a real C channel expires.
eq "an ephemeral watch on a C-channel expires too (not a DM-only rule)" \
   "$(retire '{"watches":[{"watch_id":"c","type":"slack_channel","channel_id":"C1","ephemeral":true,"created_ts":"'$EPH_OLD'.0"}]}' \
             '{}' '{"items":[]}' "$NOW")" '[]'
eq "a custom 336h (2wk) window keeps the 10-day watch" \
   "$(retire "$DW" '{"retire_ephemeral_after_hours":336}' '{"items":[]}' "$NOW")" \
   '["stale","fresh","standing"]'
eq "'never' disables expiry entirely" \
   "$(retire "$DW" '{"retire_ephemeral_after_hours":"never"}' '{"items":[]}' "$NOW")" \
   '["stale","fresh","standing"]'
AT_EPH='{"watches":[{"watch_id":"at","type":"slack_channel","channel_id":"D1","ephemeral":true,"created_ts":"'$(( NOW - 168*HR ))'.0"}]}'
eq "an ephemeral watch exactly at the 168h cutoff survives (strict <)" \
   "$(retire "$AT_EPH" '{}' '{"items":[]}' "$NOW")" '["at"]'

echo
echo "── ephemeral: unknown age is backfilled, never deleted on a guess ─────────"
# Entries predating created_ts have no knowable age: last_checked_ts is a watermark that
# advances every tick, not a birth date. Deleting one on a guess is the worst outcome this
# file can produce, so the clock starts instead — and MUST persist, or it restarts forever.
for BAD in '' '"created_ts":"0",' '"created_ts":"-5",' '"created_ts":"garbage",' '"created_ts":"nan",'; do
  printf '%s' '{"watches":[{"watch_id":"u","type":"slack_channel","channel_id":"D1","ephemeral":true,'"$BAD"'"reason":"r"}]}' > "$TMP/w.json"
  printf '{}' > "$TMP/m.json"; printf '{"items":[]}' > "$TMP/a.json"
  python3 "$HK" retire "$TMP/w.json" "$TMP/m.json" "$TMP/a.json" --now "$NOW" >/dev/null 2>&1
  eq "unknown age (${BAD:-missing}) survives" "$(jq -c '[.watches[].watch_id]' "$TMP/w.json")" '["u"]'
  eq "...and its clock was written to disk" \
     "$(jq -r '.watches[0].created_ts' "$TMP/w.json")" "$(printf '%.6f' "$NOW")"
done
# NaN is the one that bites: every comparison against it is False, so without an explicit
# finite check it fails BOTH the backfill test and the retire test and lives forever.
printf '%s' '{"watches":[{"watch_id":"n","type":"slack_channel","channel_id":"D1","ephemeral":true,"created_ts":"nan"}]}' > "$TMP/w.json"
python3 "$HK" retire "$TMP/w.json" "$TMP/m.json" "$TMP/a.json" --now "$NOW" >/dev/null 2>&1
python3 "$HK" retire "$TMP/w.json" "$TMP/m.json" "$TMP/a.json" --now "$(( NOW + 240*HR ))" >/dev/null 2>&1
eq "a NaN created_ts is not immortal — it retires on the next run" \
   "$(jq -c '[.watches[].watch_id]' "$TMP/w.json")" '[]'

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "housekeep: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
