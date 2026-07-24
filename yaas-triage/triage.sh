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

# triage.sh — yaas idle triage (v2, per-quest aware)
#
# Loops through every active quest's watch.json, checks Slack for new activity
# in the specific threads/reactions/DMs each quest is watching. If nothing new
# anywhere, advances each clean quest's watermark and exits — $0 cost. If any
# quest has new activity, dispatches the Sonnet worker with the list of dirty
# quest IDs.
#
# Termination safety:
#   - Clean quests' watermarks are advanced at end of triage.sh
#   - Dirty quests' watermarks are advanced ONLY by the worker on successful
#     completion. If triage or worker dies mid-run, the next tick re-sees the
#     same activity.
#
# Env:
#   DRY_RUN=1     Skip worker dispatch even if activity is found.
#   VERBOSE=1     Print per-quest check details to stderr.
#   YAAS_WORKER_PERMISSION_MODE  Passed to claude --permission-mode (default:
#                  acceptEdits). Set in repo-root .env.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load personal secrets (CODA_API_KEY, YAAS_FROM_EMAIL, etc.)
# errexit must be OFF while sourcing: under macOS bash 3.2, a malformed line
# in .env (e.g. a var name with a hyphen -> "command not found") aborts the
# whole script at the source statement even inside a && chain with || true.
# Incident: 2026-06-11, TEST_EVM_NON-WHITELIST_VASP_1111 killed every tick.
# shellcheck source=../.env
if [ -f "$REPO_ROOT/.env" ]; then
  set +e
  set -a
  source "$REPO_ROOT/.env"
  set +a
  set -e
fi
QUESTS_DIR="$REPO_ROOT/state/quests/active"
TRIAGE_STATE="$REPO_ROOT/state/triage/last-run.json"
RUN_LOG="$REPO_ROOT/state/run-log.ndjson"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/triage.log"
export MCP_CALL="$SCRIPT_DIR/mcp-call.sh"
export GWS_BIN=$(command -v gws 2>/dev/null || echo "/opt/homebrew/bin/gws")

# Which agent backend the worker dispatch runs on (claude|codex|cursor).
# Set in repo-root .env. dispatch-agent.sh reads this plus the per-backend
# model env vars (YAAS_CLAUDE_MODEL / YAAS_CODEX_MODEL / YAAS_CURSOR_MODEL).
YAAS_AGENT="${YAAS_AGENT:-claude}"
export YAAS_AGENT

mkdir -p "$LOG_DIR" "$QUESTS_DIR" "$(dirname "$TRIAGE_STATE")"

