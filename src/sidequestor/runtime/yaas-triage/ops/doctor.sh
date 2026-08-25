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

# doctor.sh — is THIS MACHINE configured to run yaas?
#
# Three different questions, three different tools:
#   yaas-triage/tests/run-all.sh   is the CODE correct?   (fixtures, no real state —
#                                  passes on a machine with nothing set up)
#   doctor.sh                      is this MACHINE set up? (real Keychain, real PATH,
#                                  real .env, real plist)
#   health-monitor.py              is it WORKING right now? (runtime, continuous)
#
# Only doctor answers the middle one, which is why the test suite does not replace it.
#
# Verifies prerequisites, config, credentials, launchd state, and recent
# successful runs. Prints a checklist; exits 0 if everything is green,
# 1 if anything failed.
#
# Usage:
#   ./doctor.sh            # full report
#   ./doctor.sh --quiet    # only show problems (for cron / CI use)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The repo root is the nearest ancestor directory that contains yaas-triage/.
#
# NOT "$SCRIPT_DIR/..": that holds only while every script sits directly in yaas-triage/,
# and silently resolves to yaas-triage/ itself once a script moves into a subdirectory,
# writing state into a parallel tree nothing reads. `pwd -P` resolves symlinks so this
# agrees with Python's Path.resolve() in the sibling scripts. Fails non-zero rather than
# guessing, because a guessed root is the silent divergence being eliminated.
_repo_root() {
  local d
  d=$(cd "$1" 2>/dev/null && pwd -P) || { echo "no such dir: $1" >&2; return 1; }
  while :; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d"; return 0; }
    [ "$d" = "/" ] && break
    d=$(dirname "$d")
  done
  echo "cannot locate repo root above $1 (no ancestor has yaas-triage/)" >&2
  return 1
}
_workspace_root() {
  local candidate
  for candidate in "${SIDEQUESTOR_WORKSPACE:-}" "${YAAS_WORKSPACE:-}" "${REPO_ROOT:-}"; do
    [ -n "$candidate" ] || continue
    if candidate=$(cd "$candidate" 2>/dev/null && pwd -P); then
      printf '%s' "$candidate"
      return 0
    fi
  done
  _repo_root "$SCRIPT_DIR"
}
REPO_ROOT="$(_workspace_root)" || exit 1
RUNTIME_ROOT="${SIDEQUESTOR_RUNTIME_ROOT:-${YAAS_RUNTIME_ROOT:-$(_repo_root "$SCRIPT_DIR")}}"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

FAIL=0
ok()    { [ "$QUIET" = "0" ] && printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m⚠\033[0m %s\n' "$1"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
section() { [ "$QUIET" = "0" ] && printf '\n\033[1m%s\033[0m\n' "$1"; }

# ── 1. Required commands ────────────────────────────────────────────────────
# The agent binary is whichever backend YAAS_AGENT selects, NOT always claude. doctor.sh
# used to hard-fail on a missing `claude` even with YAAS_AGENT=codex, which contradicted
# the README's multi-backend story and made a perfectly good install look broken.
section "Prerequisites"
AGENT="${SIDEQUESTOR_AGENT:-${YAAS_AGENT:-codex}}"
case "$AGENT" in
  claude) AGENT_BIN="claude" ;;
  codex)  AGENT_BIN="codex" ;;
  cursor) AGENT_BIN="cursor-agent" ;;
  *)      AGENT_BIN="$AGENT" ;;
esac
for cmd in "$AGENT_BIN" jq perl python3 security; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd present: $(command -v "$cmd")"
  else
    fail "$cmd not found in PATH"
  fi
done
[ "$AGENT" = "claude" ] || ok "agent backend: $AGENT (SIDEQUESTOR_AGENT) — checking $AGENT_BIN, not claude"

