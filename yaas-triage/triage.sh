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
#   YAAS_CLAUDE_PERMISSION_MODE  Passed to claude --permission-mode (default:
#                  acceptEdits). Set in repo-root .env.
#   YAAS_CODEX_PERMISSION_MODE   workspace-write (default) or bypassPermissions.

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
# Ack ledger (see ack-watch.py). MANIFEST_DIR holds one dispatch-<run_id>.json per
# invocation; UNACKED_FILE counts consecutive dispatches a watch was named in but
# never closed, so a forgotten ack can't re-dispatch the same watch forever.
MANIFEST_DIR="$REPO_ROOT/state/triage"
UNACKED_FILE="$MANIFEST_DIR/unacked-counts.json"
UNACKED_PROMOTE="${YAAS_UNACKED_PROMOTE:-3}"
# Per-watch checker backoff (see checker-health.py). A checker `error` must never
# dispatch a paid worker; it holds the watermark and backs off instead.
CHECKER_HEALTH="$MANIFEST_DIR/checker-health.json"
CHECKER_ERROR_PROMOTE="${YAAS_CHECKER_ERROR_PROMOTE:-6}"
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

# Clear a stale dashboard blocker once triage has concrete evidence that the
# quest recovered. A routine note is enough: all dashboard views already define
# "blocked now" as a blocked event with no later non-blocked timeline event.
mark_recovered_if_blocked() {
  local qid="$1" recovered_source="$2" recovery_note="$3" recovery_run_start="$4"
  local timeline="$QUESTS_DIR/$qid/timeline.ndjson"
  [ -f "$timeline" ] || return 0
  local last_record last_event blocker_ts blocker_kind blocker_text recoverable
  last_record=$(tail -n 1 "$timeline" 2>/dev/null || true)
  last_event=$(printf '%s' "$last_record" | jq -r '.event // empty' 2>/dev/null || true)
  [ "$last_event" = "blocked" ] || return 0
  blocker_ts=$(printf '%s' "$last_record" | jq -r '.ts // empty' 2>/dev/null || true)
  # Never let evidence from this dispatch clear a blocker created during the
  # same dispatch. Missing or malformed timestamps fail closed.
  if ! python3 - "$blocker_ts" "$recovery_run_start" <<'PYEOF'
import sys
from datetime import datetime
def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
try:
    older = parse(sys.argv[1]) < parse(sys.argv[2])
except (IndexError, TypeError, ValueError):
    older = False
raise SystemExit(0 if older else 1)
PYEOF
  then
    return 0
  fi

  # Structured kinds are authoritative for new events. The strict legacy regex
  # handles old Slack MCP/tool outage records without matching business prose
  # such as "partner unreachable on Slack Connect".
  blocker_kind=$(printf '%s' "$last_record" | jq -r '.blocker_kind // empty' 2>/dev/null || true)
  blocker_text=$(printf '%s' "$last_record" | jq -r '[.reason, .note] | map(select(type == "string")) | join(" ")' 2>/dev/null || true)
  recoverable=$(jq -nr --arg kind "$blocker_kind" --arg text "$blocker_text" --arg source "$recovered_source" '
    if $source != "slack" then false
    elif $kind == "slack_tooling_outage" then true
    else
      ($text | ascii_downcase) as $t |
      ($t | test("slack[_ *-]+(mcp|tools?)")) and
      ($t | test("unavailable|outage|not (exposed|registered|authenticated|connected)|absent|no[ -]such[ -]tool|protocol|malformed|failed to connect|needs authentication"))
    end')
  [ "$recoverable" = "true" ] || return 0

  if ! jq -nc --arg ts "$NOW_UTC" --arg note "$recovery_note" \
    --arg source "$recovered_source" \
    '{ts:$ts,event:"note",note:$note,recovered_from:"blocked",recovered_source:$source}' >> "$timeline"; then
    log "RECOVERY WRITE FAILED: $qid — stale blocker left unchanged"
    return 0
  fi
  log "RECOVERED: $qid — $recovery_note"
}

quest_has_recovery_evidence() {
  local qid="$1" source="$2"
  awk -F '\t' -v q="$qid" -v source="$source" '
    $1 == q && $2 == "source_recovered" && $4 == source { recovered=1 }
    $1 == q && ($2 == "skip" || $2 == "error" || $2 == "misconfig") { unsafe=1 }
    END { exit !(recovered && !unsafe) }
  ' "$TMP_RESULTS"
}

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
  rm -f "${WATCH_ID_FAILURES:-}"
  rm -f "${DIRTY_WATCHES_NDJSON:-}"
  # Stamp completion HERE, at true end of tick, not mid-run. It used to be written
  # before dispatch, which meant a tick that crashed during dispatch still looked
  # completed — precisely why the 2026-06-30 crash loop went 6.5 hours undetected.
  # Paired with tick_started_utc below, "started but never completed" becomes a
  # detectable state, which is what health-monitor.py watches for.
  python3 - "$TRIAGE_STATE" <<'PYEOF' 2>/dev/null || true
import json, os, sys
from datetime import datetime, timezone
p = sys.argv[1]
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
    if not isinstance(d, dict): d = {}
except Exception:
    d = {}
d["last_triage_completed_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, p)
PYEOF
  bash "$SCRIPT_DIR/rotate-logs.sh"    2>>"$LOG_FILE" || true
  bash "$SCRIPT_DIR/notify.sh"         2>>"$LOG_FILE" || true
  bash "$SCRIPT_DIR/sync-yaas-v2.sh"   2>>"$LOG_FILE" || true
}
trap '_on_exit' EXIT

NOW_EPOCH=$(date +%s)
NOW_TS=$(python3 -c "import time; print(f'{time.time():.6f}')")
# Snapshot checker-health once. Reading it per watch would cost 121 extra jq/python
# startups a tick; the subshells inherit this string and query it in-memory.
CHECKER_HEALTH_JSON=$(cat "$CHECKER_HEALTH" 2>/dev/null || echo '{}')
case "$CHECKER_HEALTH_JSON" in \{*) ;; *) CHECKER_HEALTH_JSON='{}' ;; esac
NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Record that a tick BEGAN. health-monitor.py compares this against the completion
# stamp written by _on_exit: a start newer than the last completion, by more than the
# tick budget, means a tick died mid-run.
mkdir -p "$(dirname "$TRIAGE_STATE")"
python3 - "$TRIAGE_STATE" "$NOW_UTC" <<'PYEOF' 2>/dev/null || true
import json, os, sys
p, now = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
    if not isinstance(d, dict): d = {}
except Exception:
    d = {}
d["tick_started_utc"] = now
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, p)
PYEOF

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
d['runs_total'] = d.get('runs_total', 0) + 1
d['runs_idle']  = d.get('runs_idle', 0) + 1
json.dump(d, open(p, 'w'), indent=2)
"
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_idle_no_quests\"}" >> "$RUN_LOG"
  log "IDLE — no active quests. Exit 0."
  slog "Run OK — idle. 0 active quests, 0 checks performed."
  exit 0
fi

# Every checker result carries the persistent ID of the exact watch that fired.
# Older quest files predate watch_id, so migrate them once before parallel reads.
# The helper writes atomically and is a no-op after all entries have IDs.
WATCH_ID_FAILURES=$(mktemp)
for qd in "${QUEST_DIRS[@]}"; do
  qid=$(basename "$qd")
  watch="$qd/watch.json"
  [ -f "$watch" ] || continue
  if ! python3 "$SCRIPT_DIR/ensure-watch-ids.py" "$qid" "$watch" 2>>"$LOG_FILE"; then
    echo "$qid" >> "$WATCH_ID_FAILURES"
    log "SKIP: $qid — invalid watch.json; watch IDs could not be ensured"
    jq -nc --arg ts "$NOW_UTC" --arg quest "$qid" \
      '{ts:$ts,event:"gate_quest_unreadable",quest:$quest,reason:"invalid watch.json; watch IDs could not be ensured"}' >> "$RUN_LOG"
  fi
done