log()  { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE" >&2; }
slog() { printf '%s  %s\n' "$(TZ=Asia/Singapore date +%Y-%m-%dT%H:%M:%S+08:00)" "$*"; }
vlog() { [ "${VERBOSE:-0}" = "1" ] && log "  $*" || true; }

# Cheap pre-dispatch Slack reachability probe via mcp-call.sh (curl + Keychain
# token). Returns 0 if Slack answered, non-zero otherwise. This is the
# backend-agnostic replacement for the Claude-only post-run .mcp_servers guard:
# Codex/Cursor emit no server-status event, so we verify Slack is up BEFORE
# spending a dispatch. If Slack is down we skip the tick (watermarks preserved).
slack_health_ok() {
  # Healthy iff mcp-call.sh exits 0. Its exit code already encodes real failure:
  # 1 = curl/network or token issue, 2 = JSON-RPC .error from Slack. Do NOT also
  # require non-empty output — a search that legitimately returns zero results
  # yields empty text on a perfectly healthy, authed Slack, and treating that as
  # "down" would skip the dispatch for no reason (false negative). Worst case a
  # rare non-JSON 5xx reads as "up" and the wasted dispatch self-corrects.
  "$MCP_CALL" slack_search_public_and_private '{"query":"yaas-health-ping","limit":1}' >/dev/null 2>&1
}

# ── Single-instance lock ────────────────────────────────────────────────────
# launchd fires every StartInterval seconds regardless of whether the previous
# run finished. A worker dispatch can take minutes, so concurrent triage runs
# would race on watch.json and the RUN_LOG. We take an exclusive non-blocking
# flock on a lockfile — if another triage holds it, exit 0 (skip this tick,
# let launchd try again at its next interval).
#
# macOS doesn't ship flock(1); we use Perl's Fcntl::flock, which is on every
# system Perl install. The OS auto-releases the lock when this process exits,
# so no trap/cleanup is needed.
LOCKFILE="$LOG_DIR/triage.lock"
# Open FD 9 for read so holder can still be read while we hold the exclusive lock.
# Write our PID to a SEPARATE file so contenders can see it without racing the lock fd.
HOLDERFILE="$LOG_DIR/triage.lock.holder"
exec 9>>"$LOCKFILE"
if ! perl -e 'use Fcntl qw(:flock); exit !flock(STDIN, LOCK_EX|LOCK_NB)' 0<&9; then
  HOLDER=$(cat "$HOLDERFILE" 2>/dev/null || echo "unknown")
  log "SKIP — previous triage still running (holder pid: $HOLDER). Will retry next tick."
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_skip_locked\",\"holder_pid\":\"$HOLDER\"}" >> "$RUN_LOG"
  exit 0
fi
# Lock acquired — record our PID for future contenders to read
echo "$$" > "$HOLDERFILE"

# ── Post-run hook — runs on every exit except lock-contention ────────────────
# Covers all code paths (no-quests early exit, idle, dry-run, post-dispatch).
# rotate-logs.sh is self-gated (23h sentinel) so calling it every tick is safe.
# notify.sh uses its own watermark — fires nothing if there are no new events.
_on_exit() {
  rm -f "${TMP_RESULTS:-}"
  bash "$SCRIPT_DIR/rotate-logs.sh"    2>>"$LOG_FILE" || true
  bash "$SCRIPT_DIR/notify.sh"         2>>"$LOG_FILE" || true
  bash "$SCRIPT_DIR/sync-yaas-v2.sh"   2>>"$LOG_FILE" || true
}
trap '_on_exit' EXIT

NOW_EPOCH=$(date +%s)
NOW_TS=$(python3 -c "import time; print(f'{time.time():.6f}')")
NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Per-checker watermark lag map ────────────────────────────────────────────
# Each checkers/<type>.lag file contains an integer seconds lag. Watermarks for
# that type advance to NOW_TS - lag instead of NOW_TS, giving slow-indexing
# sources (e.g. Gmail) time to catch up before a clean tick claims "nothing new".
LAG_MAP="{}"
for _lagfile in "$SCRIPT_DIR/checkers/"*.lag; do
  [ -f "$_lagfile" ] || continue
  _type=$(basename "$_lagfile" .lag)
  _lag=$(tr -d '[:space:]' < "$_lagfile")
  # A non-integer lag file would fail --argjson and, under set -e, abort the
  # whole tick (all dispatch stops). Skip a malformed file instead.
  case "$_lag" in ''|*[!0-9]*) continue ;; esac
  LAG_MAP=$(printf '%s' "$LAG_MAP" | jq --arg t "$_type" --argjson l "$_lag" '. + {($t): $l}')
done