# Python floor. Checked explicitly because the failure is otherwise a TypeError on a
# `X | None` annotation deep inside a dispatch, which reads as a code bug rather than
# a version problem. zoneinfo and str.removeprefix put the real floor at 3.9.
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    ok "python3 is $PYV (need 3.9+)"
  else
    fail "python3 is $PYV — yaas needs 3.9 or newer (zoneinfo and str.removeprefix)"
  fi
fi

# Optional but common
for cmd in gws node; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd present (optional): $(command -v "$cmd")"
  else
    warn "$cmd not found in PATH (needed if you use Gmail/Coda)"
  fi
done

# ── 2. .env ─────────────────────────────────────────────────────────────────
section ".env config"
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  fail "$ENV_FILE missing — copy from .env.example and fill in"
else
  ok ".env present"
  # Source it in a subshell to catch syntax errors
  if ( set -a && source "$ENV_FILE" && set +a ) 2>/dev/null; then
    ok ".env parses cleanly"
  else
    fail ".env has syntax errors — try sourcing it manually for the message"
  fi

  set -a; source "$ENV_FILE" 2>/dev/null || true; set +a
  SLACK_CHECKERS_ENABLED="${SIDEQUESTOR_SLACK_CHECKERS_ENABLED:-${YAAS_SLACK_CHECKERS_ENABLED:-1}}"
  case "$SLACK_CHECKERS_ENABLED" in
    0|1) ok "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=$SLACK_CHECKERS_ENABLED" ;;
    *) fail "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=$SLACK_CHECKERS_ENABLED is invalid (expected 0 or 1)" ;;
  esac
  if [ "$SLACK_CHECKERS_ENABLED" = "0" ]; then
    SLACK_REQUIRED_VARS=""
    warn "local Slack checkers and reaction sweep disabled; slack_* watermarks are held"
  else
    SLACK_REQUIRED_VARS="SLACK_APP_ID SLACK_CLIENT_ID SLACK_WORKSPACE_NAME SLACK_WORKSPACE_DOMAIN"
  fi
  for var in $SLACK_REQUIRED_VARS SIDEQUESTOR_FROM_EMAIL; do
    legacy_var="${var/SIDEQUESTOR_/YAAS_}"
    if [ -n "${!var:-${!legacy_var:-}}" ]; then
      ok "$var set"
    else
      fail "$var empty in .env"
    fi
  done
  for var in CODA_API_KEY CODA_MCP_PATH; do
    if [ -n "${!var:-}" ]; then
      ok "$var set (Coda MCP enabled)"
    else
      warn "$var empty (Coda MCP disabled — fine if you don't use it)"
    fi
  done

  # env.example ships the SIDEQUESTOR_ names; reading only YAAS_ here reported the
  # DEFAULT for a correctly configured workspace instead of its real setting.
  CLAUDE_PERMISSION_MODE="${SIDEQUESTOR_CLAUDE_PERMISSION_MODE:-${YAAS_CLAUDE_PERMISSION_MODE:-${SIDEQUESTOR_WORKER_PERMISSION_MODE:-${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}}}}"
  ok "SIDEQUESTOR_CLAUDE_PERMISSION_MODE=$CLAUDE_PERMISSION_MODE"

  CODEX_PERMISSION_MODE="${SIDEQUESTOR_CODEX_PERMISSION_MODE:-${YAAS_CODEX_PERMISSION_MODE:-workspace-write}}"
  case "$CODEX_PERMISSION_MODE" in
    workspace-write|bypassPermissions)
      ok "SIDEQUESTOR_CODEX_PERMISSION_MODE=$CODEX_PERMISSION_MODE"
      ;;
    *)
      fail "SIDEQUESTOR_CODEX_PERMISSION_MODE=$CODEX_PERMISSION_MODE is invalid (expected workspace-write or bypassPermissions)"
      ;;
  esac
fi

# ── 3. Slack token in Keychain ──────────────────────────────────────────────
section "Slack credentials"
if [ "${SLACK_CHECKERS_ENABLED:-1}" = "0" ]; then
  ok "Slack OAuth token not required while local Slack checkers are disabled"
