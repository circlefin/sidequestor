#!/bin/bash
# log-event.test.sh -- a timeline entry is stamped by a clock, never by the caller.
#
# The bug this pins: an LLM worker hand-writing `"ts"` has only a local date and
# no time of day, so it emits midnight-of-the-local-date labelled UTC. That is
# hours off, and when the local date runs ahead of UTC it lands in the future,
# which sorts a finished action above everything real on the dashboard.

set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"

mkdir -p "$REPO/yaas-triage/surfaces" "$REPO/state/quests/active/q-log" "$REPO/state/quests/archived/q-old"
cp "$HERE/surfaces/log-event.py" "$REPO/yaas-triage/surfaces/"
: > "$REPO/state/quests/active/q-log/timeline.ndjson"
: > "$REPO/state/quests/archived/q-old/timeline.ndjson"
cd "$REPO" || exit 1

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
eq()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi }

LOG="python3 $REPO/yaas-triage/surfaces/log-event.py"

echo "── the clock, not the caller, owns ts ────────────────────────────────────"

# The exact shape a hand-writing worker produces: midnight of the local date.
out="$($LOG '{"quest_id":"q-log","event":"note","note":"hand-written stamp","ts":"2035-01-01T00:00:00Z"}')"
eq "a caller-supplied ts is reported as overridden" \
   "$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ts_overridden"])')" \
   "2035-01-01T00:00:00Z"

written="$(tail -1 state/quests/active/q-log/timeline.ndjson | python3 -c 'import json,sys; print(json.load(sys.stdin)["ts"])')"
if [[ "$written" == "2035-01-01T00:00:00Z" ]]; then
  bad "the caller's ts must never reach the timeline"
else
  ok "the caller's ts never reaches the timeline"
fi

# Stamped from the real clock: within a minute of now, and never in the future.
python3 - "$written" <<'PY' && ok "the stamp is now, and not in the future" || bad "the stamp is not a real current time"
import sys
from datetime import datetime, timezone
ts = datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
delta = (datetime.now(timezone.utc) - ts).total_seconds()
sys.exit(0 if 0 <= delta < 60 else 1)
PY

echo
echo "── the entry keeps what the caller actually knows ─────────────────────────"

$LOG '{"quest_id":"q-log","event":"info_received","channel_id":"C123","thread_ts":"1.5","note":"n","link_url":"https://example.com/x"}' >/dev/null
entry="$(tail -1 state/quests/active/q-log/timeline.ndjson)"
eq "event is recorded"        "$(printf '%s' "$entry" | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"])')"      "info_received"
eq "known fields pass through" "$(printf '%s' "$entry" | python3 -c 'import json,sys; print(json.load(sys.stdin)["channel_id"])')" "C123"
eq "unlisted fields pass through too" \
   "$($LOG '{"quest_id":"q-log","event":"executed","approval_id":"appr-1"}' >/dev/null; tail -1 state/quests/active/q-log/timeline.ndjson | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_id"])')" \
   "appr-1"

eq "the flag form works too" \
   "$($LOG --quest-id q-log --event note --note "via flags" >/dev/null; tail -1 state/quests/active/q-log/timeline.ndjson | python3 -c 'import json,sys; print(json.load(sys.stdin)["note"])')" \
   "via flags"

eq "one line per call, nothing rewritten" "$(wc -l < state/quests/active/q-log/timeline.ndjson | tr -d ' ')" "4"

# Real timelines carry ~27 event names, most quest-specific. Rejecting them would
# push a worker back to hand-writing the line, reintroducing the bad timestamp.
$LOG '{"quest_id":"q-log","event":"brief_written","note":"b"}' 2>"$TMP/warn.txt" >/dev/null
eq "an undocumented event still lands in the timeline" \
   "$(tail -1 state/quests/active/q-log/timeline.ndjson | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"])')" \
   "brief_written"
grep -q "outside the documented vocabulary" "$TMP/warn.txt" \
  && ok "and it warns on stderr" \
  || bad "an undocumented event should warn on stderr"

echo
echo "── refusals ──────────────────────────────────────────────────────────────"

set +e
$LOG '{"quest_id":"q-log","event":"weekly_recap_posted"}' >/dev/null 2>&1
eq "a quest-specific event is accepted, not rejected" "$?" "0"
$LOG '{"event":"note"}' >/dev/null 2>&1
eq "a missing quest_id is rejected" "$?" "1"
$LOG '{"quest_id":"q-log"}' >/dev/null 2>&1
eq "a missing event is rejected" "$?" "1"
$LOG '{"quest_id":"../../etc","event":"note"}' >/dev/null 2>&1
eq "a traversing quest id is rejected" "$?" "1"
$LOG '{"quest_id":"q-missing","event":"note"}' >/dev/null 2>&1
eq "an unknown quest exits 2, distinct from bad args" "$?" "2"
$LOG 'not json' >/dev/null 2>&1
eq "a malformed payload is rejected" "$?" "1"
set -e

# Quests that finished still accept a late entry: the trail outlives the quest.
$LOG '{"quest_id":"q-old","event":"note","note":"late"}' >/dev/null
eq "an archived quest can still be logged to" "$(wc -l < state/quests/archived/q-old/timeline.ndjson | tr -d ' ')" "1"

echo
echo "── no instruction file asks the model to invent a time ────────────────────"

# The helper only helps if nothing still tells a worker to write the line itself.
# Every file that instructs logging is checked, not just the dispatch skill, and
# on the permissive PHRASING too -- "log it by hand" reopens the hole just as
# effectively as the old {"ts":"<utc_iso>"} template did.
ROOT="$(cd "$HERE/.." && pwd)"
INSTRUCTION_FILES=(
  "$HERE/skills/yaas-quest-dispatch/SKILL.md"
  "$HERE/skills/yaas-reactions/SKILL.md"
  "$ROOT/CLAUDE.example.md"
  "$ROOT/ARCHITECTURE.md"
)
# CLAUDE.md is the private original of CLAUDE.example.md and is absent from a
# fresh clone, so check it only when it is there.
[[ -f "$ROOT/CLAUDE.md" ]] && INSTRUCTION_FILES+=("$ROOT/CLAUDE.md")

for f in "${INSTRUCTION_FILES[@]}"; do
  name="${f#"$ROOT"/}"
  # Drop prohibitions first: a line that says "never hand-write the line" names
  # the bad practice in order to forbid it, and must not read as permission.
  if grep -vi 'never' "$f" \
     | grep -qE '"ts" *: *"<utc_iso>"|log (a send|it|this) by hand|hand-write the (line|entry|timeline)'; then
    bad "$name still permits a hand-written timeline entry"
  else
    ok "$name does not hand the model a ts to fill in"
  fi
done

for f in "$HERE/skills/yaas-quest-dispatch/SKILL.md" "$HERE/skills/yaas-reactions/SKILL.md"; do
  grep -q 'log-event.py' "$f" \
    && ok "${f##*/skills/} points at the helper" \
    || bad "${f##*/skills/} does not mention log-event.py"
done

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
