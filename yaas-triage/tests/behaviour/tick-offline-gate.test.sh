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

# tick-offline-gate.test.sh — a tick with no network does not happen at all.
#
# The incident (2026-08-11): the host lost DNS for ~40 minutes. Ticks kept firing, dispatched
# nine workers, and every one of them exited without reaching the API (`ENOTFOUND`, 0 tokens,
# ~3 minutes of wall time each). A worker that never reached the model cannot ack its manifest,
# so each of those dispatches counted as a no-progress strike against a watch that had done
# nothing wrong, and three watches ended up held across three quests.
#
# Nothing a tick does works without a network, so the fix is upstream of every counter: probe
# first, and if the machine is offline treat the tick as if launchd had never fired. No checks,
# no dispatches, no strikes, no state change of any kind. The next tick tries again 60s later.
#
# The probe FAILS OPEN by design: only an explicit connection error counts as offline, because a
# probe that wrongly reports "offline" would silently stop the whole system.

set -u
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
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

echo "── the probe itself ───────────────────────────────────────────────────────"
probe() { # $1 = host to point the probe at
  python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import tick
class C: env = {'YAAS_NETWORK_PROBE_HOST': '$1'}
class T:
    cfg = C(); agent = 'claude'
    def log(self, m): pass
print('yes' if tick.have_network(T()) else 'no')
"
}
# A name that cannot resolve is exactly the ENOTFOUND the incident produced.
eq "an unresolvable host reads as offline" "$(probe 'api.anthropic.com.invalid')" "no"
eq "the real API host reads as online"     "$(probe 'api.anthropic.com')" "yes"

# The escape hatch, so a broken probe can never wedge the system permanently.
eq "YAAS_SKIP_NETWORK_PROBE=1 forces online" \
   "$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import tick
class C: env = dict(YAAS_NETWORK_PROBE_HOST='api.anthropic.com.invalid', YAAS_SKIP_NETWORK_PROBE='1')
class T:
    cfg = C(); agent = 'claude'
    def log(self, m): pass
print('yes' if tick.have_network(T()) else 'no')
")" "yes"