elif security find-generic-password -s slack-oauth-token-bundle -a yaas >/dev/null 2>&1; then
  CREDENTIAL_STATUS=$(python3 "$RUNTIME_ROOT/yaas-triage/surfaces/slack_credentials.py" status 2>/dev/null || true)
  CREDENTIAL_MODE=$(printf '%s' "$CREDENTIAL_STATUS" | jq -r '.mode // "error"' 2>/dev/null)
  CREDENTIAL_COMPLETE=$(printf '%s' "$CREDENTIAL_STATUS" | jq -r '.complete // false' 2>/dev/null)
  if [ "$CREDENTIAL_MODE" = "rotating" ] && [ "$CREDENTIAL_COMPLETE" = "true" ]; then
    ACCESS_REMAINING=$(printf '%s' "$CREDENTIAL_STATUS" | jq -r '.access_expires_in // 0')
    REFRESH_REMAINING=$(printf '%s' "$CREDENTIAL_STATUS" | jq -r '.refresh_expires_in // 0')
    if [ "$REFRESH_REMAINING" -le 0 ]; then
      fail "rotating Slack credential's refresh token has also expired — rerun bash \"$RUNTIME_ROOT/yaas-triage/setup/setup.sh\""
    elif [ "$ACCESS_REMAINING" -le 0 ]; then
      ok "rotating Slack credential bundle is complete (access token expired, will refresh on next use; refresh valid for about $(( REFRESH_REMAINING / 3600 ))h)"
    else
      ok "rotating Slack credential bundle is complete (access expires in about $(( ACCESS_REMAINING / 60 ))m)"
    fi
  else
    fail "Slack credential bundle is incomplete or unreadable — rerun bash \"$RUNTIME_ROOT/yaas-triage/setup/setup.sh\""
  fi
elif security find-generic-password -s slack-xoxp-token -a yaas >/dev/null 2>&1; then
  LEGACY_TOKEN=$(security find-generic-password -s slack-xoxp-token -a yaas -w 2>/dev/null || true)
  case "$LEGACY_TOKEN" in
    xoxp-*) ok "legacy long-lived Slack credential is installed" ;;
    xoxe.xoxp-*) fail "rotating Slack credential has no refresh token — rerun bash \"$RUNTIME_ROOT/yaas-triage/setup/setup.sh\"" ;;
    *) fail "Slack credential has an unsupported format — rerun bash \"$RUNTIME_ROOT/yaas-triage/setup/setup.sh\"" ;;
  esac
  unset LEGACY_TOKEN
else
  fail "Slack token not in Keychain — run bash \"$RUNTIME_ROOT/yaas-triage/setup/setup.sh\""
fi

# ── 4. State directory ──────────────────────────────────────────────────────
section "State directories"
for d in state state/quests state/quests/active state/triage logs; do
  if [ -d "$REPO_ROOT/$d" ]; then
    ok "$d/ exists"
  else
    warn "$d/ missing (will be created on first run)"
  fi
done

# ── 5. launchd job ──────────────────────────────────────────────────────────
section "launchd"
PLIST="$HOME/Library/LaunchAgents/com.yaas.triage.plist"
if [ -f "$PLIST" ]; then
  ok "plist installed at $PLIST"
else
  fail "plist not installed — run \`sq start\`"
fi

