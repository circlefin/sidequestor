#!/bin/bash
# approval_store.test.sh - corrupt durable state is never replaced by an empty queue.

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
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage" "$REPO/state"
cp "$SCRIPT_DIR/approval_store.py" "$REPO/yaas-triage/"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$2')"; }

STORE="$REPO/state/pending-approvals.json"
printf '%s\n' 'not json at all' > "$STORE"

(cd "$REPO" && python3 - <<'PY'
import sys
sys.path.insert(0, "yaas-triage")
import approval_store

approval_store.mutate_item("missing", lambda item: {"status": "reviewed"})
PY
) >/dev/null 2>&1
RC=$?

[ "$RC" -ne 0 ] && ok "a mutation fails when the queue is malformed" \
  || bad "a malformed queue was accepted"
eq "malformed queue bytes are preserved" "$(cat "$STORE")" "not json at all"
[ ! -e "$STORE.tmp" ] && ok "a failed mutation leaves no replacement file" \
  || bad "a failed mutation left a temporary replacement"

printf '%s\n' '[]' > "$STORE"
(cd "$REPO" && python3 - <<'PY'
import sys
sys.path.insert(0, "yaas-triage")
import approval_store

approval_store.mutate_queue(lambda data: None)
PY
) >/dev/null 2>&1
RC=$?
[ "$RC" -ne 0 ] && ok "a non-object queue is rejected" \
  || bad "a non-object queue was accepted"
eq "structurally invalid queue bytes are preserved" "$(cat "$STORE")" "[]"

echo
echo "approval_store: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