# ── For each quest, perform its watch.json checks in parallel ───────────────
check_quest() {
  # Prints "QUEST_ID\tclean|dirty\tWATCH_ID\tWATCH_TYPE\treason" to stdout.
  # Runs sequentially within one quest (so its watch.json lookups are ordered),
  # but multiple quests can run in parallel via `&`.
  #
  # Dispatches to checkers/<type>.py for each entry in watches[]. Adding a new
  # channel type requires only dropping a new script in checkers/ — no changes
  # to this function.
  local quest_dir="$1"
  local qid; qid=$(basename "$quest_dir")
  local watch="$quest_dir/watch.json"

  if grep -Fxq "$qid" "$WATCH_ID_FAILURES"; then
    echo -e "${qid}\tskip\t-\t-\t-\tfalse\twatch_id migration failed; watermark held"
    return 0
  fi

  if [ ! -f "$watch" ]; then
    echo -e "${qid}\tclean\t-\t-\t-\ttrue\tno_watch_file"
    return 0
  fi

  # Evaluate non-Slack watches (approval/schedule/email/cron) BEFORE Slack ones.
  # Those checkers read local state and never hit Slack, so they can never be
  # rate-limited. A `ratelimited` Slack watch returns `skip` and short-circuits
  # this function (return 0) — so if a Slack watch sits earlier in watches[]
  # than a `reviewed` approval or a due `schedule`, the rate-limit skip shadows
  # the local dirty signal and the quest never dispatches. Ordering local
  # checkers first guarantees a local dirty signal always wins over a Slack
  # rate-limit skip. (Incident 2026-07-25: an approved draft sat unexecuted
  # for hours because the quest's first Slack watch rate-limited every tick,
  # short-circuiting before the approval watch at the array tail was ever
  # evaluated.)
  local order
  order=$(jq -r '
    ([ .watches // [] | to_entries[] | select((.value.type // "") | startswith("slack_") | not) | .key ]
     + [ .watches // [] | to_entries[] | select((.value.type // "") | startswith("slack_")) | .key ])
    | .[]' "$watch")
  local i had_dirty=0 had_skip=0
  local slack_expected slack_succeeded=0
  slack_expected=$(jq '[.watches[]? | select((.type // "") | startswith("slack_"))] | length' "$watch")
  for i in $order; do
    local entry watch_id type checker parsed new_count preview
    entry=$(jq -c ".watches[$i]" "$watch")
    watch_id=$(jq -r '.watch_id' <<< "$entry")
    type=$(jq -r '.type // "unknown"' <<< "$entry")
    local watch_id_core watch_id_suffix invalid_watch_id=0
    case "$watch_id" in
      watch-????????????????)
        watch_id_core=${watch_id#watch-}
        ;;
      watch-????????????????-*)
        watch_id_core=${watch_id#watch-}
        watch_id_suffix=${watch_id_core#????????????????-}
        watch_id_core=${watch_id_core%%-*}
        case "$watch_id_suffix" in ''|*[!0-9]*) invalid_watch_id=1 ;; esac
        ;;
      *) invalid_watch_id=1 ;;
    esac
    case "${watch_id_core:-}" in *[!0-9a-f]*) invalid_watch_id=1 ;; esac
    if [ "$invalid_watch_id" = "1" ]; then
      echo -e "${qid}\tmisconfig\t-\t${type}\t-\tfalse\t[$type] invalid or missing watch_id; watermark held"
      had_skip=1
      continue
    fi
    # A watch the worker keeps being handed but that never actually progresses
    # (never acked, acked `blocked` every time, or its commit keeps failing) is
    # not transient: dispatching it again just burns another invocation. Promote
    # to misconfig so the watermark is held, the dashboard gets an event, and a
    # human decides. A commit that advances the watermark clears the counter (see
    # _record_progress), so this only fires on repeated no-progress.
    if [ -f "$UNACKED_FILE" ]; then
      local unacked
      unacked=$(jq -r --arg k "$qid|$watch_id" '.[$k].count // 0' "$UNACKED_FILE" 2>/dev/null || echo 0)
      case "$unacked" in ''|*[!0-9]*) unacked=0 ;; esac
      if [ "$unacked" -ge "$UNACKED_PROMOTE" ]; then
        echo -e "${qid}\tmisconfig\t${watch_id}\t${type}\t-\tfalse\t[$type] dispatched $unacked time(s) with no progress; watermark held pending review"
        had_skip=1
        continue
      fi
    fi
    # Still inside its backoff window? Not dirty, not clean: hold exactly as the
    # rate-limit skip does, and cost nothing.
    local bo_retry
    bo_retry=$(jq -r --arg id "$watch_id" '.[$id].next_retry_ts // "0"' <<< "$CHECKER_HEALTH_JSON" 2>/dev/null || echo 0)
    case "$bo_retry" in ''|*[!0-9.]*) bo_retry=0 ;; esac
    if [ "$(printf '%s\n%s\n' "$bo_retry" "$NOW_TS" | sort -g | tail -1)" = "$bo_retry" ] \
       && [ "$bo_retry" != "0" ]; then
      echo -e "${qid}\tbackoff\t${watch_id}\t${type}\t-\tfalse\t[$type] in checker backoff until $bo_retry"
      had_skip=1
      continue
    fi

    checker="$SCRIPT_DIR/checkers/$type.py"
    if [ ! -x "$checker" ]; then
      echo -e "${qid}\tmisconfig\t${watch_id}\t${type}\t-\tfalse\t[$type] no executable checker; watermark held"
      had_skip=1
      continue
    fi
    vlog "[$qid] checking type=$type"
    parsed=$(python3 "$checker" "$entry" 2>/dev/null || echo "error|checker failed to execute")

    # Two accepted shapes. New: one line of JSON per checkers/result.py, which can
    # express the safe cursor (advance_to) and whether the bounded window was fully
    # drained (complete). Legacy: `count|preview`, still parsed so an unconverted
    # or third-party checker keeps working (as complete=true, no advance_to).
    local advance_to complete outcome reason
    advance_to=""
    complete="true"
    case "$parsed" in
      \{*)
        outcome=$(jq -r '.outcome // "error"'   <<< "$parsed" 2>/dev/null || echo error)
        new_count=$(jq -r '.count // 0'          <<< "$parsed" 2>/dev/null || echo 0)
        preview=$(jq -r '.preview // ""'         <<< "$parsed" 2>/dev/null || echo "")
        advance_to=$(jq -r '.advance_to // ""'   <<< "$parsed" 2>/dev/null || echo "")
        reason=$(jq -r '.reason // ""'           <<< "$parsed" 2>/dev/null || echo "")
        complete=$(jq -r 'if .complete == false then "false" else "true" end' <<< "$parsed" 2>/dev/null || echo true)
        [ -n "$reason" ] && preview="${preview:+$preview — }$reason"
        case "$outcome" in
          clean)  new_count=0 ;;
          dirty)  [ "${new_count:-0}" -gt 0 ] 2>/dev/null || new_count=1 ;;
          # The non-numeric outcomes keep flowing through new_count, which the
          # branches below already switch on.
          ratelimited|error|misconfig) new_count="$outcome" ;;
          *) new_count="error"; preview="unknown outcome '$outcome'" ;;
        esac
        ;;
      *)
        new_count="${parsed%%|*}"
        preview="${parsed#*|}"
        ;;
    esac

    if [ "$new_count" = "misconfig" ]; then
      echo -e "${qid}\tmisconfig\t${watch_id}\t${type}\t-\tfalse\t[$type] $preview"
      had_skip=1
      continue
    fi
    if [ "$new_count" = "ratelimited" ]; then
      # Transient Slack rate-limit — NOT dirty. Marking dirty here would burn a
      # full Opus dispatch that finds nothing, and the rate-limit is usually
      # self-inflicted by checker volume, so dispatching makes it worse. The
      # checker already held its watermark; skip this quest for this tick and
      # let it retry next tick once the tier recovers. (Real incident
      # 2026-07-24: ratelimited checks read as `error`→dirty and re-fired the
      # same 6 quests every 60s for ~13.5h, burning >$1k with zero output.)
      echo -e "${qid}\tskip\t${watch_id}\t${type}\t-\tfalse\t[$type] $preview"
      had_skip=1
      # Slack watches are ordered after local watches. Once Slack rate-limits
      # one request, further Slack calls in this quest only deepen the burst.
      # Stop here; unexamined watches retain their old watermarks and retry.
      break
    fi
    if [ "$new_count" = "error" ]; then
      # An LLM dispatch is NEVER the retry mechanism for a checker failure. This
      # used to set had_dirty=1, so anything failing repeatably (expired
      # credential, changed upstream shape, DNS failure) woke a paid worker every
      # 60s forever and the worker could do nothing about it. Now: hold the
      # watermark, back off exponentially, and promote to `misconfig` once it is
      # clearly not transient.
      local errn
      errn=$(python3 "$SCRIPT_DIR/checker-health.py" fail "$watch_id" "$preview" 2>>"$LOG_FILE" || echo 1)
      case "$errn" in ''|*[!0-9]*) errn=1 ;; esac
      if [ "$errn" -ge "$CHECKER_ERROR_PROMOTE" ]; then
        echo -e "${qid}\tmisconfig\t${watch_id}\t${type}\t-\tfalse\t[$type] $errn consecutive checker errors — $preview"
      else
        echo -e "${qid}\tbackoff\t${watch_id}\t${type}\t-\tfalse\t[$type] checker error $errn/$CHECKER_ERROR_PROMOTE, backing off — $preview"
      fi
      had_skip=1
      continue
    fi
    case "$new_count" in
      ''|*[!0-9]*)
        echo -e "${qid}\tmisconfig\t${watch_id}\t${type}\t-\tfalse\t[$type] malformed checker result; watermark held"
        had_skip=1
        continue
        ;;
    esac
    # A clean result with complete=false is the subtle case: the checker looked at a
    # saturated window and counted zero, which does NOT prove there is nothing older.
    # It used to collapse into the quest-level `clean` line and get advanced by the
    # clean pass, which is a silent-loss path inside the mechanism built to prevent
    # silent loss. Emit it as `hold` so the analysis loop keeps its cursor.
    if [ "${new_count:-0}" -eq 0 ] && [ "$complete" = "false" ]; then
      echo -e "${qid}\thold\t${watch_id}\t${type}\t-\tfalse\t[$type] window saturated with 0 matches; cursor held"
      had_skip=1
      case "$type" in slack_*) slack_succeeded=$((slack_succeeded + 1)) ;; esac
      continue
    fi

    case "$type" in slack_*) slack_succeeded=$((slack_succeeded + 1)) ;; esac
    # Recovery. Guarded on the snapshot so the healthy path spawns no process.
    if [ "$(jq -r --arg id "$watch_id" 'has($id)' <<< "$CHECKER_HEALTH_JSON" 2>/dev/null || echo false)" = "true" ]; then
      python3 "$SCRIPT_DIR/checker-health.py" ok "$watch_id" >/dev/null 2>>"$LOG_FILE" || true
      log "CHECKER RECOVERED: $qid [$type] $watch_id"
    fi
    if [ "${new_count:-0}" -gt 0 ]; then
      echo -e "${qid}\tdirty\t${watch_id}\t${type}\t${advance_to:--}\t${complete}\t[$type] $new_count new — \"$preview\""
      had_dirty=1
    fi
  done

  if [ "$slack_expected" -gt 0 ] && [ "$slack_succeeded" -eq "$slack_expected" ]; then
    echo -e "${qid}\tsource_recovered\t-\tslack\t-\ttrue\tall Slack watches checked successfully"
  fi
  if [ "$had_dirty" = "0" ] && [ "$had_skip" = "0" ]; then
    echo -e "${qid}\tclean\t-\t-\t-\ttrue\tall_checks_passed"
  fi
}

