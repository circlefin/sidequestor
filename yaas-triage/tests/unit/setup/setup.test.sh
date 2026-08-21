#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

# A no-Slack-app install must reach the ordinary job prompts without entering OAuth.

set -u
HERE="$(cd "$(dirname "$0")" && pwd -P)"
TRIAGE="$(cd "$HERE/../../.." && pwd -P)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
SETUP="$REPO/yaas-triage/setup"
mkdir -p "$SETUP" "$REPO/yaas-triage/ledger"
cp "$TRIAGE/setup/setup.sh" "$TRIAGE/setup/yaas-app-config.json" "$SETUP/"
cp "$TRIAGE/ledger/approval-helper.py" "$TRIAGE/ledger/add-watch.py" "$REPO/yaas-triage/ledger/"
cp "$TRIAGE/approval_state.py" "$TRIAGE/approval_store.py" "$REPO/yaas-triage/"
cat > "$REPO/.env" <<'ENV'
YAAS_SLACK_CHECKERS_ENABLED=0
SLACK_APP_ID=
SLACK_CLIENT_ID=
SLACK_WORKSPACE_NAME=
SLACK_WORKSPACE_DOMAIN=
ENV

OUT="$TMP/setup.out"
# Decline triage, heartbeat, dashboard, auto-sync, and verification installs.
printf 'n\nn\nn\nn\nn\n' | bash "$SETUP/setup.sh" > "$OUT" 2>&1
RC=$?

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

[ "$RC" -eq 0 ] && ok "setup succeeds without Slack credentials" \
                 || bad "setup exited $RC"
grep -q "Skipping Slack app validation and OAuth" "$OUT" \
  && ok "setup reports the intentional Slack skip" || bad "Slack skip was not reported"
grep -q "Slack onboarding" "$OUT" \
  && bad "setup entered Slack OAuth" || ok "setup never enters Slack OAuth"
grep -q "setup complete" "$OUT" \
  && ok "ordinary setup flow still completes" || bad "setup did not complete"
[ -f "$REPO/state/quests/active/quest-inbox/watch.json" ] \
  && ok "setup creates the permanent Inbox quest" || bad "setup did not create Inbox"
grep -Fq 'slack_credentials.py" install "$CLIENT_ID"' "$TRIAGE/setup/setup.sh" \
  && ok "OAuth response is installed through the credential module" \
  || bad "setup bypasses the credential module"
grep -Fq -- '-w "$TOKEN"' "$TRIAGE/setup/setup.sh" \
  && bad "setup exposes a Slack token in process arguments" \
  || ok "setup does not put Slack tokens in process arguments"
grep -Fq '{"user_id":"me"}' "$TRIAGE/setup/setup.sh" \
  && bad "setup smoke test still uses the unsupported me placeholder" \
  || ok "setup smoke test uses the OAuth user ID"

echo "setup without Slack: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
