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

# test-doctor.sh — the setup validator is itself validated.
#
# doctor.sh answers a question nothing else does: is THIS MACHINE configured. The test
# suites deliberately avoid real credentials and real launchd, so they pass on a fresh
# clone with nothing set up; health-monitor.py only watches runtime. That leaves doctor
# as the only check of the install itself — and until now the script whose job is
# verifying the install was the one thing nothing verified.
#
# It is driven against a fake repo with a fake PATH, so no real Keychain, launchd job or
# Slack token is touched.

set -u
# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
# yaas-triage/, found by walking up rather than by counting "..": these suites live at
# varying depths under tests/, and counting is the bug A1 removed from the scripts.
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

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

REPO="$TMP/repo"; BIN="$TMP/bin"
mkdir -p "$REPO/yaas-triage/setup" "$REPO/state/triage" "$REPO/logs" \
         "$REPO/state/quests/active" "$BIN"
mkdir -p "$REPO/yaas-triage/ops"
cp "$SCRIPT_DIR/ops/doctor.sh" "$REPO/yaas-triage/ops/"

# Fake externals so nothing real is consulted. Each is overridden per-case below.
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }
# `launchctl list <label>` prints a plist dict, one key per line. Shape matters: a
# one-line blob made the old greedy sed parse look broken for the wrong reason.
mklaunchctl() {
  cat > "$BIN/launchctl" <<EOF
#!/bin/bash
cat <<'PLIST'
{
	"LimitLoadToSessionType" = "Aqua";
	"Label" = "com.yaas.triage";
	"OnDemand" = false;
	"LastExitStatus" = $1;
	"PID" = ${2:-1};
	"Program" = "/bin/bash";
}
PLIST
EOF
  chmod +x "$BIN/launchctl"
}
for real in jq python3 perl; do
  ln -sf "$(command -v "$real")" "$BIN/$real" 2>/dev/null || true
done

reset_env() {
  mk security 'echo "xoxp-fake-token"'
  mklaunchctl 0 123
  mk gws 'exit 0'
  mk node 'exit 0'
  mk claude 'exit 0'
  cat > "$REPO/.env" <<'ENV'
SLACK_APP_ID=A123
SLACK_CLIENT_ID=456.789
SLACK_WORKSPACE_NAME=example
SLACK_WORKSPACE_DOMAIN=example
YAAS_FROM_EMAIL="Someone <someone@example.com>"
ENV
  printf '# Quest Activation Protocol\n' > "$REPO/CLAUDE.md"
  printf '{"healthy":true,"ts":"2026-01-01T00:00:00Z","problems":[]}\n' > "$REPO/state/health-status.json"
  # A fake HOME so the plist check reads a fixture, never the real LaunchAgents dir.
  mkdir -p "$TMP/home/Library/LaunchAgents"
  : > "$TMP/home/Library/LaunchAgents/com.yaas.triage.plist"
}
# $BIN plus the system dirs only. With the inherited PATH appended, deleting a fake
# binary was a no-op because the real homebrew one answered instead, so the
# missing-prerequisite cases passed vacuously.
D() { ( cd "$REPO" && PATH="$BIN:/usr/bin:/bin:/usr/sbin:/sbin" \
          HOME="$TMP/home" bash yaas-triage/ops/doctor.sh "$@" 2>&1 ); }

echo "── it reports the three questions it is NOT answering ─────────────────────"
grep -q "tests/run-all.sh" "$SCRIPT_DIR/ops/doctor.sh" \
  && ok "the header points at the test suite for code correctness" \
  || bad "no pointer to the test suite"
grep -q "health-monitor.py" "$SCRIPT_DIR/ops/doctor.sh" \
  && ok "...and at health-monitor for runtime" || bad "no pointer to health-monitor"

echo
echo "── missing prerequisites are caught ───────────────────────────────────────"
# `claude` rather than jq: jq ships in /usr/bin on this OS, so it cannot be hidden by
# PATH at all, and a case that cannot fail proves nothing.
reset_env
rm -f "$BIN/claude"
OUT=$(D); RC=$?
printf '%s' "$OUT" | grep -q "claude not found" \
  && ok "a missing REQUIRED binary is reported" || bad "a missing claude was not reported"
[ "$RC" -ne 0 ] && ok "...and makes doctor exit non-zero" || bad "missing claude still exited 0"

reset_env
rm -f "$BIN/gws"
OUT=$(D); RC=$?
printf '%s' "$OUT" | grep -qi "gws not found" \
  && ok "a missing OPTIONAL binary is reported" || bad "gws absence not reported"
[ "$RC" -eq 0 ] && ok "...but only warns, so doctor still exits 0" \
  || bad "an optional binary was treated as required"

echo
echo "── .env problems are caught ───────────────────────────────────────────────"
reset_env; rm -f "$REPO/.env"
printf '%s' "$(D)" | grep -q "missing" && ok "a missing .env fails" || bad ".env absence not reported"

reset_env
printf 'BROKEN=Name <unquoted@example.com>\n' >> "$REPO/.env"
OUT=$(D); printf '%s' "$OUT" | grep -qi "syntax error" \
  && ok "an unquoted metacharacter in .env is caught" \
  || bad "the .env syntax check missed a shell metacharacter"
