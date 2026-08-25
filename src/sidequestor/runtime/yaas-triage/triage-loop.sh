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

# triage-loop.sh — long-running KeepAlive driver for tick.py (the orchestrator).
#
# Why this exists: on macOS 26.5.0 (post-update, 2026-06), launchd's
# StartInterval delivery for the package triage job stopped firing — the interval
# spawn shows "pended nondemand spawn = interval" but never delivers, even
# after a clean bootout/bootstrap. kickstart and RunAtLoad fire once but the
# recurring 60s timer is dead. Not power-related (AC, Low Power Mode off).
#
# The robust alternative is KeepAlive + an internal sleep loop: launchd keeps
# exactly one copy of THIS script alive (restarting it if it ever dies), and
# the loop itself paces tick.py at INTERVAL seconds. No dependency on the
# StartInterval timer.
#
# tick.py holds its own single-instance flock, so even if launchd briefly
# double-spawns this loop during a reload, the inner runs serialize safely.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="${SIDEQUESTOR_WORKSPACE:-${YAAS_WORKSPACE:-${PWD:-$SCRIPT_DIR/..}}}"
if [ ! -f "$WORKSPACE_ROOT/.yaas/instance.json" ]; then
  echo "Sidequestor workspace is not set or is missing .yaas/instance.json: $WORKSPACE_ROOT" >&2
  exit 2
fi

# Read one simple KEY=VALUE entry without executing the workspace .env. The Python
# runtime parses the full file for tick.py; this shell wrapper only needs its pacing
# value, and must preserve any value launchd or `sq loop` explicitly exported.
_dotenv_value() {
  python3 - "$WORKSPACE_ROOT/.env" "$1" <<'PY'
from pathlib import Path
import sys

path, wanted = Path(sys.argv[1]), sys.argv[2]
try:
    lines = path.read_text().splitlines()
except (OSError, UnicodeError):
    raise SystemExit(0)
for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if key != wanted:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    print(value)
    break
PY
}

if [ -z "${SIDEQUESTOR_TRIAGE_INTERVAL:-}" ] && [ -z "${YAAS_TRIAGE_INTERVAL:-}" ]; then
  _dotenv_interval="$(_dotenv_value SIDEQUESTOR_TRIAGE_INTERVAL)"
  [ -n "$_dotenv_interval" ] || _dotenv_interval="$(_dotenv_value YAAS_TRIAGE_INTERVAL)"
  if [ -n "$_dotenv_interval" ]; then
    export SIDEQUESTOR_TRIAGE_INTERVAL="$_dotenv_interval"
  fi
fi

INTERVAL="${SIDEQUESTOR_TRIAGE_INTERVAL:-${YAAS_TRIAGE_INTERVAL:-60}}"
INTERVAL="$(python3 - "$INTERVAL" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except (TypeError, ValueError):
    value = 60.0
if not math.isfinite(value) or value <= 0:
    value = 60.0
print(value)
PY
)"
[ -n "$INTERVAL" ] || INTERVAL=60

# `|| true` is required — a worker failure legitimately makes tick.py exit non-zero
# and this loop must not die on it. But discarding the code outright lets a crash loop
# run indefinitely while launchd still reports a healthy job. So count
# consecutive failures to a file that health-monitor.py reads. The threshold there is
# several ticks, precisely because a single non-zero exit is normal.
FAILFILE="$WORKSPACE_ROOT/state/triage/consecutive-tick-failures"
mkdir -p "$(dirname "$FAILFILE")" 2>/dev/null || true

while true; do
  # The loop drives tick.py (the Python orchestrator). The port is complete and validated;
  # the original shell orchestrator has been retired to archive/.
  if python3 "$SCRIPT_DIR/tick.py"; then
    echo 0 > "$FAILFILE" 2>/dev/null || true
  else
    _prev=$(cat "$FAILFILE" 2>/dev/null || echo 0)
    case "$_prev" in ''|*[!0-9]*) _prev=0 ;; esac
    echo $((_prev + 1)) > "$FAILFILE" 2>/dev/null || true
  fi
  sleep "$INTERVAL"
done