# Run quest checks in parallel, a few quests at a time.
TMP_RESULTS=$(mktemp)
# (trap set earlier at post-lock — covers TMP_RESULTS via ${TMP_RESULTS:-})

# MAX_PARALLEL caps how many quest checkers run at once, i.e. the peak number of
# concurrent Slack API calls (each check_quest fires its calls sequentially, so
# one in-flight call per running quest). This burst concurrency is what trips
# Slack's rate-limit detection, so keep it low. 3 lanes clears the full ~18-quest
# sweep in ~20-30s (bounded below by the biggest single quest's serial calls),
# well inside the 60s tick. Was 8, which contributed to the 2026-07-24 storm.
# Override with YAAS_TRIAGE_MAX_PARALLEL if the quest set grows.
MAX_PARALLEL="${YAAS_TRIAGE_MAX_PARALLEL:-3}"
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
DIRTY_WATCHES_NDJSON=$(mktemp)
while IFS=$'\t' read -r qid status watch_id watch_type advance_to complete reason; do
  if [ "$status" = "dirty" ]; then
    if [[ " ${DIRTY_QUESTS[*]-} " != *" $qid "* ]]; then
      DIRTY_QUESTS+=("$qid")
    fi
    # advance_to and complete ride along so commit_quest can use the checker's own
    # safe cursor, and can refuse to advance a watch whose window was truncated.
    jq -nc --arg quest_id "$qid" --arg watch_id "$watch_id" --arg type "$watch_type" --arg outcome "$status" \
      --arg advance_to "$advance_to" --arg complete "$complete" \
      '{quest_id:$quest_id,watch_id:$watch_id,type:$type,checker_outcome:$outcome,
        advance_to:(if $advance_to == "" or $advance_to == "-" then null else $advance_to end),
        complete:($complete != "false")}' >> "$DIRTY_WATCHES_NDJSON"
    log "DIRTY: $qid — $reason"
  elif [ "$status" = "hold" ]; then
    # Clean, but the checker could not prove it drained its window. Not dirty (there
    # is nothing to act on) and not clean (the cursor must not move).
    if [[ " ${SKIPPED_QUESTS[*]-} " != *" $qid "* ]]; then
      SKIPPED_QUESTS+=("$qid")
    fi
    log "HOLD: $qid — $reason"
    jq -nc --arg ts "$NOW_UTC" --arg quest "$qid" --arg watch_id "$watch_id" --arg type "$watch_type" \
      '{ts:$ts,event:"gate_watch_backlog",quest:$quest,watch_id:$watch_id,type:$type,reason:"clean but window not drained"}' \
      >> "$RUN_LOG" || true
  elif [ "$status" = "backoff" ]; then
    # Checker-level failure under exponential backoff. Held exactly like a rate
    # limit (watermark preserved, no dispatch), but logged distinctly because this
    # one is a real fault that will be promoted to misconfig if it persists.
    if [[ " ${SKIPPED_QUESTS[*]-} " != *" $qid "* ]]; then
      SKIPPED_QUESTS+=("$qid")
    fi
    log "BACKOFF: $qid — $reason"
  elif [ "$status" = "skip" ]; then
    # Transient rate-limit (see check_quest). Neither dispatched nor watermark-
    # advanced: it stays out of CLEAN_QUESTS so the watermark is held, and out
    # of DIRTY_QUESTS so no Opus dispatch fires. Retries next tick.
    if [[ " ${SKIPPED_QUESTS[*]-} " != *" $qid "* ]]; then
      SKIPPED_QUESTS+=("$qid")
    fi
    log "SKIP: $qid — $reason"
  elif [ "$status" = "misconfig" ]; then
    # NOT transient. An unknown watch type, an invalid watch_id, or a checker
    # that returned garbage will fail identically on every future tick, and
    # holding the watermark keeps the whole quest out of CLEAN_QUESTS forever
    # (every other watch in it stops advancing too). A rate-limit SKIP line
    # self-heals and is noise; this needs a human, so it also lands in the
    # run-log where the dashboard can surface it.
    if [[ " ${SKIPPED_QUESTS[*]-} " != *" $qid "* ]]; then
      SKIPPED_QUESTS+=("$qid")
    fi
    log "MISCONFIG: $qid — $reason (will not self-heal; every watch in this quest is held)"
    jq -nc --arg ts "$NOW_UTC" --arg quest "$qid" --arg watch_id "$watch_id" \
      --arg type "$watch_type" --arg reason "$reason" \
      '{ts:$ts,event:"gate_watch_misconfigured",quest:$quest,watch_id:$watch_id,type:$type,reason:$reason}' \
      >> "$RUN_LOG" || log "RUN LOG WRITE FAILED: $qid misconfig not recorded"
  elif [ "$status" = "clean" ]; then
    CLEAN_QUESTS+=("$qid")
    vlog "CLEAN: $qid — $reason"
  elif [ "$status" = "source_recovered" ]; then
    vlog "SOURCE OK: $qid — $watch_type — $reason"
  fi
done < "$TMP_RESULTS"

DIRTY_WATCHES_JSON=$(jq -sc '.' "$DIRTY_WATCHES_NDJSON")
rm -f "$DIRTY_WATCHES_NDJSON"
DIRTY_WATCHES_NDJSON=""
WATCHES_SKIPPED_COUNT=$(awk -F '\t' '$2 == "skip" || $2 == "misconfig" || $2 == "backoff" || $2 == "hold" { count++ } END { print count + 0 }' "$TMP_RESULTS")
WATCHES_MISCONFIGURED_COUNT=$(awk -F '\t' '$2 == "misconfig" { count++ } END { print count + 0 }' "$TMP_RESULTS")
WATCHES_BACKOFF_COUNT=$(awk -F '\t' '$2 == "backoff" { count++ } END { print count + 0 }' "$TMP_RESULTS")
# Saturated windows: dispatched, but the checker could not prove it saw everything,
# so their cursors must not advance no matter what the worker acks.
WATCHES_TRUNCATED_COUNT=$(awk -F '\t' '($2 == "dirty" || $2 == "hold") && $6 == "false" { count++ } END { print count + 0 }' "$TMP_RESULTS")

# A quest can contain both dispatched dirty watches and rate-limited watches
# whose watermarks must remain held. Count it as dirty (not also skipped) while
# preserving the per-watch skip outcome for the commit logic below.
if [ "${#DIRTY_QUESTS[@]}" -gt 0 ] && [ "${#SKIPPED_QUESTS[@]}" -gt 0 ]; then
  FILTERED_SKIPPED=()
  for qid in "${SKIPPED_QUESTS[@]}"; do
    if [[ " ${DIRTY_QUESTS[*]} " != *" $qid "* ]]; then
      FILTERED_SKIPPED+=("$qid")
    fi
  done
  SKIPPED_QUESTS=("${FILTERED_SKIPPED[@]+"${FILTERED_SKIPPED[@]}"}")
