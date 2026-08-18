#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

# Disabling the local Slack adapter must be a quiet capability choice, not a checker failure.
# Slack entries stay durable with held watermarks, while local watches keep running normally.

set -u

_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SOURCE="$(_find_triage "$0")" || exit 1
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ROOT="$TMP/repo"
TRIAGE="$ROOT/yaas-triage"
QUEST="$ROOT/state/quests/active/quest-toggle"
mkdir -p "$TRIAGE/checkers" "$TRIAGE/ledger" "$TRIAGE/dispatch" "$TRIAGE/ops" \
         "$TRIAGE/surfaces" "$QUEST" "$ROOT/state/triage" "$ROOT/logs"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (got '$2', want '$3')"; }

cp "$SOURCE/tick.py" "$SOURCE/tick_state.py" "$SOURCE/tick_check.py" \
   "$SOURCE/tick_dispatch.py" "$SOURCE/reaction_config.py" \
   "$SOURCE/approval_state.py" "$SOURCE/approval_store.py" "$TRIAGE/"
cp "$SOURCE/ledger/ensure-watch-ids.py" "$SOURCE/ledger/ack-watch.py" \
   "$SOURCE/ledger/checker-health.py" "$SOURCE/ledger/watch-guard.py" \
   "$SOURCE/ledger/commit.py" "$SOURCE/ledger/housekeep.py" \
   "$SOURCE/ledger/approval-helper.py" "$SOURCE/ledger/add-watch.py" "$TRIAGE/ledger/"
cp "$SOURCE/dispatch/slack-read-health.py" "$SOURCE/dispatch/spend-window.py" \
   "$SOURCE/dispatch/run-agent.py" "$SOURCE/dispatch/plan.py" "$TRIAGE/dispatch/"

for helper in ops/sync-yaas-v2.sh surfaces/mcp-call.sh ops/notify.py ops/rotate-logs.py; do
  mkdir -p "$TRIAGE/$(dirname "$helper")"
  printf '#!/bin/bash\nexit 0\n' > "$TRIAGE/$helper"
  chmod +x "$TRIAGE/$helper"
done
printf '#!/usr/bin/env python3\n' > "$TRIAGE/dispatch/extract-tokens.py"

cat > "$TRIAGE/checkers/local_fixture.py" <<PY
#!/usr/bin/env python3
import json, pathlib
pathlib.Path("$ROOT/state/triage/LOCAL-RAN").write_text("yes")
print(json.dumps({"outcome":"clean","count":0,"advance_to":"200","complete":True}))
PY
cat > "$TRIAGE/checkers/slack_fixture.py" <<PY
#!/usr/bin/env python3
import pathlib
pathlib.Path("$ROOT/state/triage/SLACK-RAN").write_text("yes")
raise SystemExit("disabled Slack checker ran")
PY
cat > "$TRIAGE/checkers/reactions.py" <<PY
#!/usr/bin/env python3
import pathlib
pathlib.Path("$ROOT/state/triage/REACTIONS-RAN").write_text("yes")
raise SystemExit("disabled reaction sweep ran")
PY
chmod +x "$TRIAGE/tick.py" "$TRIAGE/checkers/"*.py

cat > "$QUEST/watch.json" <<'JSON'
{"watches":[
  {"type":"local_fixture","last_checked_ts":"100","reason":"non-Slack source stays live"},
  {"type":"slack_fixture","last_checked_ts":"100","reason":"Slack source stays dormant"}
]}
JSON
printf '{"id":"quest-toggle","title":"Adapter toggle"}\n' > "$QUEST/meta.json"
printf '# Adapter toggle\n' > "$QUEST/context.md"
: > "$QUEST/timeline.ndjson"

cd "$ROOT" || exit 1
python3 "$TRIAGE/ledger/ensure-watch-ids.py" quest-toggle "$QUEST/watch.json" >/dev/null
YAAS_SLACK_CHECKERS_ENABLED=0 YAAS_SKIP_NETWORK_PROBE=1 YAAS_TRIAGE_MAX_PARALLEL=1 \
  python3 "$TRIAGE/tick.py" > "$TMP/tick.out" 2>&1
RC=$?

eq "tick succeeds" "$RC" "0"
[ -f "$ROOT/state/triage/LOCAL-RAN" ] && ok "non-Slack checker still runs" \
                                          || bad "non-Slack checker was disabled"
[ ! -f "$ROOT/state/triage/SLACK-RAN" ] && ok "Slack checker is not executed" \
                                           || bad "Slack checker executed"
[ ! -f "$ROOT/state/triage/REACTIONS-RAN" ] && ok "reaction sweep is not executed" \
                                               || bad "reaction sweep executed"
eq "non-Slack watermark advances" \
   "$(jq -r '.watches[] | select(.type=="local_fixture") | .last_checked_ts' "$QUEST/watch.json")" "200.000000"
eq "Slack watermark is held" \
   "$(jq -r '.watches[] | select(.type=="slack_fixture") | .last_checked_ts' "$QUEST/watch.json")" "100"
HEALTH_COUNT=0
if [ -f "$ROOT/state/triage/checker-health.json" ]; then
  HEALTH_COUNT=$(jq 'length' "$ROOT/state/triage/checker-health.json")
fi
eq "no Slack checker-health error is created" "$HEALTH_COUNT" "0"

echo "slack checker toggle: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
