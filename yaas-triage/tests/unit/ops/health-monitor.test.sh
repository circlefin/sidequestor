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

# test-health-monitor.sh — the dead-man switch actually fires.
#
# A monitor that silently fails to alert is worse than no monitor, because it
# converts "we have no coverage" into "we believe we have coverage". So every
# condition is provoked against a fixture tree and checked, and the healthy case is
# checked too, so the monitor cannot pass by simply alarming about everything.

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
. "$SCRIPT_DIR/tests/lib/harness.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/repo"
mkdir -p "$REPO/state/triage"
MON() { python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" --json "$@" 2>/dev/null; }
ago() { date -u -v-"$1"M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "$1 minutes ago" +%Y-%m-%dT%H:%M:%SZ; }

reset_state() {
  printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 1)" "$(ago 1)" \
    > "$REPO/state/triage/last-run.json"
  echo 0 > "$REPO/state/triage/consecutive-tick-failures"
  echo '{}' > "$REPO/state/triage/checker-health.json"
  printf '{"version":1,"items":[]}\n' > "$REPO/state/pending-approvals.json"
  : > "$REPO/state/run-log.ndjson"
  rm -f "$REPO/state/triage/health-alerts.json"
}

has() { printf '%s' "$1" | jq -e --arg k "$2" '[.problems[].key] | index($k)' >/dev/null 2>&1; }

echo "── the healthy case must stay quiet ───────────────────────────────────────"
reset_state
OUT=$(MON)
[ "$(printf '%s' "$OUT" | jq -r '.healthy')" = "true" ] && ok "a fresh tick reads healthy" \
  || bad "healthy fixture flagged problems: $(printf '%s' "$OUT" | jq -c '[.problems[].key]')"
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" >/dev/null 2>&1 \
  && ok "exit 0 when healthy" || bad "non-zero exit on a healthy tree"
[ -f "$REPO/state/health-status.json" ] && ok "publishes health-status.json for the dashboard" \
  || bad "no health-status.json written"

echo
echo "── triage stopped running ─────────────────────────────────────────────────"
reset_state
printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 40)" "$(ago 40)" \
  > "$REPO/state/triage/last-run.json"
OUT=$(MON)
has "$OUT" "triage_stalled" && ok "a 40-minute-old completion is flagged as stalled" \
  || bad "stalled loop not detected"
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" >/dev/null 2>&1 \
  && bad "exit 0 despite a problem" || ok "exit 1 when unhealthy"

echo
echo "── a tick started and never finished (the 6.5h crash-loop shape) ─────────"
reset_state
# Start stamp NEWER than the completion stamp, and older than the hung threshold.
printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 100)" "$(ago 200)" \
  > "$REPO/state/triage/last-run.json"
OUT=$(MON)
has "$OUT" "tick_hung" && ok "start-newer-than-completion is flagged as hung" \
  || bad "hung tick not detected — this is the case that went undetected for 6.5h"
# And a long-but-legitimate dispatch must NOT trip it.
printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 20)" "$(ago 200)" \
  > "$REPO/state/triage/last-run.json"
OUT=$(MON)
has "$OUT" "tick_hung" && bad "a 20-minute in-flight tick was wrongly called hung" \
  || ok "a 20-minute in-flight dispatch is not called hung"

echo
echo "── consecutive tick failures ──────────────────────────────────────────────"
reset_state
echo 7 > "$REPO/state/triage/consecutive-tick-failures"
has "$(MON)" "tick_failures" && ok "7 consecutive failures flagged" || bad "failure streak not detected"
reset_state
echo 2 > "$REPO/state/triage/consecutive-tick-failures"
has "$(MON)" "tick_failures" && bad "2 failures tripped the threshold (too twitchy)" \
  || ok "2 failures stay under the threshold"

echo
echo "── a watch whose checker is permanently broken ────────────────────────────"
reset_state
printf '{"watch-abc":{"consecutive_errors":6,"last_error":"invalid_auth","last_error_utc":"%s"}}\n' "$(ago 5)" \
  > "$REPO/state/triage/checker-health.json"
has "$(MON)" "checker_stuck" && ok "a checker at the promotion threshold is flagged" \
  || bad "stuck checker not detected"
reset_state
printf '{"watch-abc":{"consecutive_errors":1,"last_error":"blip","last_error_utc":"%s"}}\n' "$(ago 1)" \
  > "$REPO/state/triage/checker-health.json"
has "$(MON)" "checker_stuck" && bad "a single transient blip was escalated" \
  || ok "a single transient error is not escalated"

echo
echo "── an approved action stuck mid-execution ────────────────────────────────"
reset_state
printf '{"version":1,"items":[{"id":"appr-x","status":"executing","executing_at":"%s"}]}\n' "$(ago 90)" \
  > "$REPO/state/pending-approvals.json"
has "$(MON)" "approval_stuck" && ok "a 90-minute-old executing approval is flagged" \
  || bad "stuck approval not detected (this is the item-7 loss path)"
reset_state
printf '{"version":1,"items":[{"id":"appr-x","status":"executing","executing_at":"%s"}]}\n' "$(ago 2)" \
  > "$REPO/state/pending-approvals.json"
has "$(MON)" "approval_stuck" && bad "an approval executing for 2 minutes was flagged" \
  || ok "a freshly-claimed approval is left alone"