# ── Gather quests ───────────────────────────────────────────────────────────
shopt -s nullglob
QUEST_DIRS=("$QUESTS_DIR"/*/)
shopt -u nullglob

QUEST_COUNT=${#QUEST_DIRS[@]}
log "Triage starting. Active quests: $QUEST_COUNT"

if [ "$QUEST_COUNT" = "0" ]; then
  # No active quests — fully idle with nothing to check — fully idle with nothing to check.
  python3 -c "
import json, sys, os
p = '$TRIAGE_STATE'
d = json.load(open(p)) if os.path.exists(p) else {}
d['last_triage_completed_utc'] = '$NOW_UTC'
d['runs_total'] = d.get('runs_total', 0) + 1
d['runs_idle']  = d.get('runs_idle', 0) + 1
json.dump(d, open(p, 'w'), indent=2)
"
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_idle_no_quests\"}" >> "$RUN_LOG"
  log "IDLE — no active quests. Exit 0."
  slog "Run OK — idle. 0 active quests, 0 checks performed."
  exit 0
fi

# ── For each quest, perform its watch.json checks in parallel ───────────────
check_quest() {
  # Prints "QUEST_ID\tclean|dirty\treason" to stdout. Runs sequentially within
  # one quest (so its watch.json lookups are ordered), but multiple quests can
  # run in parallel via `&`.
  #
  # Dispatches to checkers/<type>.py for each entry in watches[]. Adding a new
  # channel type requires only dropping a new script in checkers/ — no changes
  # to this function.
  local quest_dir="$1"
  local qid; qid=$(basename "$quest_dir")
  local watch="$quest_dir/watch.json"

  if [ ! -f "$watch" ]; then
    echo -e "${qid}\tclean\tno_watch_file"
    return 0
  fi

  local watch_count
  watch_count=$(jq '.watches // [] | length' "$watch")
  local i=0
  while [ "$i" -lt "$watch_count" ]; do
    local entry type checker parsed new_count preview
    entry=$(jq -c ".watches[$i]" "$watch")
    type=$(jq -r '.type // "unknown"' <<< "$entry")
    checker="$SCRIPT_DIR/checkers/$type.py"
    if [ ! -x "$checker" ]; then
      vlog "[$qid] no checker for type '$type', skipping"
      i=$((i + 1)); continue
    fi
    vlog "[$qid] checking type=$type"
    parsed=$(python3 "$checker" "$entry" 2>/dev/null || echo "error|checker failed to execute")
    new_count="${parsed%%|*}"
    preview="${parsed#*|}"
    if [ "$new_count" = "ratelimited" ]; then
      # Transient Slack rate-limit — NOT dirty. Marking dirty here would burn a
      # full Opus dispatch that finds nothing, and the rate-limit is usually
      # self-inflicted by checker volume, so dispatching makes it worse. The
      # checker already held its watermark; skip this quest for this tick and
      # let it retry next tick once the tier recovers. (Real incident
      # 2026-07-24: ratelimited checks read as `error`→dirty and re-fired the
      # same 6 quests every 60s for ~13.5h, burning >$1k with zero output.)
      echo -e "${qid}\tskip\t[$type] $preview"
      return 0
    fi
    if [ "$new_count" = "error" ]; then
      echo -e "${qid}\tdirty\t[$type] error — $preview"
      return 0
    fi
    if [ "${new_count:-0}" -gt 0 ]; then
      echo -e "${qid}\tdirty\t[$type] $new_count new — \"$preview\""
      return 0
    fi
    i=$((i + 1))
  done

  echo -e "${qid}\tclean\tall_checks_passed"
}

# Run quest checks in parallel (up to 8 at a time)
TMP_RESULTS=$(mktemp)
# (trap set earlier at post-lock — covers TMP_RESULTS via ${TMP_RESULTS:-})

MAX_PARALLEL=8
pids=()
for qd in "${QUEST_DIRS[@]}"; do
  check_quest "$qd" >> "$TMP_RESULTS" &
  pids+=($!)
  # Throttle
  if [ "${#pids[@]}" -ge "$MAX_PARALLEL" ]; then
    wait "${pids[0]}"
    pids=("${pids[@]:1}")
  fi
done
wait

# ── Analyze ─────────────────────────────────────────────────────────────────
DIRTY_QUESTS=()
CLEAN_QUESTS=()
SKIPPED_QUESTS=()
while IFS=$'\t' read -r qid status reason; do
  if [ "$status" = "dirty" ]; then
    DIRTY_QUESTS+=("$qid")
    log "DIRTY: $qid — $reason"
  elif [ "$status" = "skip" ]; then
    # Transient rate-limit (see check_quest). Neither dispatched nor watermark-
    # advanced: it stays out of CLEAN_QUESTS so the watermark is held, and out
    # of DIRTY_QUESTS so no Opus dispatch fires. Retries next tick.
    SKIPPED_QUESTS+=("$qid")
    log "SKIP: $qid — $reason"
  else
    CLEAN_QUESTS+=("$qid")
    vlog "CLEAN: $qid — $reason"
  fi
done < "$TMP_RESULTS"

DIRTY_COUNT=${#DIRTY_QUESTS[@]}
CLEAN_COUNT=${#CLEAN_QUESTS[@]}
SKIPPED_COUNT=${#SKIPPED_QUESTS[@]}

# ── Global reaction sweep ──────────────────────────────────────────────────
# Checks :claude-intensifies:, :writing_hand:, :floppy_disk: reactions applied by
# the user. Uses a rolling 60-day window (Slack's after: applies to message
# post time, so we set it wide enough to catch reactions added to recent-ish
# messages). Diffs against global state files in state/*_replied|saved.json —
# anything not yet in the state file is considered dirty.
#
# Writes state/triage/pending_reactions.json (transient) if any reactions are
# new, for the worker to consume. Worker appends processed ts to the state
# files after acting.
PENDING_REACTIONS="$REPO_ROOT/state/triage/pending_reactions.json"
CUTOFF_DATE=$(date -u -v-60d +%Y-%m-%d 2>/dev/null || date -u -d "60 days ago" +%Y-%m-%d)
REACTIONS_DIRTY=0

python3 "$SCRIPT_DIR/checkers/reactions.py" "$MCP_CALL" "$CUTOFF_DATE" "$REPO_ROOT" "$PENDING_REACTIONS" 2>&1 || log "REACTIONS checker failed to execute (non-fatal) — reaction sweep skipped this cycle"

# Parse sweep result
if [ -f "$PENDING_REACTIONS" ]; then
  REACTIONS_DIRTY=1
  log "DIRTY: reactions — pending in $PENDING_REACTIONS"
else
  vlog "CLEAN: no new reactions"
fi

# ── Advance clean quest watermarks ──────────────────────────────────────────
for qid in "${CLEAN_QUESTS[@]}"; do
  watch="$QUESTS_DIR/$qid/watch.json"
  [ -f "$watch" ] || continue
  TMP=$(mktemp)
  jq --arg now "$NOW_TS" --argjson lags "$LAG_MAP" '
    .watches //= [] |
    .watches[] |= (.last_checked_ts = (($now | tonumber) - ($lags[.type] // 0) | tostring))
  ' "$watch" > "$TMP" && mv "$TMP" "$watch"
  vlog "Advanced watermark for $qid"
done

# ── Update triage run counters ────────────────────────────────────────────────
python3 -c "
import json, os
p = '$TRIAGE_STATE'
d = json.load(open(p)) if os.path.exists(p) else {}
d['last_triage_completed_utc'] = '$NOW_UTC'
d['runs_total']     = d.get('runs_total', 0) + 1
d['quests_checked'] = $QUEST_COUNT
d['quests_dirty']   = $DIRTY_COUNT
d['quests_clean']   = $CLEAN_COUNT
d['quests_skipped'] = $SKIPPED_COUNT
if $DIRTY_COUNT == 0:
    d['runs_idle'] = d.get('runs_idle', 0) + 1
else:
    d['runs_dispatched'] = d.get('runs_dispatched', 0) + 1
    d['last_dispatch_utc'] = '$NOW_UTC'
json.dump(d, open(p, 'w'), indent=2)
"

# ── Retire stale slack_thread watches per-quest ─────────────────────────────
# Each quest's meta.json may set "retire_slack_threads_after_days":
#   <positive int> — drop slack_thread watches whose parent thread_ts is older than N days
#   0 / false / "never" / null — never retire (use for partner conversations etc.)
#   missing — defaults to 30 days
# Other watch types (slack_channel, slack_dm, schedule, email) are never retired here —
# they have semantic permanence.
RETIRE_DEFAULT_DAYS="${YAAS_RETIRE_DEFAULT_DAYS:-30}"
NOW_EPOCH_INT=$(date +%s)
for qd in "${QUEST_DIRS[@]}"; do
  qid=$(basename "$qd")
  meta="$qd/meta.json"
  watch="$qd/watch.json"
  [ -f "$meta" ] && [ -f "$watch" ] || continue

  _days=$(jq -r "(.retire_slack_threads_after_days // $RETIRE_DEFAULT_DAYS) | tostring" "$meta")
  case "$_days" in
    0|false|never|null|"") continue ;;
    # Reject any non-integer. _days flows into $(( )) below, where bash
    # evaluates array subscripts — a poisoned meta.json value like
    # "1[$(cmd)]" would execute cmd in this (unsandboxed) triage process.
    *[!0-9]*) continue ;;
  esac

  _cutoff=$((NOW_EPOCH_INT - _days * 86400))
  _retired=$(jq --argjson cutoff "$_cutoff" '
    [.watches[]? | select(
      .type == "slack_thread"
      and ((.thread_ts // "0") | tonumber) < $cutoff
    )] | length
  ' "$watch")

  if [ "${_retired:-0}" -gt 0 ]; then
    TMP=$(mktemp)
    jq --argjson cutoff "$_cutoff" '
      .watches = [.watches[]? | select(
        .type != "slack_thread"
        or ((.thread_ts // "0") | tonumber) >= $cutoff
      )]
    ' "$watch" > "$TMP" && mv "$TMP" "$watch"
    log "Retired $_retired stale slack_thread watch(es) from $qid (thread_ts older than ${_days}d)"
  fi
done

# ── Retire completed approval watches ────────────────────────────────────────
# Drop approval watch entries whose corresponding pending-approvals.json item
# has status "executed" or "cancelled" — they will never fire again.
_APPROVALS_FILE="$REPO_ROOT/state/pending-approvals.json"
if [ -f "$_APPROVALS_FILE" ]; then
  for qd in "${QUEST_DIRS[@]}"; do
    watch="$qd/watch.json"
    [ -f "$watch" ] || continue
    _has=$(jq '[.watches[]? | select(.type == "approval")] | length' "$watch" 2>/dev/null || echo 0)
    [ "${_has:-0}" -gt 0 ] || continue
    TMP=$(mktemp)
    python3 - "$watch" "$_APPROVALS_FILE" "$TMP" <<'PYEOF' && mv "$TMP" "$watch" || { rm -f "$TMP"; true; }
import json, sys
watch_path, approvals_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
watch    = json.load(open(watch_path))
approvals = json.load(open(approvals_path))
done_ids  = {i["id"] for i in approvals.get("items", [])
             if i.get("status") in ("executed", "cancelled")}
before = len(watch.get("watches", []))
watch["watches"] = [
    w for w in watch.get("watches", [])
    if not (w.get("type") == "approval" and w.get("approval_id") in done_ids)
]
after = len(watch["watches"])
json.dump(watch, open(out_path, "w"), indent=2)
if before != after:
    print(f"Retired {before - after} approval watch(es) from {watch_path}")
PYEOF
  done
fi

# ── Prune reaction state files (keep newest 1000 timestamps) ─────────────────
for _state in "$REPO_ROOT/state/claude_intensifies_replied.json" \
              "$REPO_ROOT/state/writing_hand_replied.json" \
              "$REPO_ROOT/state/floppy_disk_saved.json" \
              "$REPO_ROOT/state/incoming_envelope_adopted.json"; do
  [ -f "$_state" ] || continue
  _key=$(jq -r 'keys_unsorted[0]' "$_state")  # replied_timestamps or saved_timestamps
  _count=$(jq --arg k "$_key" '.[$k] | length' "$_state")
  if [ "${_count:-0}" -gt 1000 ]; then
    TMP=$(mktemp)
    jq --arg k "$_key" '.[$k] = (.[$k] | sort | .[-1000:])' "$_state" > "$TMP" && mv "$TMP" "$_state"
    log "Pruned $_state to 1000 entries (was $_count)"
  fi
done

# ── Prune old per-dispatch worker logs ──────────────────────────────────────
# Configurable via YAAS_LOG_RETAIN_DAYS in .env (default 14). 0 = disabled.
# Only worker-*.{log,ndjson} are deleted — append-only triage.log/out/err are kept.
LOG_RETAIN_DAYS="${YAAS_LOG_RETAIN_DAYS:-14}"
if [ "${LOG_RETAIN_DAYS:-0}" -gt 0 ] 2>/dev/null; then
  _pruned=$(find "$LOG_DIR" -maxdepth 1 -type f \( -name "worker-*.log" -o -name "worker-*.ndjson" \) -mtime "+$LOG_RETAIN_DAYS" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${_pruned:-0}" -gt 0 ]; then
    find "$LOG_DIR" -maxdepth 1 -type f \( -name "worker-*.log" -o -name "worker-*.ndjson" \) -mtime "+$LOG_RETAIN_DAYS" -delete 2>/dev/null || true
    log "Pruned $_pruned worker log file(s) older than ${LOG_RETAIN_DAYS}d"
  fi
fi

# ── Decide ──────────────────────────────────────────────────────────────────
if [ "$DIRTY_COUNT" = "0" ] && [ "$REACTIONS_DIRTY" = "0" ]; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_idle\",\"quests_checked\":$QUEST_COUNT,\"quests_skipped\":$SKIPPED_COUNT}" >> "$RUN_LOG"
  log "IDLE — $QUEST_COUNT quest(s) checked, 0 dirty, $SKIPPED_COUNT rate-limit skip(s), 0 new reactions. Watermarks advanced (skipped quests held)."
  slog "Run OK — idle. $QUEST_COUNT quest(s) swept, 0 activity, $SKIPPED_COUNT rate-limit skip(s)."
  exit 0
fi

# Build the dispatch target list (quests + optional synthetic "reactions")
DISPATCH_TARGETS=("${DIRTY_QUESTS[@]+"${DIRTY_QUESTS[@]}"}")
if [ "$REACTIONS_DIRTY" = "1" ]; then
  DISPATCH_TARGETS+=("reactions")
fi

TARGETS_JSON=$(printf '%s\n' "${DISPATCH_TARGETS[@]}" | jq -R . | jq -sc .)
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_dirty_dry_run\",\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
  log "DRY_RUN=1 — would dispatch for ${DISPATCH_TARGETS[*]}. Watermarks of dirty quests NOT advanced; pending reactions retained."
  slog "[DRY RUN] Would dispatch worker for: ${DISPATCH_TARGETS[*]}"
  exit 0
fi

# ── Pre-dispatch Slack health gate ───────────────────────────────────────────
# Determine whether this dispatch actually needs Slack (a "reactions" target, or
# any dirty quest with a slack_* watch). If it does, ping Slack once before
# spending the dispatch. On failure, skip the tick WITHOUT advancing dirty
# watermarks or clearing pending reactions — the next tick re-surfaces the same
# activity. This protects every backend (esp. Codex/Cursor, which lack the
# post-run .mcp_servers status signal the Claude guard below relies on).
SLACK_NEEDED=0
for _tgt in "${DISPATCH_TARGETS[@]}"; do
  if [ "$_tgt" = "reactions" ]; then SLACK_NEEDED=1; break; fi
  _w="$QUESTS_DIR/$_tgt/watch.json"
  [ -f "$_w" ] || continue
  if [ "$(jq '[.watches[]? | select(.type | type=="string" and startswith("slack_"))] | length' "$_w" 2>/dev/null || echo 0)" -gt 0 ]; then
    SLACK_NEEDED=1; break
  fi
done
if [ "$SLACK_NEEDED" = "1" ] && ! slack_health_ok; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_slack_down\",\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
  log "SLACK DOWN — pre-dispatch ping failed and Slack is needed for [${DISPATCH_TARGETS[*]}]. Skipping dispatch; watermarks preserved, reactions retained. Retrying next tick."
  slog "Run OK — Slack unreachable, dispatch skipped (will retry)."
  exit 0
fi

# ── Dispatch worker with dirty targets ──────────────────────────────────────
#
# Transaction boundary: watermarks for DIRTY quests are advanced HERE, AFTER
# the worker exits successfully. If the worker dies or exits non-zero, dirty
# quests' watermarks are left untouched, and pending_reactions.json is left
# in place, so the next triage tick re-surfaces the same activity.
log "DISPATCH — invoking yaas worker (backend=$YAAS_AGENT) for: ${DISPATCH_TARGETS[*]}"
TARGET_LIST=$(printf '%s\n' "${DISPATCH_TARGETS[@]}" | paste -sd',' -)
echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_dispatch\",\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
slog "Run OK — ${#DISPATCH_TARGETS[@]} dirty target(s): ${DISPATCH_TARGETS[*]}. Dispatching worker..."

cd "$REPO_ROOT"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORKER_LOG="$LOG_DIR/worker-$STAMP.log"       # human-readable (tail-friendly)
WORKER_NDJSON="$LOG_DIR/worker-$STAMP.ndjson" # raw stream-json (metrics source)
ln -sf "$(basename "$WORKER_LOG")"    "$LOG_DIR/worker-latest.log"
ln -sf "$(basename "$WORKER_NDJSON")" "$LOG_DIR/worker-latest.ndjson"
log "Worker log → $WORKER_LOG (raw: $WORKER_NDJSON)"
{
  echo "=== Worker dispatch $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "Dirty targets: $TARGET_LIST"
  [ "$REACTIONS_DIRTY" = "1" ] && echo "Pending reactions: state/triage/pending_reactions.json"
  echo "========================================================"
} > "$WORKER_LOG"

WORKER_START=$(date +%s)
WORKER_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)  # boundary for post-run blocked-event scan
WORKER_TIMEOUT=1800  # 30 min; normal workers finish in <3 min, but a live
                     # sandbox retest (on-chain sends + payment propagation) can
                     # run long — 900s was killing those mid-run (exit 124),
                     # re-dispatching, and never completing (livelock).
WORKER_PERMISSION_MODE="${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}"

# Recursive process-tree killer — needed to terminate claude's background subprocesses
# which keep pipe FDs open and prevent the pipeline from exiting cleanly.
_kill_tree() {
  local _p=$1 _sig=${2:-TERM}
  local _ch
  _ch=$(pgrep -P "$_p" 2>/dev/null) || true
  for _c in $_ch; do _kill_tree "$_c" "$_sig"; done
  kill -"$_sig" "$_p" 2>/dev/null || true
}

# Preserve PIPESTATUS[0] (claude's exit code) across the subshell boundary.
_EXITFILE=$(mktemp)

# The worker prompt. Backend-neutral: each agent loads its own rules file
# (CLAUDE.md / AGENTS.md) from the repo root; this only names the dirty targets
# and the run discipline.
WORKER_PROMPT="Yaas worker dispatch: dirty targets: $TARGET_LIST. For each target, pick the matching path in your rules file: quest IDs → Quest Activation Protocol; 'reactions' → Reactions Fast Path (SELF-CONTAINED; do NOT read any quest folder). For quest IDs, read ONLY context.md first; read meta.json/watch.json/timeline.ndjson only when you actually need them to act. DO NOT modify existing watch.json entries — appending new watches[] entries per § 3a is the only allowed watch.json write. ACT SILENTLY: emit NO text between tool calls — no 'Reading X' or 'I need to check Y' narration. Batch independent reads/edits into a single turn using parallel tool_use blocks whenever possible. OUTPUT CONTRACT: emit the summary ONLY if something material happened (message sent, draft created, state changed, quest status changed). If nothing material happened — just exit with no text. When you do emit it, keep it under 8 lines."

# Run pipeline inside a subshell so the watchdog can kill the whole tree via $_BGPID.
# Pipeline: dispatch-agent.sh (YAAS_AGENT backend) → tee (raw ndjson) →
# format-stream.py (human log). dispatch-agent.sh streams raw JSONL on stdout
# and exits with the agent's exit code, so PIPESTATUS[0] is still the agent's.
(
  YAAS_AGENT="$YAAS_AGENT" REPO_ROOT="$REPO_ROOT" \
  YAAS_WORKER_PERMISSION_MODE="$WORKER_PERMISSION_MODE" \
    bash "$SCRIPT_DIR/dispatch-agent.sh" "$WORKER_PROMPT" \
    2> "${WORKER_NDJSON}.err" \
    | tee "$WORKER_NDJSON" \
    | python3 "$SCRIPT_DIR/format-stream.py" >> "$WORKER_LOG"
  echo "${PIPESTATUS[0]}" > "$_EXITFILE"
) 9>&- &
_BGPID=$!

# Watchdog: if the worker exceeds the timeout, kill it and all its descendants.
# Writes "124" to $_EXITFILE BEFORE killing so the parent always reads the timeout code.
(
  sleep $WORKER_TIMEOUT
  if kill -0 "$_BGPID" 2>/dev/null; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  TIMEOUT — worker exceeded ${WORKER_TIMEOUT}s, killing (pid=$_BGPID)" >> "$LOG_FILE"
    echo "124" > "$_EXITFILE"
    _kill_tree "$_BGPID" TERM
    sleep 3
    _kill_tree "$_BGPID" KILL
  fi
) 9>&- &
_WATCHDOG=$!

wait "$_BGPID" 2>/dev/null || true
WORKER_WALL=$(($(date +%s) - WORKER_START))
# Kill the watchdog AND its inner `sleep` child — a bare `kill "$_WATCHDOG"`
# only reaps the subshell, orphaning the sleep. (Belt-and-suspenders: with the
# 9>&- above the orphaned sleep no longer holds the lock FD anyway.)
_kill_tree "$_WATCHDOG" TERM
wait "$_WATCHDOG" 2>/dev/null || true

EXIT=$(cat "$_EXITFILE" 2>/dev/null)
EXIT=${EXIT:-1}
rm -f "$_EXITFILE"
log "Worker exited with $EXIT in ${WORKER_WALL}s (readable: $WORKER_LOG)"

# ── Infra-failure guard: detect silent worker failure when Slack MCP is down. ─
# When Slack is unreachable, the worker exits 0 anyway (no tool call failed — it
# just couldn't reach Slack, emitted text, and stopped), which would advance
# every dirty quest's watermark and silently bury the activity. This is what
# buried a real user question (2026-06-15) and matches the recurring outage in
# memory (project_slack_mcp_outage).
#
# The reliable signal is the Slack MCP server's status in the init event's
# .mcp_servers[], NOT the .tools[] list. MCP servers connect ASYNCHRONOUSLY
# after init, so in every HEALTHY run Slack shows status "pending" at init and
# lists zero Slack tools there — yet the run goes on to make 3-6 Slack calls
# once the server finishes connecting. Counting init tools therefore flags every
# healthy run (the bug in the previous version of this guard). The genuine
# failure statuses are:
#   "failed"    — server could not connect (outage)            → 0 slack calls
#   "needs-auth"— server up but unauthenticated                → 0 slack calls
# Anything else ("pending"/"connected"/unknown) is treated as healthy.
#
# We only override when Slack was actually NEEDED for this dispatch (a target is
# "reactions", or a dispatched quest has a slack_* watch). A schedule/email-only
# dispatch that completed fine during a Slack outage still advances normally.
if [ "$EXIT" = "0" ] && [ -f "$WORKER_NDJSON" ]; then
  SLACK_STATUS=$(jq -r 'select(.type=="system" and .subtype=="init") | .mcp_servers[]? | select(.name=="slack") | .status' "$WORKER_NDJSON" 2>/dev/null | head -1)
  case "$SLACK_STATUS" in
    failed|needs-auth)
      SLACK_NEEDED=0
      for _tgt in "${DISPATCH_TARGETS[@]}"; do
        if [ "$_tgt" = "reactions" ]; then SLACK_NEEDED=1; break; fi
        _w="$QUESTS_DIR/$_tgt/watch.json"
        [ -f "$_w" ] || continue
        if [ "$(jq '[.watches[]? | select(.type | type=="string" and startswith("slack_"))] | length' "$_w" 2>/dev/null || echo 0)" -gt 0 ]; then
          SLACK_NEEDED=1; break
        fi
      done
      if [ "$SLACK_NEEDED" = "1" ]; then
        log "INFRA FAILURE — Slack MCP status='$SLACK_STATUS' and Slack was needed for this dispatch. Forcing EXIT=9 so watermarks are preserved (next tick re-surfaces)."
        EXIT=9
      else
        log "Slack MCP status='$SLACK_STATUS' but no dispatched target needed Slack — advancing normally."
      fi
      ;;
  esac
fi

# ── Extract token usage from the raw ndjson ──────────────────────────────────
# Claude: extract-tokens.py parses its result event (tokens + $ cost) and writes
# the gate_dispatch_tokens run-log entry. Codex/Cursor emit a different schema
# with no cost field, so translate-stream.py reports raw token counts instead.
if [ "$YAAS_AGENT" = "claude" ]; then
  python3 "$SCRIPT_DIR/extract-tokens.py" \
    "$WORKER_NDJSON" "$EXIT" "$WORKER_WALL" "$TARGET_LIST" "$RUN_LOG" "$LOG_FILE" "$WORKER_LOG" 2>&1
else
  TOK_SUMMARY=$(python3 "$SCRIPT_DIR/translate-stream.py" "$YAAS_AGENT" "$WORKER_NDJSON" "$EXIT" 2>/dev/null)
  if [ -n "$TOK_SUMMARY" ]; then
    _in=$(printf '%s' "$TOK_SUMMARY" | jq -r '.input_tokens // 0')
    _out=$(printf '%s' "$TOK_SUMMARY" | jq -r '.output_tokens // 0')
    echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_tokens\",\"backend\":\"$YAAS_AGENT\",\"input_tokens\":$_in,\"output_tokens\":$_out,\"wall_sec\":$WORKER_WALL,\"targets\":\"$TARGET_LIST\",\"note\":\"raw tokens; no cost (non-claude backend)\"}" >> "$RUN_LOG"
    log "Worker tokens (backend=$YAAS_AGENT): in=$_in out=$_out wall=${WORKER_WALL}s (no cost figure for non-claude)"
  fi
fi

if [ "$EXIT" = "0" ]; then
  # Worker succeeded — advance dirty quest watermarks and clear pending reactions.
  # Per-quest blocked-event guard: CLAUDE.md tells the worker to append a
  # {"event":"blocked",...} line to timeline.ndjson when it can't finish a
  # quest's work (and to stop without doing the rest). Exit code alone can't
  # carry that — claude -p always exits 0 on normal completion — so we read the
  # signal here. If a quest logged a blocked event during THIS run (ts at/after
  # WORKER_START_UTC), hold its watermark so the next tick re-surfaces the work.
  for qid in "${DIRTY_QUESTS[@]+"${DIRTY_QUESTS[@]}"}"; do
    watch="$QUESTS_DIR/$qid/watch.json"
    [ -f "$watch" ] || continue

    tl="$QUESTS_DIR/$qid/timeline.ndjson"
    if [ -f "$tl" ]; then
      BLOCKED=$(python3 - "$tl" "$WORKER_START_UTC" <<'PYEOF'
import json, sys
from datetime import datetime
tl_path, boundary_raw = sys.argv[1], sys.argv[2]
def parse(t):
    if not t: return None
    try: return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception: return None
boundary = parse(boundary_raw)
hit = False
for line in open(tl_path):
    line = line.strip()
    if not line: continue
    try: d = json.loads(line)
    except Exception: continue
    if d.get("event") != "blocked": continue
    ets = parse(d.get("ts", ""))
    if boundary is None or (ets is not None and ets >= boundary):
        hit = True; break
print("1" if hit else "0")
PYEOF
)
      if [ "$BLOCKED" = "1" ]; then
        log "BLOCKED — quest $qid logged a blocked event this run; watermark NOT advanced (next tick re-surfaces)."
        echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_quest_blocked\",\"quest\":\"$qid\"}" >> "$RUN_LOG"
        continue
      fi
    fi

    TMP=$(mktemp)
    jq --arg now "$NOW_TS" --argjson lags "$LAG_MAP" '
      .watches //= [] |
      .watches[] |= (.last_checked_ts = (($now | tonumber) - ($lags[.type] // 0) | tostring))
    ' "$watch" > "$TMP" && mv "$TMP" "$watch"
    log "Advanced watermark for dirty quest $qid (post-worker-success)"
  done
  if [ -f "$PENDING_REACTIONS" ]; then
    rm -f "$PENDING_REACTIONS"
    log "Cleared pending_reactions.json (worker handled them)"
  fi
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_success\",\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
else
  log "WORKER FAILURE — watermarks and pending_reactions.json left intact. Next tick will re-surface."
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_failure\",\"exit_code\":$EXIT,\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
fi

exit $EXIT