echo "── it probes the backend actually in use, and fails open otherwise ────────"
# Probing Anthropic on a Codex host would freeze a perfectly healthy system.
host() { python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import tick
class C: env = $2
class T:
    cfg = C(); agent = '$1'
print(tick.network_probe_host(T()))
"; }
eq "claude probes Anthropic"       "$(host claude '{}')" "api.anthropic.com"
eq "codex probes its own backend"  "$(host codex '{}')"  "chatgpt.com"
eq "an unknown backend falls back" "$(host stub '{}')"   "api.anthropic.com"
eq "YAAS_NETWORK_PROBE_HOST wins (proxied deployments)" \
   "$(host claude "{'YAAS_NETWORK_PROBE_HOST': 'proxy.internal'}")" "proxy.internal"

# Only unambiguous network failures may stop the tick. A local fault (fd exhaustion, a sandbox
# denial) must NOT read as offline, or a transient host problem silently kills the orchestrator.
oserr() { python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import tick, socket, errno
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError(errno.$1, 'x'))
class C: env = {}
class T:
    cfg = C(); agent = 'claude'
    def log(self, m): pass
print('yes' if tick.have_network(T()) else 'no')
"; }
eq "fd exhaustion fails OPEN"          "$(oserr EMFILE)"      "yes"
eq "a network-unreachable fails closed" "$(oserr ENETUNREACH)" "no"
eq "a host-unreachable fails closed"    "$(oserr EHOSTUNREACH)" "no"

echo "── an offline tick changes nothing ────────────────────────────────────────"
ROOT="$TMP/repo"
TRIAGE="$ROOT/yaas-triage"
QUEST="$ROOT/state/quests/active/quest-offline"
mkdir -p "$TRIAGE/checkers" "$TRIAGE/ledger" "$TRIAGE/dispatch" "$TRIAGE/ops" \
         "$TRIAGE/surfaces" "$QUEST" "$ROOT/state/triage" "$ROOT/logs"

cp "$SCRIPT_DIR/tick.py" "$SCRIPT_DIR/tick_state.py" "$SCRIPT_DIR/tick_check.py" \
   "$SCRIPT_DIR/tick_dispatch.py" "$SCRIPT_DIR/reaction_config.py" "$TRIAGE/"
cp "$SCRIPT_DIR/ledger/ensure-watch-ids.py" "$SCRIPT_DIR/ledger/ack-watch.py" \
   "$SCRIPT_DIR/ledger/checker-health.py" "$SCRIPT_DIR/ledger/watch-guard.py" \
   "$SCRIPT_DIR/ledger/commit.py" "$SCRIPT_DIR/ledger/housekeep.py" \
   "$SCRIPT_DIR/ledger/approval-helper.py" "$SCRIPT_DIR/ledger/add-watch.py" "$TRIAGE/ledger/"
cp "$SCRIPT_DIR/dispatch/slack-read-health.py" "$SCRIPT_DIR/dispatch/spend-window.py" \
   "$SCRIPT_DIR/dispatch/run-agent.py" "$SCRIPT_DIR/dispatch/plan.py" "$TRIAGE/dispatch/"
cp "$SCRIPT_DIR/checkers/result.py" "$SCRIPT_DIR/checkers/approval.py" "$TRIAGE/checkers/"

for helper in ops/sync-yaas-v2.sh surfaces/mcp-call.sh ops/notify.py ops/rotate-logs.py; do
  mkdir -p "$TRIAGE/$(dirname "$helper")"
  printf '#!/bin/bash\nexit 0\n' > "$TRIAGE/$helper"
  chmod +x "$TRIAGE/$helper"
done
printf '#!/usr/bin/env python3\n' > "$TRIAGE/dispatch/extract-tokens.py"

# A checker that reports DIRTY, and records that it ran. If the offline gate works, this file
# is never created — proving the tick stopped before any checker, not merely before dispatch.
cat > "$TRIAGE/checkers/local_fixture.py" <<PY
#!/usr/bin/env python3
import json, pathlib
pathlib.Path("$ROOT/state/triage/CHECKER-RAN").write_text("yes")
print(json.dumps({"outcome": "dirty", "count": 1, "preview": "x", "complete": True}))
PY
chmod +x "$TRIAGE"/tick.py "$TRIAGE"/checkers/*.py

cat > "$QUEST/watch.json" <<'JSON'
{"watches":[{"type":"local_fixture","channel_id":"D0OFF","thread_ts":"1786408315.983449","last_checked_ts":"100","reason":"would fire if the tick ran"}]}
JSON
printf '{"id":"quest-offline","title":"Offline quest"}\n' > "$QUEST/meta.json"
printf '# Offline quest\n' > "$QUEST/context.md"
: > "$QUEST/timeline.ndjson"

cd "$ROOT" || exit 1
python3 "$TRIAGE/ledger/ensure-watch-ids.py" quest-offline "$QUEST/watch.json" >/dev/null

# Point the probe at an unresolvable host for this tick — the same effect as pulling the wifi,
# without needing to. Uses the same knob a proxied deployment would.
YAAS_AGENT=claude YAAS_TRIAGE_MAX_PARALLEL=1 \
YAAS_NETWORK_PROBE_HOST=api.anthropic.com.invalid \
  python3 "$TRIAGE/tick.py" >"$TMP/tick.out" 2>&1; RC=$?

eq "the tick exits 0 (a skip, not a failure)" "$RC" "0"
grep -q "OFFLINE" "$TMP/tick.out" && ok "it says why it skipped" || bad "no OFFLINE line"
[ -f "$ROOT/state/triage/CHECKER-RAN" ] && bad "a checker ran while offline" \
                                        || ok "no checker ran at all"
eq "the watermark is untouched" \
   "$(jq -r '.watches[0].last_checked_ts' "$QUEST/watch.json")" "100"
[ -f "$ROOT/state/triage/unacked-counts.json" ] \
  && bad "an offline tick created no-progress counters" \
  || ok "no watch takes a no-progress strike"
grep -q "gate_tick_offline" "$ROOT/state/run-log.ndjson" \
  && ok "the run log records the offline skip" || bad "no gate_tick_offline event"
eq "the offline run is counted for the dashboard" \
   "$(jq -r '.runs_offline // 0' "$ROOT/state/triage/last-run.json" 2>/dev/null)" "1"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