# Forward compatibility with item 7's lease field.
reset_state
printf '{"version":1,"items":[{"id":"appr-y","status":"executing","executing_at":"%s","lease_expires_at":"%s"}]}\n' \
  "$(ago 5)" "$(ago 1)" > "$REPO/state/pending-approvals.json"
has "$(MON)" "approval_stuck" && ok "an expired lease is flagged even when young" \
  || bad "lease_expires_at ignored"

echo
echo "── health events from the run log ────────────────────────────────────────"
reset_state
printf '{"ts":"%s","event":"gate_budget_exceeded","reason":"1h spend $99 over cap $40"}\n' "$(ago 5)" \
  >> "$REPO/state/run-log.ndjson"
printf '{"ts":"%s","event":"gate_watch_misconfigured","quest":"q1","reason":"no checker"}\n' "$(ago 5)" \
  >> "$REPO/state/run-log.ndjson"
OUT=$(MON)
has "$OUT" "event:gate_budget_exceeded"     && ok "budget breach surfaced"      || bad "budget breach missed"
has "$OUT" "event:gate_watch_misconfigured" && ok "misconfigured watch surfaced" || bad "misconfig missed"
# Old events must not alarm forever.
reset_state
printf '{"ts":"%s","event":"gate_budget_exceeded","reason":"old"}\n' "$(ago 600)" >> "$REPO/state/run-log.ndjson"
has "$(MON)" "event:gate_budget_exceeded" && bad "a 10-hour-old event still alarms" \
  || ok "events outside the lookback window are ignored"

echo
echo "── unreadable state ──────────────────────────────────────────────────────"
reset_state
printf 'not json' > "$REPO/state/triage/last-run.json"
has "$(MON)" "state_unreadable" && ok "corrupt last-run.json is flagged" || bad "corrupt state not detected"
reset_state
rm -f "$REPO/state/triage/last-run.json"
has "$(MON)" "state_unreadable" && ok "missing last-run.json is flagged" || bad "missing state not detected"

echo
echo "── notification dedup: alert once, not every five minutes ────────────────"
reset_state
printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 40)" "$(ago 40)" \
  > "$REPO/state/triage/last-run.json"
REC="$TMP/notified.txt"; : > "$REC"
cat > "$TMP/fake-notify.sh" <<SH
#!/bin/bash
printf '%s|%s\n' "\$2" "\$3" >> "$REC"
SH
chmod +x "$TMP/fake-notify.sh"
export YAAS_HEALTH_NOTIFY_CMD="$TMP/fake-notify.sh"
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" --notify >/dev/null 2>&1 || true
FIRST=$(wc -l < "$REC" | tr -d ' ')
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" --notify >/dev/null 2>&1 || true
SECOND=$(wc -l < "$REC" | tr -d ' ')
[ "$FIRST" -ge 1 ] && ok "the first run notifies" || bad "no notification fired at all"
[ "$SECOND" = "$FIRST" ] && ok "an unchanged condition does not re-notify" \
  || bad "re-notified on an unchanged condition ($FIRST then $SECOND)"
# A changed signature must break through the cooldown.
printf '{"tick_started_utc":"%s","last_triage_completed_utc":"%s"}\n' "$(ago 90)" "$(ago 90)" \
  > "$REPO/state/triage/last-run.json"
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" --notify >/dev/null 2>&1 || true
THIRD=$(wc -l < "$REC" | tr -d ' ')
[ "$THIRD" -gt "$SECOND" ] && ok "a worsening condition re-notifies" \
  || bad "a changed signature was suppressed by the cooldown"
# And once it clears, the bookkeeping is dropped so a recurrence alerts again.
reset_state
python3 "$SCRIPT_DIR/ops/health-monitor.py" --repo "$REPO" --notify >/dev/null 2>&1 || true
[ "$(jq -r 'keys | length' "$REPO/state/triage/health-alerts.json" 2>/dev/null || echo 0)" = "0" ] \
  && ok "cleared conditions are forgotten, so a recurrence alerts again" \
  || bad "stale alert bookkeeping survived the condition clearing"
unset YAAS_HEALTH_NOTIFY_CMD

echo
echo "── the monitor shares no state with triage ───────────────────────────────"
# What matters is not whether the word "triage" appears — the operator-facing detail
# strings deliberately name the commands to run. What matters is that the monitor
# takes no lock and executes no triage code, so it can never block or perturb the
# thing it is watching.
if grep -qE "^import fcntl|fcntl\.|flock" "$SCRIPT_DIR/ops/health-monitor.py"; then
  bad "health-monitor.py uses file locking; it must never be able to block triage"
else
  ok "takes no lock, so it cannot wedge what it watches"
fi
if grep -qE "subprocess\.[a-z]+\(\[[^]]*(triage|dispatch-agent|mcp-call)" "$SCRIPT_DIR/ops/health-monitor.py"; then
  bad "health-monitor.py executes triage code, so a triage fault could take it down too"
else
  ok "executes no triage code; only reads state files and shells out to the notifier"
fi
# It must also be readable with nothing but stdlib, so a broken venv cannot mute it.
if grep -qE "^\s*import (requests|yaml|jq)|^\s*from (requests|yaml)" "$SCRIPT_DIR/ops/health-monitor.py"; then
  bad "health-monitor.py has a third-party dependency; the dead-man switch must be stdlib-only"
else
  ok "stdlib-only, so a broken environment cannot silence the alarm"
fi

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "health monitor: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