# This is the exact class of bug that once killed every tick for hours.

reset_env
python3 - "$REPO/.env" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read().replace("SLACK_APP_ID=A123", "SLACK_APP_ID=")
open(p, "w").write(s)
PY
printf '%s' "$(D)" | grep -q "SLACK_APP_ID empty" \
  && ok "an empty required var is caught" || bad "an empty SLACK_APP_ID passed"

echo
echo "── the worker contract must be present and intact ─────────────────────────"
reset_env; rm -f "$REPO/CLAUDE.md"
printf '%s' "$(D)" | grep -q "CLAUDE.md missing" \
  && ok "a missing CLAUDE.md fails" || bad "missing CLAUDE.md not reported"

reset_env; printf 'just some notes\n' > "$REPO/CLAUDE.md"
printf '%s' "$(D)" | grep -q "Quest Activation Protocol" \
  && ok "a CLAUDE.md without the protocol fails" \
  || bad "a gutted CLAUDE.md passed — the worker would have no rules"

echo
echo "── credentials ────────────────────────────────────────────────────────────"
reset_env; mk security 'exit 1'
printf '%s' "$(D)" | grep -q "Slack token not in Keychain" \
  && ok "a missing Keychain token fails" || bad "missing Slack token not reported"

echo
echo "── launchd exit codes are interpreted, not just printed ───────────────────"
reset_env; mklaunchctl 512
printf '%s' "$(D)" | grep -q "code 2" \
  && ok "512 is explained as an exit-code-2 (bad .env knob) abort" || bad "512 not explained"

reset_env; mklaunchctl 36608
printf '%s' "$(D)" | grep -q "SIGTERM (143)" \
  && ok "36608 is explained as the watchdog SIGTERM" || bad "36608 not explained"

# The one that used to warn spuriously: `launchctl kickstart -k` leaves raw 15, and
# restarting the job is routine (triage-loop.sh edits require it).
reset_env; mklaunchctl 15
OUT=$(D)
printf '%s' "$OUT" | grep -q "normal after a launchctl kickstart" \
  && ok "raw 15 is explained as a kickstart, not flagged as unexpected" \
  || bad "raw 15 still reads as unexpected"
printf '%s' "$OUT" | grep -q "Unexpected LastExitStatus=15" \
  && bad "15 still produces an 'unexpected' warning" || ok "...and produces no warning"

# Defensive, not a live bug: real `launchctl list` puts one key per line, which the
# original greedy `sed 's/.*= //; s/;//'` parsed correctly. But it only stripped the
# FIRST semicolon and never trimmed, so any variant that packed more onto the line
# yielded "0 };" and fell through every case arm to "Unexpected" — a silent downgrade
# from an interpreted status to a scary-looking warning. Digit extraction can't.
reset_env
cat > "$BIN/launchctl" <<'EOF'
#!/bin/bash
echo '{ "Label" = "com.yaas.triage"; "LastExitStatus" = 0; "PID" = 7; };'
EOF
chmod +x "$BIN/launchctl"
OUT=$(D)
printf '%s' "$OUT" | grep -q "Last triage tick exited cleanly" \
  && ok "the status parse survives extra text on the line" \
  || bad "a packed launchctl line broke the exit-code interpretation"
printf '%s' "$OUT" | grep -q "LastExitStatus=0" \
  && ok "...and reports the bare number, with no trailing punctuation" \
  || bad "trailing punctuation leaked into the reported status"

echo
echo "── runtime health is delegated, not re-implemented ────────────────────────"
grep -q "Recent activity" "$SCRIPT_DIR/ops/doctor.sh" \
  && bad "doctor still re-implements liveness (Recent activity section)" \
  || ok "the duplicated liveness section is gone"
reset_env
printf '%s' "$(D)" | grep -q "health-monitor reports healthy" \
  && ok "a healthy verdict is surfaced" || bad "health-status.json not read"

reset_env
printf '{"healthy":false,"ts":"x","problems":[{"headline":"triage is not running"}]}\n' \
  > "$REPO/state/health-status.json"
printf '%s' "$(D)" | grep -q "triage is not running" \
  && ok "an unhealthy verdict is surfaced with its headline" || bad "unhealthy verdict not surfaced"

reset_env; rm -f "$REPO/state/health-status.json"
printf '%s' "$(D)" | grep -q "install-launchd-heartbeat" \
  && ok "a missing heartbeat tells you how to install it" || bad "no heartbeat hint"

echo
echo "── a fully healthy machine reports success and exits 0 ────────────────────"
reset_env
OUT=$(D); RC=$?
printf '%s' "$OUT" | grep -q "All checks passed" && ok "reports success" || bad "did not report success"
[ "$RC" -eq 0 ] && ok "...and exits 0" || bad "exited $RC on a healthy machine"

echo
echo "── --quiet suppresses the noise but not the problems ──────────────────────"
reset_env; rm -f "$REPO/CLAUDE.md"
Q=$(D --quiet)
printf '%s' "$Q" | grep -q "CLAUDE.md missing" && ok "--quiet still shows failures" \
  || bad "--quiet hid a failure"
[ "$(printf '%s' "$Q" | grep -c '✓')" -eq 0 ] && ok "...and hides the passing lines" \
  || bad "--quiet still printed passing lines"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "doctor: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