if launchctl list com.yaas.triage >/dev/null 2>&1; then
  # Extract just the number. The old `sed 's/.*= //; s/;//'` was greedy and only
  # dropped the FIRST semicolon, so anything trailing on the line came along for the
  # ride ("512 };") and then matched no case arm below — the interpretation silently
  # degraded to "Unexpected". Pulling the digits out directly cannot do that.
  LC_OUT=$(launchctl list com.yaas.triage 2>/dev/null)
  # Split on ';' so each key is its own record no matter how the lines are packed.
  # Without that, grabbing "the last number on the matching line" picks up a
  # neighbouring key's value instead.
  _num() { printf '%s\n' "$LC_OUT" | tr ';' '\n' | grep "\"$1\"" | head -1 \
             | grep -oE -- '-?[0-9]+' | tail -1; }
  STATUS=$(_num LastExitStatus); PID=$(_num PID)
  [ -n "$STATUS" ] || STATUS="unknown"
  [ -n "$PID" ] || PID="none"
  ok "launchd job loaded (PID=$PID, LastExitStatus=$STATUS)"
  case "$STATUS" in
    0)
      ok "Last triage tick exited cleanly"
      ;;
    36608)
      # 143 << 8 — SIGTERM, expected when the watchdog kills a worker
      ok "Last exit was SIGTERM (143) — handled normally by watchdog logic"
      ;;
    15|-15)
      # Raw SIGTERM, which is what `launchctl kickstart -k` leaves behind. Restarting
      # the job is a routine thing to do (triage-loop.sh edits require it), so
      # reporting it as "unexpected" was noise.
      ok "Last exit was SIGTERM (15) — normal after a launchctl kickstart -k"
      ;;
    512)
      # 2 << 8 — set -eu aborted
      fail "Last exit was code 2 — a bad numeric .env knob makes the orchestrator refuse to run. Run 'sq tick' manually to see the error."
      ;;
    *)
      warn "Unexpected LastExitStatus=$STATUS — check logs/triage.err.log"
      ;;
  esac
else
  fail "launchd job not loaded — run \`sq start\`"
fi

# ── 6. Runtime liveness — delegated ─────────────────────────────────────────
# This used to re-implement "how long since the last tick", with its own date
# parsing and its own thresholds. health-monitor.py does that continuously, on its
# own launchd job, with alerting and a published verdict — so a copy here was a
# worse version of a better mechanism, and a copy that only ran when someone
# remembered to.
#
# doctor answers "is this machine configured"; health answers "is it working now".
section "Runtime health"
HEALTH="$REPO_ROOT/state/health-status.json"
if [ -f "$HEALTH" ]; then
  if [ "$(jq -r '.healthy // false' "$HEALTH" 2>/dev/null)" = "true" ]; then
    ok "health-monitor reports healthy ($(jq -r '.ts // "?"' "$HEALTH"))"
  else
    fail "health-monitor reports problems: $(jq -r '[.problems[].headline] | join("; ")' "$HEALTH" 2>/dev/null)"
    [ "$QUIET" = "0" ] && echo "    Detail: python3 \"$RUNTIME_ROOT/yaas-triage/ops/health-monitor.py\" --json"
  fi
else
  warn "no health-status.json — install the heartbeat: \`sq start\`"
fi

# ── 7. Quest folder sanity ──────────────────────────────────────────────────
section "Quest folders"
QUEST_COUNT=0
for q in "$REPO_ROOT/state/quests/active/"*/; do
  [ -d "$q" ] || continue
  QUEST_COUNT=$((QUEST_COUNT + 1))
  qid=$(basename "$q")
  for f in meta.json watch.json context.md timeline.ndjson; do
    if [ ! -f "$q/$f" ]; then
      fail "$qid missing $f"
    fi
  done
  if [ -f "$q/meta.json" ]; then
    meta_id=$(jq -r '.id' "$q/meta.json" 2>/dev/null)
    if [ "$meta_id" != "$qid" ]; then
      fail "$qid: meta.json id=\"$meta_id\" does not match folder name"
    fi
  fi
done
ok "$QUEST_COUNT active quest(s) checked"

# ── Summary ─────────────────────────────────────────────────────────────────
section "Summary"
if [ "$FAIL" -eq 0 ]; then
  echo "  All checks passed."
  exit 0
else
  echo "  $FAIL check(s) failed. Address the ✗ items above."
  exit 1
fi
