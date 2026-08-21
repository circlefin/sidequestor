#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

set -u
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
TRIAGE="$(_find_triage "$0")" || exit 1
CHECKER="$TRIAGE/checkers/reactions.py"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

mkdir -p "$TMP/repo/state/triage"
PENDING="$TMP/repo/state/triage/pending_reactions.json"
printf '%s\n' '{"writing_hand":["1000.000001"]}' > "$PENDING"
cp "$PENDING" "$TMP/pending.before"

cat > "$TMP/mcp-auth-failure.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$TMP/calls"
echo invalid_auth >&2
exit 1
EOF
chmod +x "$TMP/mcp-auth-failure.sh"

echo "── auth failure preserves already-detected reactions ──────────────────────"
set +e
python3 "$CHECKER" "$TMP/mcp-auth-failure.sh" 2026-01-01 "$TMP/repo" "$PENDING" \
  >"$TMP/stdout" 2>"$TMP/stderr"
STATUS=$?
set -e

eq "returns the adapter's auth status" "$STATUS" "1"
cmp -s "$PENDING" "$TMP/pending.before" \
  && ok "leaves the pending queue byte-for-byte intact" \
  || bad "changed or deleted the pending queue"
eq "aborts after the first failed emoji" "$(wc -l < "$TMP/calls" | tr -d ' ')" "1"

printf '%s\n' '{"writing_hand":["1000.000001"]}' > "$PENDING"
cp "$PENDING" "$TMP/pending.before"
: > "$TMP/calls"
cat > "$TMP/mcp-empty.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$TMP/calls"
exit 0
EOF
chmod +x "$TMP/mcp-empty.sh"

echo
echo "── empty adapter output is a failed check, not an empty result ─────────────"
set +e
python3 "$CHECKER" "$TMP/mcp-empty.sh" 2026-01-01 "$TMP/repo" "$PENDING" \
  >"$TMP/stdout" 2>"$TMP/stderr"
STATUS=$?
set -e

eq "returns a hard-error status" "$STATUS" "2"
cmp -s "$PENDING" "$TMP/pending.before" \
  && ok "preserves the queue when no result payload was returned" \
  || bad "changed or deleted the pending queue"
eq "stops after the incomplete response" "$(wc -l < "$TMP/calls" | tr -d ' ')" "1"

printf '%s\n' '{"writing_hand":["1000.000001"]}' > "$PENDING"
cp "$PENDING" "$TMP/pending.before"
: > "$TMP/calls"
cat > "$TMP/mcp-malformed.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$TMP/calls"
printf '%s\n' 'not-json'
EOF
chmod +x "$TMP/mcp-malformed.sh"

echo
echo "── malformed output is a failed check, not an empty result ────────────────"
set +e
python3 "$CHECKER" "$TMP/mcp-malformed.sh" 2026-01-01 "$TMP/repo" "$PENDING" \
  >"$TMP/stdout" 2>"$TMP/stderr"
STATUS=$?
set -e

eq "returns a hard-error status for malformed output" "$STATUS" "2"
cmp -s "$PENDING" "$TMP/pending.before" \
  && ok "preserves the queue when the result cannot be parsed" \
  || bad "changed or deleted the pending queue"
eq "stops after the malformed response" "$(wc -l < "$TMP/calls" | tr -d ' ')" "1"

printf '%s\n' '{"writing_hand":["1000.000001"]}' > "$PENDING"
cp "$PENDING" "$TMP/pending.before"
: > "$TMP/calls"
cat > "$TMP/mcp-partial.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$TMP/calls"
if [ "\$(wc -l < "$TMP/calls" | tr -d ' ')" = "1" ]; then
  printf '%s\n' '{"results":"### Result 1 of 1\\nMessage_ts: 2000.000001"}'
  exit 0
fi
echo ratelimited >&2
exit 4
EOF
chmod +x "$TMP/mcp-partial.sh"

echo
echo "── a partial sweep never replaces the complete pending queue ──────────────"
set +e
python3 "$CHECKER" "$TMP/mcp-partial.sh" 2026-01-01 "$TMP/repo" "$PENDING" \
  >"$TMP/stdout" 2>"$TMP/stderr"
STATUS=$?
set -e

eq "preserves the adapter's transient status" "$STATUS" "4"
cmp -s "$PENDING" "$TMP/pending.before" \
  && ok "does not publish partial findings over the existing queue" \
  || bad "replaced the queue with a partial sweep"
eq "aborts the remaining emoji searches" "$(wc -l < "$TMP/calls" | tr -d ' ')" "2"

printf '%s\n' '{"writing_hand":["1000.000001"]}' > "$PENDING"
: > "$TMP/calls"
cat > "$TMP/mcp-clean.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$TMP/calls"
printf '%s\n' '{"results":""}'
EOF
chmod +x "$TMP/mcp-clean.sh"

echo
echo "── only a complete successful empty sweep clears the queue ────────────────"
set +e
python3 "$CHECKER" "$TMP/mcp-clean.sh" 2026-01-01 "$TMP/repo" "$PENDING" \
  >"$TMP/stdout" 2>"$TMP/stderr"
STATUS=$?
set -e

eq "reports a successful complete sweep" "$STATUS" "0"
[ ! -e "$PENDING" ] \
  && ok "clears stale pending state after checking every emoji" \
  || bad "left the queue after a complete empty sweep"
eq "checks all configured emojis before clearing" "$(wc -l < "$TMP/calls" | tr -d ' ')" "4"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "reactions: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
