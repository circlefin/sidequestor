#!/bin/bash
# timeline-helper-dedup.test.sh -- the shared timeline helpers must stay shared.

set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "── shared once, imported by both CLIs ────────────────────────────────────"

for f in "$HERE/surfaces/log-event.py" "$HERE/surfaces/slack-send.py"; do
  name="${f#$HERE/}"
  for helper in _utc_now _quest_dir _append_timeline; do
    if grep -q "^def $helper" "$f"; then
      bad "$name still defines $helper"
    else
      ok "$name no longer defines $helper"
    fi
  done
done

for helper in utc_now quest_dir append_timeline; do
  if grep -q "^def $helper" "$HERE/surfaces/timeline_io.py"; then
    ok "timeline_io.py defines $helper"
  else
    bad "timeline_io.py is missing $helper"
  fi
done

for f in "$HERE/surfaces/log-event.py" "$HERE/surfaces/slack-send.py"; do
  name="${f#$HERE/}"
  if grep -q 'from timeline_io import utc_now, quest_dir, append_timeline' "$f"; then
    ok "$name imports the shared helpers"
  else
    bad "$name does not import the shared helpers"
  fi
done

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
