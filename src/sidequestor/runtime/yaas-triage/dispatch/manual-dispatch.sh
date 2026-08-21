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

# manual-dispatch.sh — legacy utility for running one direct worker dispatch
# against a single quest with a free-text instruction, WITHOUT waiting for
# Slack/email activity. Dashboard instructions now use the durable approval
# queue. This utility shares the original shell orchestrator's single-instance lock (logs/triage.lock) so a
# manual run and a triage tick can never race on watch.json, and reuses the
# exact worker-latest.log streaming pipeline so the dashboard's existing live
# panel shows progress with no extra wiring.
#
# Unlike the original shell orchestrator, this NEVER advances any watermark — it is a direct
# instruction, not a response to new inbound activity. The worker may still
# append new watches[] entries per CLAUDE.md §3a; existing entries are left
# untouched, exactly as in a normal Mode A run.
#
# Usage:   manual-dispatch.sh <quest_id> <instruction-text>
# Exit:    0   worker ran (exit code logged; check worker-latest.log)
#          75  lock held by a triage tick or another manual run — try again
#          2   bad usage / unknown quest

set -uo pipefail

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
REPO_ROOT="$(_repo_root "$SCRIPT_DIR")" || exit 1

QUEST_ID="${1:-}"
INSTRUCTION="${2:-}"
if [ -z "$QUEST_ID" ] || [ -z "$INSTRUCTION" ]; then
  echo "usage: manual-dispatch.sh <quest_id> <instruction-text>" >&2
  exit 2
fi

# Load .env for YAAS_AGENT + secrets, exactly as the original shell orchestrator does (errexit off
# while sourcing — a malformed .env line must not abort us).
if [ -f "$REPO_ROOT/.env" ]; then
  set +e; set -a; source "$REPO_ROOT/.env"; set +a
fi

QUESTS_DIR="$REPO_ROOT/state/quests/active"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/triage.log"
RUN_LOG="$REPO_ROOT/state/run-log.ndjson"
YAAS_AGENT="${YAAS_AGENT:-claude}"; export YAAS_AGENT
mkdir -p "$LOG_DIR"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE" >&2; }

QUEST_DIR="$QUESTS_DIR/$QUEST_ID"
# Reject anything that isn't a real active-quest folder (also blocks traversal:
# a "../" id won't resolve to a child of the active dir).
case "$QUEST_ID" in */*|..|.) log "MANUAL — rejected quest id '$QUEST_ID'"; exit 2;; esac
if [ ! -d "$QUEST_DIR" ]; then
  log "MANUAL — unknown quest '$QUEST_ID' (no folder under active/)"; exit 2
fi

# ── Share triage's single-instance lock (non-blocking) ──────────────────────
LOCKFILE="$LOG_DIR/triage.lock"
HOLDERFILE="$LOG_DIR/triage.lock.holder"
exec 9>>"$LOCKFILE"
if ! perl -e 'use Fcntl qw(:flock); exit !flock(STDIN, LOCK_EX|LOCK_NB)' 0<&9; then
  HOLDER=$(cat "$HOLDERFILE" 2>/dev/null || echo unknown)
  log "MANUAL SKIP — triage/worker already running (holder pid: $HOLDER)."
  exit 75
fi
echo "$$" > "$HOLDERFILE"

cd "$REPO_ROOT"
log "MANUAL DISPATCH — quest=$QUEST_ID backend=$YAAS_AGENT"

NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
INSTR_JSON=$(printf '%s' "$INSTRUCTION" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
echo "{\"ts\":\"$NOW_UTC\",\"event\":\"manual_dispatch\",\"quest\":\"$QUEST_ID\",\"instruction\":$INSTR_JSON}" >> "$RUN_LOG"

WORKER_TIMEOUT=1800  # keep in sync with the original shell orchestrator WORKER_TIMEOUT
CLAUDE_PERMISSION_MODE="${YAAS_CLAUDE_PERMISSION_MODE:-${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}}"
CODEX_PERMISSION_MODE="${YAAS_CODEX_PERMISSION_MODE:-workspace-write}"

# The prompt keeps the worker in Mode A / Quest Activation Protocol so all the
# tone, send-authorization, watch.json and logging rules apply, but leads with
# the human instruction as the thing to act on this run.
WORKER_PROMPT="Yaas worker dispatch (manual, dashboard-initiated). Dirty target: $QUEST_ID. This is NOT triggered by new Slack/email activity — it is a direct instruction from the operator. Follow the Quest Activation Protocol for this quest (read context.md first; read other files only when you need them). ALL normal rules apply: draft-vs-send authorization (§3, §3d), never modify existing watch.json entries, act-first-then-report (§3b), log every outbound action to timeline.ndjson via slack-send.py. MANUAL INSTRUCTION TO ACT ON: $INSTRUCTION. NO ACK LEDGER for this dispatch: there is no run_id and this run advances no watermarks, so skip § 4a entirely and do NOT call ack-watch.py. ACT SILENTLY: emit no narration between tool calls. OUTPUT CONTRACT: emit the summary only if something material happened; keep it under 8 lines."

# The pipeline, watchdog and process-tree kill live in run-agent.py, shared with
# the original shell orchestrator. This file used to carry its own copy of all three, untested, so a fix to
# either had to be made twice and was verified once.
AGENT_JSON=$(python3 "$SCRIPT_DIR/run-agent.py" \
  --prompt "$WORKER_PROMPT" --label "$QUEST_ID" --timeout "$WORKER_TIMEOUT" \
  --header "Dirty targets: $QUEST_ID (manual)" \
  --header "Manual instruction: $INSTRUCTION" 2>>"$LOG_FILE") || true
EXIT=$(printf '%s' "$AGENT_JSON" | jq -r '.exit // 1' 2>/dev/null || echo 1)
case "$EXIT" in ''|*[!0-9]*) EXIT=1 ;; esac
WORKER_LOG=$(printf '%s' "$AGENT_JSON" | jq -r '.log // ""' 2>/dev/null || echo "")

log "MANUAL DISPATCH done — quest=$QUEST_ID worker exit=$EXIT (log: $WORKER_LOG)"
echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"manual_dispatch_done\",\"quest\":\"$QUEST_ID\",\"exit\":$EXIT}" >> "$RUN_LOG"
exit 0
