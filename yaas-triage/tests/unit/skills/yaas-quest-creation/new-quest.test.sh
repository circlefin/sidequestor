#!/bin/bash
# new-quest.test.sh - watch manifests drive quest-creation validation.

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

REPO="$TMP/repo"
TRIAGE="$REPO/yaas-triage"
mkdir -p "$TRIAGE/skills/yaas-quest-creation" "$TRIAGE/checkers" \
  "$REPO/state/quests/active" "$REPO/state/quests/completed" "$REPO/state/quests/archived"
cp "$SCRIPT_DIR/skills/yaas-quest-creation/new-quest.py" "$TRIAGE/skills/yaas-quest-creation/"
cp "$SCRIPT_DIR/tick_state.py" "$TRIAGE/"
cp "$SCRIPT_DIR"/checkers/*.py "$SCRIPT_DIR"/checkers/*.watch.json "$TRIAGE/checkers/"
cd "$REPO" || exit 1

NQ() { python3 yaas-triage/skills/yaas-quest-creation/new-quest.py "$1" >/dev/null 2>&1; }

echo "-- manifest-backed schedule alternatives --------------------------------"
CRON='{"title":"Recurring schedule","watches":[{"type":"schedule","cron":"0 9 * * *","tz":"Asia/Singapore","reason":"daily check"}]}'
NQ "$CRON" && ok "cron with timezone is accepted" || bad "cron with timezone was rejected"
CRON_WATCH=$(find state/quests/active -path '*recurring-schedule*/watch.json' -print -quit)
eq "recurring schedule preserves timezone" "$(jq -r '.watches[0].tz' "$CRON_WATCH")" "Asia/Singapore"

ONCE='{"title":"One shot schedule","watches":[{"type":"schedule","next_fire_ts":"1893456000","reason":"follow up once"}]}'
NQ "$ONCE" && ok "one-shot schedule is accepted" || bad "one-shot schedule was rejected"
ONCE_DIR=$(find state/quests/active -maxdepth 1 -type d -name '*one-shot-schedule*' -print -quit)
eq "one-shot schedule is written" "$(jq -r '.watches[0].next_fire_ts' "$ONCE_DIR/watch.json")" "1893456000"
grep -q 'once at `1893456000`' "$ONCE_DIR/context.md" \
  && ok "one-shot context renders without assuming cron fields" \
  || bad "one-shot context is missing its fire time"

echo
echo "-- invalid and runtime-only shapes are rejected -------------------------"
for spec in \
  'bare cron|{"title":"Bare cron","watches":[{"type":"schedule","cron":"0 9 * * *","reason":"ambiguous"}]}' \
  'unknown type|{"title":"Unknown","watches":[{"type":"nope","reason":"typo"}]}' \
  'runtime-only approval|{"title":"Approval","watches":[{"type":"approval","approval_id":"a1","reason":"runtime only"}]}' \
  ; do
  label="${spec%%|*}"; body="${spec#*|}"
  NQ "$body" && bad "$label was accepted" || ok "$label is rejected"
done
eq "only the two valid quests were created" "$(find state/quests/active -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" "2"

echo
echo "----------------------------------------------------------------------------"
echo "new quest: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
