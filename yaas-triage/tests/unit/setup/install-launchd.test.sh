#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

# The three public installer commands keep their job-specific output while sharing mechanics.

set -u
HERE="$(cd "$(dirname "$0")" && pwd -P)"
TRIAGE="$(cd "$HERE/../../.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

REPO="$TMP/repo"
SETUP="$REPO/yaas-triage/setup"
HOME_FIX="$TMP/home"
BIN="$TMP/bin"
mkdir -p "$SETUP" "$HOME_FIX" "$BIN"
cp "$TRIAGE"/setup/install-launchd*.sh "$TRIAGE"/setup/*.plist.template "$SETUP/"

cat > "$BIN/launchctl" <<'SH'
#!/bin/bash
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
if [ "${1:-}" = "list" ]; then
  printf 'PID Status Label\n- 0 com.yaas.fixture\n'
fi
SH
chmod +x "$BIN/launchctl"

run_case() {
  local script="$1" label="$2" output_marker="$3"
  local log="$TMP/${label}.launchctl.log"
  local out="$TMP/${label}.out"
  local plist="$HOME_FIX/Library/LaunchAgents/${label}.plist"

  HOME="$HOME_FIX" PATH="$BIN:$PATH" LAUNCHCTL_LOG="$log" \
    bash "$SETUP/$script" install > "$out"

  if [ -f "$plist" ] && grep -Fq "$REPO" "$plist" && ! grep -Fq '{{' "$plist"; then
    ok "$script renders its plist with the fixture paths"
  else
    bad "$script did not render a resolved plist"
  fi
  grep -Fq "$label" "$out" && grep -Fq "$output_marker" "$out" \
    && ok "$script keeps its job-specific install guidance" \
    || bad "$script lost its job-specific install guidance"
  grep -Fq "load $plist" "$log" \
    && ok "$script loads the rendered plist" \
    || bad "$script did not load the rendered plist"

  HOME="$HOME_FIX" PATH="$BIN:$PATH" LAUNCHCTL_LOG="$log" \
    bash "$SETUP/$script" status > "$out"
  grep -Fq "$label" "$out" && grep -Fq "$plist" "$out" \
    && ok "$script status reports its job and plist" \
    || bad "$script status lost its job-specific state"

  HOME="$HOME_FIX" PATH="$BIN:$PATH" LAUNCHCTL_LOG="$log" \
    bash "$SETUP/$script" uninstall >/dev/null
  [ ! -e "$plist" ] \
    && ok "$script uninstall removes its plist" \
    || bad "$script uninstall left its plist behind"
}

echo "-- launchd installer entry points --"
run_case "install-launchd.sh" "com.yaas.triage" "python3 $REPO/yaas-triage/tick.py"
run_case "install-launchd-dashboard.sh" "com.yaas.dashboard" "http://localhost:8877"
run_case "install-launchd-heartbeat.sh" "com.yaas.heartbeat" "python3 $REPO/yaas-triage/ops/health-monitor.py"

echo
echo "install launchd: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