fi

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
for qid in "${CLEAN_QUESTS[@]+"${CLEAN_QUESTS[@]}"}"; do
  watch="$QUESTS_DIR/$qid/watch.json"
  [ -f "$watch" ] || continue
  TMP=$(mktemp "$(dirname "$watch")/.watch.XXXXXX")
  if ! jq --arg now "$NOW_TS" --argjson lags "$LAG_MAP" '
    .watches //= [] |
    .watches[] |= (.last_checked_ts = (($now | tonumber) - ($lags[.type] // 0) | tostring))
  ' "$watch" > "$TMP" || ! mv "$TMP" "$watch"; then
    rm -f "$TMP"
    log "WATCH WRITE FAILED: $qid — clean watermark not advanced"
    continue
  fi
  vlog "Advanced watermark for $qid"
done

# ── Update triage run counters ────────────────────────────────────────────────
python3 -c "
import json, os
p = '$TRIAGE_STATE'
d = json.load(open(p)) if os.path.exists(p) else {}
# The completion stamp is written by _on_exit, not here: this block runs BEFORE
# the dry-run, Slack-health, budget and dispatch decisions, so anything it claimed
# about the tick's outcome would be a guess. Check-phase facts only.
d['runs_total']     = d.get('runs_total', 0) + 1
d['quests_checked'] = $QUEST_COUNT
d['quests_dirty']   = $DIRTY_COUNT
d['quests_clean']   = $CLEAN_COUNT
d['quests_skipped'] = $SKIPPED_COUNT
d['watches_skipped'] = $WATCHES_SKIPPED_COUNT
d['watches_misconfigured'] = $WATCHES_MISCONFIGURED_COUNT
d['watches_in_backoff']    = $WATCHES_BACKOFF_COUNT
d['watches_truncated']     = $WATCHES_TRUNCATED_COUNT
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

# ── Retire fired one-shot schedule watches ──────────────────────────────────
# A one-shot schedule (next_fire_ts, no cron) fires exactly once: schedule.py
# gates on `last_checked_ts < next_fire_ts`, and triage advances the watermark
# past next_fire_ts on the firing tick, so the entry can never fire again.
# Nothing removed it, so every fired backstop stayed in watch.json forever and
# surfaced as a permanent "scheduled" open item — one re-armed promise showing
# as N duplicates (a conversion-limit backstop re-armed weekly showed as 5; a
# sandbox-onboarding quest had 15). Drop one-shot schedules whose next_fire_ts
# is already behind the watermark. Recurring cron schedules (have `cron`) are
# never touched — they fire repeatedly by design.
for qd in "${QUEST_DIRS[@]}"; do
  watch="$qd/watch.json"
  [ -f "$watch" ] || continue
  _has=$(jq '[.watches[]? | select(.type == "schedule" and (has("cron") | not) and has("next_fire_ts"))] | length' "$watch" 2>/dev/null || echo 0)
  [ "${_has:-0}" -gt 0 ] || continue
  TMP=$(mktemp)
  python3 - "$watch" "$TMP" <<'PYEOF' && mv "$TMP" "$watch" || { rm -f "$TMP"; true; }
import json, sys
watch_path, out_path = sys.argv[1], sys.argv[2]
watch = json.load(open(watch_path))
def fired(w):
    if w.get("type") != "schedule" or "cron" in w or "next_fire_ts" not in w:
        return False
    try:
        return float(w.get("last_checked_ts") or 0) >= float(w["next_fire_ts"])
    except (TypeError, ValueError):
        return False
before = len(watch.get("watches", []))
watch["watches"] = [w for w in watch.get("watches", []) if not fired(w)]
after = len(watch["watches"])
json.dump(watch, open(out_path, "w"), indent=2)
if before != after:
    print(f"Retired {before - after} fired one-shot schedule watch(es) from {watch_path}")
PYEOF
done

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

# ── Prune old ack manifests ─────────────────────────────────────────────────
# One dispatch-<run_id>.json per invocation. They are only needed until the
# commit that reads them, but keeping a week makes "what did the worker actually
# close" answerable after the fact.
python3 "$SCRIPT_DIR/ack-watch.py" prune "${YAAS_MANIFEST_RETAIN_DAYS:-7}" \
  >/dev/null 2>>"$LOG_FILE" || true
python3 "$SCRIPT_DIR/checker-health.py" prune "${YAAS_CHECKER_HEALTH_RETAIN_DAYS:-30}" \
  >/dev/null 2>>"$LOG_FILE" || true

# ── Decide ──────────────────────────────────────────────────────────────────
if [ "$DIRTY_COUNT" = "0" ] && [ "$REACTIONS_DIRTY" = "0" ]; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_idle\",\"quests_checked\":$QUEST_COUNT,\"quests_skipped\":$SKIPPED_COUNT,\"watches_skipped\":$WATCHES_SKIPPED_COUNT,\"watches_misconfigured\":$WATCHES_MISCONFIGURED_COUNT,\"watches_in_backoff\":$WATCHES_BACKOFF_COUNT,\"watches_truncated\":$WATCHES_TRUNCATED_COUNT}" >> "$RUN_LOG"
  python3 -c "
import json, os
p = '$TRIAGE_STATE'
d = json.load(open(p)) if os.path.exists(p) else {}
d['runs_idle'] = d.get('runs_idle', 0) + 1
json.dump(d, open(p, 'w'), indent=2)
" 2>/dev/null || true
  log "IDLE — $QUEST_COUNT quest(s) checked, 0 dirty, $SKIPPED_COUNT fully skipped quest(s), $WATCHES_SKIPPED_COUNT held watch(es) ($WATCHES_BACKOFF_COUNT in checker backoff, $WATCHES_MISCONFIGURED_COUNT misconfigured), 0 new reactions. Watermarks advanced where safe."
  slog "Run OK — idle. $QUEST_COUNT quest(s) swept, 0 activity, $WATCHES_SKIPPED_COUNT held watch(es)."
  exit 0
fi

# Build the dispatch target list (quests + optional synthetic "reactions")
DISPATCH_TARGETS=("${DIRTY_QUESTS[@]+"${DIRTY_QUESTS[@]}"}")
if [ "$REACTIONS_DIRTY" = "1" ]; then
  DISPATCH_TARGETS+=("reactions")
fi

TARGETS_JSON=$(printf '%s\n' "${DISPATCH_TARGETS[@]}" | jq -R . | jq -sc .)
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_dirty_dry_run\",\"targets\":$TARGETS_JSON,\"dirty_watches\":$DIRTY_WATCHES_JSON}" >> "$RUN_LOG"
  log "DRY_RUN=1 — would dispatch for ${DISPATCH_TARGETS[*]}. Watermarks of dirty quests NOT advanced; pending reactions retained."
  slog "[DRY RUN] Would dispatch worker for: ${DISPATCH_TARGETS[*]}"
  exit 0
fi

# ── Spend and dispatch ceilings ──────────────────────────────────────────────
# Measured 2026-08-05: $64/day across ~164 dispatches, median $0.99 each, p90
# $1.84, max $4.05 — with no cap of any kind. Per-quest dispatch then raised the
# per-tick ceiling from one invocation to YAAS_MAX_DISPATCH_FANOUT, and each
# invocation reloads the full rules file (260k-747k cache-read tokens observed), so
# a multi-quest tick now pays that N times instead of once.
#
# Every input is already in run-log.ndjson, so this needs no new accounting. Two
# windows because they catch different failures: 6h catches a runaway loop inside a
# day, 24h catches sustained drift that never trips the 6h cap.
#
# On breach we still ran every check, so clean watermarks already advanced and
# nothing is buried — only the DISPATCH is withheld, and the backlog re-surfaces
# once the window rolls forward or the cap is raised.
#
# Fails OPEN: an unreadable run log must not wedge all dispatch forever. The
# heartbeat job is what should notice a persistently unreadable run log.
# Defaults calibrated against measured behaviour on 2026-08-05, not guessed. The
# point is a circuit breaker for a storm, NOT a throttle on normal operation, so
# every cap sits well above the observed natural rate and well below storm rate:
#
#   hourly spend, measured over 80 hours: median $4.53, p90 $12.36, max $14.66
#   the 2026-07-24 storm            >$1k/13.5h  => ~$74/hour
#   busiest observed dispatch hour  34           => ~200/6h worst realistic case
#
# Three windows, each doing a different job:
#   1h    the responsive dollar tripwire. At storm rate this stops things after ~$40
#         instead of letting the 24h cap absorb ~$250 first.
#   24h   slow backstop for drift that never trips the hourly cap.
#   6h count  the only cap that works under the Codex and Cursor backends, which
#         report raw tokens and no cost figure, and the fastest of all on a tight
#         loop (250 dispatches inside ~4 minutes at a per-minute rate).
#
# $40/hour is ~2.7x the busiest hour ever observed and well under the ~$74/hour storm
# rate. It is set above the observed max rather than near it because per-quest
# dispatch (§ A) raised the per-tick invocation ceiling from 1 to 4, so those 80
# measured hours understate what a legitimately busy hour can now cost. A first
# attempt at $25/6h was rejected for exactly the opposite mistake: it sat BELOW the
# live window's existing spend and would have withheld all dispatch immediately.
MAX_SPEND_1H="${YAAS_MAX_SPEND_1H:-40}"
MAX_SPEND_24H="${YAAS_MAX_SPEND_24H:-250}"
MAX_DISPATCH_6H="${YAAS_MAX_DISPATCH_6H:-250}"
BUDGET_JSON=$(python3 "$SCRIPT_DIR/spend-window.py" "$RUN_LOG" \
  --cap-1h "$MAX_SPEND_1H" --cap-24h "$MAX_SPEND_24H" --cap-dispatch-6h "$MAX_DISPATCH_6H" \
  2>>"$LOG_FILE" || echo '')
if [ -n "$BUDGET_JSON" ]; then
  BUDGET_BREACH=$(printf '%s' "$BUDGET_JSON" | jq -r '.breach // ""')
  _s1=$(printf '%s' "$BUDGET_JSON"  | jq -r '.spend_1h // 0')
  _s6=$(printf '%s' "$BUDGET_JSON"  | jq -r '.spend_6h // 0')
  _s24=$(printf '%s' "$BUDGET_JSON" | jq -r '.spend_24h // 0')
  _d6=$(printf '%s' "$BUDGET_JSON"  | jq -r '.dispatches_6h // 0')
  _uncosted=$(printf '%s' "$BUDGET_JSON" | jq -r '.uncosted_24h // 0')
  if [ -n "$BUDGET_BREACH" ]; then
    jq -nc --arg ts "$NOW_UTC" --arg reason "$BUDGET_BREACH" --argjson budget "$BUDGET_JSON" \
      --argjson targets "$TARGETS_JSON" \
      '{ts:$ts,event:"gate_budget_exceeded",reason:$reason,budget:$budget,targets:$targets}' >> "$RUN_LOG"
    log "BUDGET EXCEEDED — $BUDGET_BREACH. Dispatch withheld for [${DISPATCH_TARGETS[*]}]; watermarks preserved, reactions retained. Override with YAAS_MAX_SPEND_1H / _24H / YAAS_MAX_DISPATCH_6H in .env."
    slog "Run OK — budget cap hit ($BUDGET_BREACH). Dispatch withheld."
    exit 0
  fi
  # spend_6h is reported for observability but not enforced: 6h sits awkwardly
  # between the hourly tripwire and the daily backstop.
  # The dollar caps only see Claude dispatches: the Codex and Cursor backends report
  # raw tokens with no cost figure (112 of 137 dispatches in the measured 24h were
  # codex, hence uncosted). The dispatch-COUNT cap is what covers those, which is why
  # it exists alongside the dollar one rather than as a redundant belt.
  if [ "${_uncosted:-0}" -gt 0 ]; then
    vlog "Budget: \$$_s1/1h \$$_s24/24h (6h \$$_s6 unenforced), $_d6 dispatch(es)/6h ($_uncosted uncosted non-claude — invisible to the dollar caps, covered by the count cap)"
  fi
fi

# ── Pre-dispatch Slack health gate ───────────────────────────────────────────
# Determine whether this dispatch actually needs Slack (a "reactions" target, or
# any dirty quest with a slack_* watch). If it does, ping Slack once before
# spending the dispatch. On failure, skip the tick WITHOUT advancing dirty
# watermarks or clearing pending reactions — the next tick re-surfaces the same
# activity. This protects every backend (esp. Codex/Cursor, which lack the
# post-run .mcp_servers status signal the Claude guard below relies on).
# Does this one target need Slack? Used by the tick-level health gate below and,
# per dispatch, by the post-run infra-failure guard.
_target_needs_slack() {
  local _t="$1" _w
  [ "$_t" = "reactions" ] && return 0
  _w="$QUESTS_DIR/$_t/watch.json"
  [ -f "$_w" ] || return 1
  [ "$(jq '[.watches[]? | select(.type | type=="string" and startswith("slack_"))] | length' "$_w" 2>/dev/null || echo 0)" -gt 0 ]
}

SLACK_NEEDED=0
for _tgt in "${DISPATCH_TARGETS[@]}"; do
  if _target_needs_slack "$_tgt"; then SLACK_NEEDED=1; break; fi
done
if [ "$SLACK_NEEDED" = "1" ] && ! slack_health_ok; then
  echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_slack_down\",\"targets\":$TARGETS_JSON}" >> "$RUN_LOG"
  log "SLACK DOWN — pre-dispatch ping failed and Slack is needed for [${DISPATCH_TARGETS[*]}]. Skipping dispatch; watermarks preserved, reactions retained. Retrying next tick."
  slog "Run OK — Slack unreachable, dispatch skipped (will retry)."
  exit 0
fi

# ── Dispatch: one agent invocation per dirty target ─────────────────────────
#
# Transaction boundary: each target gets its OWN invocation and its OWN commit.
# Previously one invocation carried every dirty quest plus "reactions", and a
# single exit 0 committed all of them — so a worker that handled quest A, hit a
# tool error on quest B, and never opened quest C still advanced all three
# watermarks and buried B's and C's activity. Now B's failure cannot touch A or C.
#
# Sequential on purpose. Two concurrent workers in one repo would append to the
# same watch.json and timeline.ndjson through the raw Edit tool with no locking,
# and fan-out multiplies the blast radius of a runaway loop. The triage flock is
# held for the whole tick, so overlapping ticks are already impossible; there is
# no reason to also overlap workers.
#
# Within a target, the ack ledger (ack-watch.py) decides what commits: triage
# advances only the items the worker explicitly closed. Unacked items keep their
# old watermark and re-surface next tick. Exit 0 alone no longer commits anything.
log "DISPATCH — ${#DISPATCH_TARGETS[@]} target(s) (backend=$YAAS_AGENT): ${DISPATCH_TARGETS[*]}"
TARGET_LIST=$(printf '%s\n' "${DISPATCH_TARGETS[@]}" | paste -sd',' -)
echo "{\"ts\":\"$NOW_UTC\",\"event\":\"gate_dispatch\",\"targets\":$TARGETS_JSON,\"dirty_watches\":$DIRTY_WATCHES_JSON}" >> "$RUN_LOG"
slog "Run OK — ${#DISPATCH_TARGETS[@]} dirty target(s): ${DISPATCH_TARGETS[*]}. Dispatching..."

cd "$REPO_ROOT"

WORKER_TIMEOUT=1800  # 30 min per target; normal workers finish in <3 min, but a
                     # live sandbox retest (on-chain sends + payment propagation)
                     # can run long — 900s was killing those mid-run (exit 124),
                     # re-dispatching, and never completing (livelock).
# Fan-out caps. Each target is a full agent invocation, so an unbounded loop over
# a large dirty set could spend a lot of money and hold the lock for many ticks.
# Targets past either cap are deferred: their watermarks are untouched, so the
# next tick re-detects them.
MAX_FANOUT="${YAAS_MAX_DISPATCH_FANOUT:-4}"
TICK_BUDGET="${YAAS_TICK_DISPATCH_BUDGET:-3600}"
# TICK_BUDGET alone only gates whether the NEXT target starts, so a target
# launched at budget-minus-one-second could still run a further WORKER_TIMEOUT and
# hold the flock far past the budget. Each dispatch's watchdog is therefore capped
# at the remaining budget, and a target that cannot get at least MIN_SLICE seconds
# is deferred rather than started and killed moments later.
MIN_DISPATCH_SLICE="${YAAS_MIN_DISPATCH_SLICE:-300}"
DISPATCH_TIMEOUT=$WORKER_TIMEOUT
CLAUDE_PERMISSION_MODE="${YAAS_CLAUDE_PERMISSION_MODE:-${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}}"
CODEX_PERMISSION_MODE="${YAAS_CODEX_PERMISSION_MODE:-workspace-write}"

# Recursive process-tree killer — needed to terminate the agent's background
# subprocesses which keep pipe FDs open and prevent the pipeline exiting cleanly.
_kill_tree() {
  local _p=$1 _sig=${2:-TERM}
  local _ch
  _ch=$(pgrep -P "$_p" 2>/dev/null) || true
  for _c in $_ch; do _kill_tree "$_c" "$_sig"; done
  kill -"$_sig" "$_p" 2>/dev/null || true
}

# Run discipline shared by every dispatch prompt. Backend-neutral: each agent
# loads its own rules file (CLAUDE.md / AGENTS.md) from the repo root.
_RUN_DISCIPLINE="watch.json is not editable: append with yaas-triage/add-watch.py per § 3a (a hook blocks the raw write). ACT SILENTLY: emit NO text between tool calls — no 'Reading X' or 'I need to check Y' narration. Batch independent reads/edits into a single turn using parallel tool_use blocks whenever possible. OUTPUT CONTRACT: emit the summary ONLY if something material happened (message sent, draft created, state changed, quest status changed). If nothing material happened — just exit with no text. When you do emit it, keep it under 8 lines."

# ── dispatch_one <target> ────────────────────────────────────────────────────
# Runs exactly one agent invocation for one target. Sets these globals for the
# caller's commit step; returns 0 always (the verdict is DISPATCH_EXIT).
DISPATCH_EXIT=1
DISPATCH_RUN_ID=""
DISPATCH_NDJSON=""
DISPATCH_START_UTC=""
DISPATCH_SLACK_READ_OK=0
DISPATCH_WALL=0

dispatch_one() {
  local target="$1"
  local stamp slug kind items prompt ack_block exitfile bgpid watchdog t0
  local worker_log worker_ndjson slack_status
  # Reset every global the caller reads. DISPATCH_WALL especially: it is assigned
  # only after `wait` below, so without this reset an early return (no items,
  # manifest failure) would leave the PREVIOUS target's wall time in place and the
  # loop would add it to TICK_SPENT a second time, deferring later targets for no
  # reason.
  DISPATCH_EXIT=1
  DISPATCH_WALL=0
  DISPATCH_SLACK_READ_OK=0
  DISPATCH_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  slug=$(printf '%s' "$target" | tr -c 'A-Za-z0-9._-' '_')
  DISPATCH_RUN_ID="run-$stamp-$$-$DISPATCHED"

  # ── Open the ack manifest: the exact set of items this invocation must close.
  if [ "$target" = "reactions" ]; then
    kind="reactions"
    items=$(jq -c '[to_entries[] | .key as $e | .value[] | {item_id: ($e + ":" + .), type: $e}]' \
      "$PENDING_REACTIONS" 2>/dev/null || echo '[]')
  else
    kind="quest"
    items=$(jq -c --arg q "$target" '[.[] | select(.quest_id == $q) | {item_id: .watch_id, type: .type}]' \
      <<< "$DIRTY_WATCHES_JSON")
  fi
  if [ -z "$items" ] || [ "$items" = "[]" ]; then
    log "DISPATCH SKIPPED: $target — no dispatchable items in manifest"
    DISPATCH_EXIT=8
    return 0
  fi
  if ! python3 "$SCRIPT_DIR/ack-watch.py" open "$DISPATCH_RUN_ID" "$target" "$kind" "$items" \
       >/dev/null 2>>"$LOG_FILE"; then
    # No manifest means no evidence-based commit is possible. Refuse to dispatch
    # rather than fall back to committing on exit code alone.
    log "ACK MANIFEST FAILED: $target — dispatch skipped, watermarks held"
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg target "$target" \
      '{ts:$ts,event:"gate_ack_manifest_failed",target:$target}' >> "$RUN_LOG"
    DISPATCH_EXIT=8
    return 0
  fi

  # ── Per-dispatch log files. worker-latest.* points at the invocation in flight
  #    so the dashboard's live panel keeps working unchanged.
  worker_log="$LOG_DIR/worker-$stamp-$slug.log"
  worker_ndjson="$LOG_DIR/worker-$stamp-$slug.ndjson"
  DISPATCH_NDJSON="$worker_ndjson"
  ln -sf "$(basename "$worker_log")"    "$LOG_DIR/worker-latest.log"
  ln -sf "$(basename "$worker_ndjson")" "$LOG_DIR/worker-latest.ndjson"
  {
    echo "=== Worker dispatch $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "Target: $target"
    echo "Run ID: $DISPATCH_RUN_ID"
    echo "Ack manifest items: $items"
    echo "========================================================"
  } > "$worker_log"
  log "DISPATCH [$target] run_id=$DISPATCH_RUN_ID log=$worker_log"

  # ── Prompt. The ack block is what makes the commit evidence-based.
  ack_block="ACK LEDGER (REQUIRED): this dispatch has run_id $DISPATCH_RUN_ID. Before you exit, close EVERY item listed above with exactly one call each: python3 yaas-triage/ack-watch.py ack $DISPATCH_RUN_ID <item_id> handled|nothing_to_do|blocked \"<one-line note>\". Use handled when you acted (replied, drafted, queued for review, adopted, saved state), nothing_to_do when you read the new activity and it correctly needs no action, blocked when you could not finish. An item you do not ack keeps its old watermark and is re-dispatched next tick — so never ack something you did not actually look at, and never skip acking something you did handle."

  if [ "$target" = "reactions" ]; then
    prompt="Yaas worker dispatch: dirty target: reactions. Ack items (JSON): $items — each item_id is \"<emoji>:<msg_ts>\". Run the Reactions Fast Path in your rules file. It is SELF-CONTAINED: do NOT read any quest folder, except as the :incoming_envelope: adoption section explicitly requires. $ack_block $_RUN_DISCIPLINE"
  else
    prompt="Yaas worker dispatch: dirty target: $target. Exact dirty watches (JSON): $items — each item_id is a watch_id. Process EVERY listed watch_id: select it directly from watch.json with jq --arg id WATCH_ID '.watches[] | select(.watch_id == \$id)' and query that watch's source. Do not scan or truncate watch.json to guess what fired. Follow the Quest Activation Protocol in your rules file: read ONLY context.md first; read meta.json/watch.json/timeline.ndjson only when you actually need them to act. $ack_block $_RUN_DISCIPLINE"
  fi

  # ── Run. Subshell so the watchdog can kill the whole tree via $bgpid.
  #    Pipeline: dispatch-agent.sh → tee (raw ndjson) → format-stream.py (human).
  #    PIPESTATUS[0] is the agent's own exit code.
  exitfile=$(mktemp)
  t0=$(date +%s)
  (
    YAAS_AGENT="$YAAS_AGENT" REPO_ROOT="$REPO_ROOT" \
    YAAS_CLAUDE_PERMISSION_MODE="$CLAUDE_PERMISSION_MODE" \
    YAAS_CODEX_PERMISSION_MODE="$CODEX_PERMISSION_MODE" \
      bash "$SCRIPT_DIR/dispatch-agent.sh" "$prompt" \
      2> "${worker_ndjson}.err" \
      | tee "$worker_ndjson" \
      | python3 "$SCRIPT_DIR/format-stream.py" >> "$worker_log"
    echo "${PIPESTATUS[0]}" > "$exitfile"
  ) 9>&- &
  bgpid=$!

  # Watchdog writes 124 BEFORE killing so the parent always reads the timeout code.
  (
    sleep $DISPATCH_TIMEOUT
    if kill -0 "$bgpid" 2>/dev/null; then
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  TIMEOUT — worker for $target exceeded ${DISPATCH_TIMEOUT}s, killing (pid=$bgpid)" >> "$LOG_FILE"
      echo "124" > "$exitfile"
      _kill_tree "$bgpid" TERM
      sleep 3
      _kill_tree "$bgpid" KILL
    fi
  ) 9>&- &
  watchdog=$!

  wait "$bgpid" 2>/dev/null || true
  DISPATCH_WALL=$(($(date +%s) - t0))
  # Kill the watchdog AND its inner `sleep` — a bare kill only reaps the subshell,
  # orphaning the sleep. (With 9>&- above the orphan no longer holds the lock FD.)
  _kill_tree "$watchdog" TERM
  wait "$watchdog" 2>/dev/null || true

  DISPATCH_EXIT=$(cat "$exitfile" 2>/dev/null)
  DISPATCH_EXIT=${DISPATCH_EXIT:-1}
  rm -f "$exitfile"
  log "Worker [$target] exited with $DISPATCH_EXIT in ${DISPATCH_WALL}s (readable: $worker_log)"

  # ── Infra-failure guard, per dispatch. When Slack is unreachable the worker
  # exits 0 anyway (no tool call failed — it just couldn't reach Slack, emitted
  # text, and stopped). The reliable signal is the Slack MCP server's status in
  # the init event's .mcp_servers[], NOT the .tools[] list: MCP servers connect
  # ASYNCHRONOUSLY after init, so in every HEALTHY run Slack shows "pending" at
  # init and lists zero Slack tools, then goes on to make several Slack calls.
  # Genuine failures are "failed" (could not connect) and "needs-auth".
  # Only override when THIS target actually needed Slack.
  if [ "$DISPATCH_EXIT" = "0" ] && [ -f "$worker_ndjson" ]; then
    slack_status=$(jq -r 'select(.type=="system" and .subtype=="init") | .mcp_servers[]? | select(.name=="slack") | .status' \
      "$worker_ndjson" 2>/dev/null | head -1)
    case "$slack_status" in
      failed|needs-auth)
        if _target_needs_slack "$target"; then
          log "INFRA FAILURE [$target] — Slack MCP status='$slack_status' and Slack was needed. Forcing exit 9 so watermarks are preserved."
          DISPATCH_EXIT=9
        else
          log "Slack MCP status='$slack_status' but $target did not need Slack — advancing normally."
        fi
        ;;
    esac
  fi

  # Recovery of a worker-side Slack outage requires evidence from the worker's
  # own event stream. Triage's curl checkers are a different execution path and
  # cannot prove native MCP/app/shell Slack access was available to the agent.
  if [ "$DISPATCH_EXIT" = "0" ] \
     && python3 "$SCRIPT_DIR/worker-source-evidence.py" slack "$worker_ndjson"; then
    DISPATCH_SLACK_READ_OK=1
    log "WORKER SOURCE OK [$target]: successful Slack read observed in worker event stream"
  fi

  # ── Token usage. Claude reports cost in its result event; Codex/Cursor use a
  # different schema with no cost field, so report raw counts instead.
  if [ "$YAAS_AGENT" = "claude" ]; then
    python3 "$SCRIPT_DIR/extract-tokens.py" \
      "$worker_ndjson" "$DISPATCH_EXIT" "$DISPATCH_WALL" "$target" \
      "$RUN_LOG" "$LOG_FILE" "$worker_log" 2>&1 || true
  else
    local tok _in _out
    tok=$(python3 "$SCRIPT_DIR/translate-stream.py" "$YAAS_AGENT" "$worker_ndjson" "$DISPATCH_EXIT" 2>/dev/null || true)
    if [ -n "$tok" ]; then
      _in=$(printf '%s' "$tok" | jq -r '.input_tokens // 0')
      _out=$(printf '%s' "$tok" | jq -r '.output_tokens // 0')
      echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_tokens\",\"backend\":\"$YAAS_AGENT\",\"input_tokens\":$_in,\"output_tokens\":$_out,\"wall_sec\":$DISPATCH_WALL,\"targets\":\"$target\",\"note\":\"raw tokens; no cost (non-claude backend)\"}" >> "$RUN_LOG"
      log "Worker tokens [$target] (backend=$YAAS_AGENT): in=$_in out=$_out wall=${DISPATCH_WALL}s"
    fi
  fi
  return 0
}

# ── Bookkeeping for dispatched items that made no progress ──────────────────
# A dispatched item whose watermark did NOT advance will be re-detected and
# re-dispatched next tick. Correct once, but unbounded it is a paid loop: a model
# that never acks, an item acked `blocked` every time, a worker that keeps exiting
# non-zero, or a watch.json that cannot be written all produce the same
# no-progress-forever shape. So the counter keys off the COMMIT RESULT, not the
# manifest status — anything still un-advanced after commit counts. check_quest
# promotes a watch past YAAS_UNACKED_PROMOTE to the existing `misconfig` outcome,
# which holds the watermark and stops dispatching it until a human looks. Real
# progress on an item clears its counter.
_record_progress() {
  # $1 = scope (quest_id, or "reactions"), $2 = run_id, $3 = newline-separated
  #      item_ids that actually committed (may be empty)
  local scope="$1" rid="$2" committed="$3"
  python3 - "$UNACKED_FILE" "$MANIFEST_DIR/dispatch-$rid.json" "$scope" "$NOW_UTC" "$committed" <<'PYEOF' 2>>"$LOG_FILE" || true
import json, os, sys
counts_path, manifest_path, scope, now, committed_raw = sys.argv[1:6]
try:
    manifest = json.load(open(manifest_path))
except Exception:
    # A manifest we cannot read means we cannot tell which items progressed. Bump a
    # scope-level counter so persistent corruption is still bounded and visible
    # instead of looping silently forever.
    manifest = None
try:
    counts = json.load(open(counts_path)) if os.path.exists(counts_path) else {}
except Exception:
    counts = {}

def bump(key, itype="", status=""):
    rec = counts.get(key) or {}
    rec["count"] = int(rec.get("count", 0)) + 1
    rec["first_utc"] = rec.get("first_utc") or now
    rec["last_utc"] = now
    if itype:
        rec["type"] = itype
    if status:
        rec["last_status"] = status
    counts[key] = rec

if manifest is None:
    bump(f"{scope}|<unreadable-manifest>")
else:
    committed = {c for c in committed_raw.split("\n") if c}
    for item in manifest.get("items", []):
        iid = item.get("item_id", "")
        key = f"{scope}|{iid}"
        if iid in committed:
            counts.pop(key, None)
        else:
            bump(key, item.get("type", ""), item.get("status", "pending"))

tmp = counts_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(counts, f, indent=2)
os.replace(tmp, counts_path)
PYEOF
}

# ── commit_quest <quest_id> ─────────────────────────────────────────────────
# Advances watermarks for the dispatched watches this worker actually acked.
commit_quest() {
  local qid="$1"
  local watch tl blocked acked_ids acked_json advanced

  watch="$QUESTS_DIR/$qid/watch.json"
  [ -f "$watch" ] || return 0

  # Exit 124 is our own watchdog kill. An ack is written only AFTER that item's
  # work completed, so acked items remain trustworthy and must commit — otherwise
  # the next tick re-does finished work and can send a duplicate reply. Every other
  # non-zero exit (including 9, our synthetic "Slack was down" verdict) holds
  # everything, because there the acks themselves are suspect.
  if [ "$DISPATCH_EXIT" != "0" ] && [ "$DISPATCH_EXIT" != "124" ]; then
    log "WORKER FAILURE [$qid] — exit $DISPATCH_EXIT; watermarks left intact. Next tick re-surfaces."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg quest "$qid" \
      --argjson exit_code "$DISPATCH_EXIT" \
      '{ts:$ts,event:"gate_dispatch_failure",exit_code:$exit_code,targets:[$quest]}' >> "$RUN_LOG"
    _record_progress "$qid" "$DISPATCH_RUN_ID" ""
    return 0
  fi

  # Per-quest blocked-event guard: CLAUDE.md tells the worker to append a
  # {"event":"blocked",...} line to timeline.ndjson when it can't finish a quest's
  # work (and to stop without doing the rest). Exit code alone can't carry that,
  # so read the signal here. A blocked event during THIS dispatch holds the whole
  # quest, ack ledger notwithstanding — the worker said it stopped early.
  tl="$QUESTS_DIR/$qid/timeline.ndjson"
  if [ -f "$tl" ]; then
    blocked=$(python3 - "$tl" "$DISPATCH_START_UTC" <<'PYEOF'
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
    if [ "$blocked" = "1" ]; then
      log "BLOCKED — quest $qid logged a blocked event this dispatch; no watermark advanced (next tick re-surfaces)."
      echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_quest_blocked\",\"quest\":\"$qid\"}" >> "$RUN_LOG"
      _record_progress "$qid" "$DISPATCH_RUN_ID" ""
      return 0
    fi
  fi

  # ── Evidence-based commit: only watch_ids the worker closed as handled or
  #    nothing_to_do. Unacked and blocked items keep their old watermark.
  # `acked` exits non-zero when the manifest is missing or corrupt, which is NOT
  # the same as "the worker acked nothing" — treat it as a hard hold and say so,
  # so a persistently corrupt manifest is diagnosable instead of looking like a
  # silent worker.
  if ! acked_ids=$(python3 "$SCRIPT_DIR/ack-watch.py" acked "$DISPATCH_RUN_ID" 2>>"$LOG_FILE"); then
    log "ACK MANIFEST UNREADABLE [$qid] — no watermark advanced (next tick re-surfaces)."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg quest "$qid" --arg run_id "$DISPATCH_RUN_ID" \
      '{ts:$ts,event:"gate_ack_manifest_unreadable",quest:$quest,run_id:$run_id}' >> "$RUN_LOG"
    _record_progress "$qid" "$DISPATCH_RUN_ID" ""
    return 0
  fi
  acked_json=$(printf '%s\n' "$acked_ids" | jq -R 'select(length > 0)' | jq -sc .)
  log "ACK SUMMARY [$qid] $(python3 "$SCRIPT_DIR/ack-watch.py" summary "$DISPATCH_RUN_ID" 2>/dev/null || echo '{}')"

  if [ "$acked_json" = "[]" ]; then
    log "NO ACKS [$qid] — worker exited 0 without closing any item; every watermark held (next tick re-surfaces)."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg quest "$qid" --arg run_id "$DISPATCH_RUN_ID" \
      '{ts:$ts,event:"gate_dispatch_unacked",quest:$quest,run_id:$run_id}' >> "$RUN_LOG"
    _record_progress "$qid" "$DISPATCH_RUN_ID" ""
    return 0
  fi

  TMP=$(mktemp "$(dirname "$watch")/.watch.XXXXXX")
  # Three conditions to advance a watch, ALL required:
  #   1. it was dispatched this run (present in the dirty manifest),
  #   2. the worker acked it handled/nothing_to_do,
  #   3. the checker proved it drained its window (complete != false).
  # (3) is the saturation tripwire. Every checker reads a BOUNDED window, and a
  # window that came back full cannot prove there is nothing older, so advancing
  # past it would skip activity nobody ever read. The cursor itself is the
  # checker's own advance_to when it supplied one — the newest item it actually
  # covered — and only otherwise the now-minus-lag guess.
  if ! jq --arg now "$NOW_TS" --arg qid "$qid" --argjson lags "$LAG_MAP" \
       --argjson dirty "$DIRTY_WATCHES_JSON" --argjson acked "$acked_json" '
    .watches //= [] |
    .watches[] |= (
      . as $w |
      ([$dirty[] | select(.quest_id == $qid and .watch_id == $w.watch_id)] | first) as $d |
      if $d != null
         and (any($acked[]; . == $w.watch_id))
         and ($d.complete != false)
      then .last_checked_ts = (
             if ($d.advance_to // "") != ""
             then ($d.advance_to | tostring)
             else (($now | tonumber) - ($lags[$w.type] // 0) | tostring)
             end)
      else .
      end
    )
  ' "$watch" > "$TMP" || ! mv "$TMP" "$watch"; then
    rm -f "$TMP"
    log "WATCH WRITE FAILED: $qid — acked watch watermarks not advanced"
    _record_progress "$qid" "$DISPATCH_RUN_ID" ""
    return 0
  fi

  # The COMMITTED set: acked AND dispatched AND the checker proved the window drained.
  # This is what actually moved, and it is what the counters, the log line and the
  # no-progress bookkeeping must all agree on. Passing the merely-acked set here was
  # the bug: it cleared the breaker for watches whose cursor had deliberately been held.
  local committed_ids truncated
  committed_ids=$(jq -r --arg qid "$qid" --argjson acked "$acked_json" \
    '.[] | select(.quest_id == $qid and .complete != false and (.watch_id as $i | any($acked[]; . == $i))) | .watch_id' \
    <<< "$DIRTY_WATCHES_JSON" 2>/dev/null || true)
  truncated=$(jq --arg qid "$qid" --argjson acked "$acked_json" \
    '[.[] | select(.quest_id == $qid and .complete == false and (.watch_id as $i | any($acked[]; . == $i)))] | length' \
    <<< "$DIRTY_WATCHES_JSON" 2>/dev/null || echo 0)
  if [ "${truncated:-0}" -gt 0 ]; then
    log "BACKLOG [$qid] — $truncated acked watch(es) had a saturated window; cursor held so unseen older items are not skipped."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg quest "$qid" --argjson n "$truncated" \
      '{ts:$ts,event:"gate_watch_backlog",quest:$quest,watches:$n}' >> "$RUN_LOG"
  fi
  advanced=$(printf '%s\n' "$committed_ids" | grep -c . || true)
  advanced=${advanced:-0}
  log "Advanced $advanced acked watch watermark(s) for dirty quest $qid (post-worker-success)"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_success\",\"targets\":[\"$qid\"],\"acked\":$advanced}" >> "$RUN_LOG"

  if [ "$DISPATCH_SLACK_READ_OK" = "1" ] && quest_has_recovery_evidence "$qid" "slack"; then
    mark_recovered_if_blocked "$qid" "slack" \
      "Every Slack watch was readable and the worker completed a successful Slack read after the previous tooling outage." \
      "$DISPATCH_START_UTC"
  fi
  _record_progress "$qid" "$DISPATCH_RUN_ID" "$committed_ids"
}

# ── commit_reactions ────────────────────────────────────────────────────────
# Keeps only the (emoji, msg_ts) pairs the worker did NOT ack, instead of the old
# behaviour of deleting the whole pending file on exit 0 — which buried every
# reaction the worker silently skipped.
commit_reactions() {
  # Same exit-124 reasoning as commit_quest: acks written before a watchdog kill
  # are trustworthy, so let them clear their pairs.
  if [ "$DISPATCH_EXIT" != "0" ] && [ "$DISPATCH_EXIT" != "124" ]; then
    log "WORKER FAILURE [reactions] — exit $DISPATCH_EXIT; pending_reactions.json left intact."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson exit_code "$DISPATCH_EXIT" \
      '{ts:$ts,event:"gate_dispatch_failure",exit_code:$exit_code,targets:["reactions"]}' >> "$RUN_LOG"
    return 0
  fi
  [ -f "$PENDING_REACTIONS" ] || return 0

  log "ACK SUMMARY [reactions] $(python3 "$SCRIPT_DIR/ack-watch.py" summary "$DISPATCH_RUN_ID" 2>/dev/null || echo '{}')"
  python3 - "$PENDING_REACTIONS" "$MANIFEST_DIR/dispatch-$DISPATCH_RUN_ID.json" \
    "$UNACKED_FILE" "$NOW_UTC" "$UNACKED_PROMOTE" <<'PYEOF' 2>>"$LOG_FILE" || true
import json, os, sys
pending_path, manifest_path, counts_path, now, promote_raw = sys.argv[1:6]
try:
    manifest = json.load(open(manifest_path))
    pending  = json.load(open(pending_path))
except Exception:
    sys.exit(0)
try:
    promote = int(promote_raw)
except ValueError:
    promote = 3
try:
    counts = json.load(open(counts_path)) if os.path.exists(counts_path) else {}
except Exception:
    counts = {}

# Only real progress clears a pending reaction. `blocked` is explicitly NOT
# progress: the worker said it could not finish, so the pair must come back.
done = {i.get("item_id") for i in manifest.get("items", [])
        if i.get("status") in ("handled", "nothing_to_do")}

# Reactions are not watch entries, so check_quest cannot bound their retries. Park
# a pair that has been dispatched `promote` times without progress into the emoji's
# skipped_notes — the same processed-set the reaction checker already diffs against
# — so it stops re-dispatching and is visible in state instead of looping forever.
STATE_FILES = {
    "claude-intensifies": "claude_intensifies_replied.json",
    "writing_hand":       "writing_hand_replied.json",
    "floppy_disk":        "floppy_disk_saved.json",
    "incoming_envelope":  "incoming_envelope_adopted.json",
}
state_dir = os.path.dirname(os.path.dirname(counts_path))   # .../state
parked = []
remaining = {}
for emoji, ts_list in pending.items():
    keep = []
    for ts in ts_list:
        iid = f"{emoji}:{ts}"
        key = f"reactions|{iid}"
        if iid in done:
            counts.pop(key, None)
            continue
        rec = counts.get(key) or {}
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["first_utc"] = rec.get("first_utc") or now
        rec["last_utc"] = now
        rec["type"] = emoji
        counts[key] = rec
        if rec["count"] >= promote and emoji in STATE_FILES:
            sp = os.path.join(state_dir, STATE_FILES[emoji])
            try:
                sdata = json.load(open(sp)) if os.path.exists(sp) else {}
            except Exception:
                sdata = {}
            notes = sdata.setdefault("skipped_notes", {})
            notes[ts] = (f"parked by triage after {rec['count']} dispatch(es) with no "
                         f"progress (last status: {rec.get('last_status','pending')}) — needs review")
            stmp = sp + ".tmp"
            with open(stmp, "w") as f:
                json.dump(sdata, f, indent=2)
            os.replace(stmp, sp)
            counts.pop(key, None)
            parked.append(iid)
            continue
        rec["last_status"] = next((i.get("status", "pending")
                                   for i in manifest.get("items", [])
                                   if i.get("item_id") == iid), "pending")
        keep.append(ts)
    if keep:
        remaining[emoji] = keep

ctmp = counts_path + ".tmp"
with open(ctmp, "w") as f:
    json.dump(counts, f, indent=2)
os.replace(ctmp, counts_path)

if parked:
    print("reactions: parked " + ", ".join(parked) + " into skipped_notes (no progress)")
if remaining:
    tmp = pending_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(remaining, f, indent=2)
    os.replace(tmp, pending_path)
    n = sum(len(v) for v in remaining.values())
    print(f"reactions: {n} pending reaction(s) unacked or blocked, retained for next tick")
else:
    os.unlink(pending_path)
    print("reactions: all progressed, pending_reactions.json cleared")
PYEOF
  if [ -f "$PENDING_REACTIONS" ]; then
    log "REACTIONS PARTIAL — unacked/blocked reactions retained in pending_reactions.json (next tick re-surfaces)."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg run_id "$DISPATCH_RUN_ID" \
      '{ts:$ts,event:"gate_reactions_partial",run_id:$run_id}' >> "$RUN_LOG"
  else
    log "Cleared pending_reactions.json (every reaction progressed)"
    echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"gate_dispatch_success\",\"targets\":[\"reactions\"]}" >> "$RUN_LOG"
  fi
}

# ── Fairness rotation ───────────────────────────────────────────────────────
# Rotate the start position by a persisted cursor so every dirty target eventually
# gets dispatched even when the dirty set is permanently larger than MAX_FANOUT.
_CURSOR=0
if [ -f "$TRIAGE_STATE" ]; then
  _CURSOR=$(jq -r '.dispatch_cursor // 0' "$TRIAGE_STATE" 2>/dev/null || echo 0)
  case "$_CURSOR" in ''|*[!0-9]*) _CURSOR=0 ;; esac
fi
_NTARGETS=${#DISPATCH_TARGETS[@]}
_OFFSET=$(( _CURSOR % _NTARGETS ))
ROTATED_TARGETS=()
_ri=0
while [ "$_ri" -lt "$_NTARGETS" ]; do
  ROTATED_TARGETS+=("${DISPATCH_TARGETS[$(( (_OFFSET + _ri) % _NTARGETS ))]}")
  _ri=$(( _ri + 1 ))
done
[ "$_OFFSET" != "0" ] && log "Rotated dispatch order by $_OFFSET for fairness: ${ROTATED_TARGETS[*]}"

# ── The dispatch loop ───────────────────────────────────────────────────────
DISPATCHED=0
TICK_SPENT=0
# LAST_NONZERO_EXIT: triage.sh's own exit status. Any failed dispatch makes the
# tick non-zero; per-target verdicts are what actually gate the commits.
WORST_EXIT=0
for _target in "${ROTATED_TARGETS[@]}"; do
  _REMAINING=$(( TICK_BUDGET - TICK_SPENT ))
  if [ "$DISPATCHED" -ge "$MAX_FANOUT" ] || [ "$_REMAINING" -lt "$MIN_DISPATCH_SLICE" ]; then
    # Deferred targets keep their watermarks, so the next tick re-detects them.
    # Logged so a deferral is never silent.
    log "DEFERRED: $_target (dispatched=$DISPATCHED spent=${TICK_SPENT}s) — re-surfaces next tick"
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg target "$_target" \
      --argjson dispatched "$DISPATCHED" --argjson spent "$TICK_SPENT" \
      '{ts:$ts,event:"gate_dispatch_deferred",target:$target,dispatched:$dispatched,spent_sec:$spent}' >> "$RUN_LOG"
    continue
  fi

  # Per-target hourly breaker. The rolling spend cap stops a storm but does not
  # identify the cause; this bounds any single target that is looping, whatever the
  # reason, and complements the per-ITEM no-progress counter. 25/hour because the
  # busiest legitimate quest observed hit 17 in an hour (a per-minute loop would be
  # 60), so a tighter cap would block real work.
  _tgt_recent=$(python3 "$SCRIPT_DIR/spend-window.py" "$RUN_LOG" --target "$_target" 2>/dev/null | jq -r '.target_dispatches_1h // 0' 2>/dev/null || echo 0)
  case "$_tgt_recent" in ''|*[!0-9]*) _tgt_recent=0 ;; esac
  if [ "$_tgt_recent" -ge "${YAAS_MAX_TARGET_DISPATCH_PER_HOUR:-25}" ]; then
    log "TARGET BREAKER OPEN: $_target dispatched $_tgt_recent time(s) in the last hour; skipping. Watermarks held."
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg target "$_target" --argjson n "$_tgt_recent" \
      '{ts:$ts,event:"gate_target_breaker_open",target:$target,dispatches_1h:$n}' >> "$RUN_LOG"
    continue
  fi

  DISPATCH_TIMEOUT=$WORKER_TIMEOUT
  [ "$_REMAINING" -lt "$DISPATCH_TIMEOUT" ] && DISPATCH_TIMEOUT=$_REMAINING
  dispatch_one "$_target"
  DISPATCHED=$((DISPATCHED + 1))
  TICK_SPENT=$((TICK_SPENT + DISPATCH_WALL))
  [ "$DISPATCH_EXIT" != "0" ] && WORST_EXIT="$DISPATCH_EXIT"

  if [ "$_target" = "reactions" ]; then
    commit_reactions
  else
    commit_quest "$_target"
  fi
done

# Persist the rotation cursor so the next tick starts after what we just ran.
if [ -f "$TRIAGE_STATE" ]; then
  _CTMP=$(mktemp)
  if jq --argjson c "$(( _OFFSET + DISPATCHED ))" '.dispatch_cursor = $c' "$TRIAGE_STATE" > "$_CTMP" 2>/dev/null; then
    mv "$_CTMP" "$TRIAGE_STATE"
  else
    rm -f "$_CTMP"
    log "CURSOR WRITE FAILED — dispatch rotation not advanced"
  fi
fi

# Recorded only now that dispatches have actually run. The old code incremented this
# before the dry-run / Slack-health / budget gates, so a tick that never dispatched
# still bumped runs_dispatched and stamped last_dispatch_utc.
if [ "$DISPATCHED" -gt 0 ]; then
  python3 -c "
import json, os
p = '$TRIAGE_STATE'
d = json.load(open(p)) if os.path.exists(p) else {}
d['runs_dispatched'] = d.get('runs_dispatched', 0) + 1
d['last_dispatch_utc'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
json.dump(d, open(p, 'w'), indent=2)
" 2>/dev/null || true
fi

log "DISPATCH DONE — $DISPATCHED invocation(s), ${TICK_SPENT}s total, last non-zero exit $WORST_EXIT"
exit "$WORST_EXIT"
