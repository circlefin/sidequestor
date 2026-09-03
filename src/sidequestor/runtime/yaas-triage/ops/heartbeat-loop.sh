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

# heartbeat-loop.sh — KeepAlive driver for health-monitor.py.
#
# Deliberately a SEPARATE launchd job from triage. The whole point of the monitor is
# to detect triage being dead, which a check running inside triage cannot do. It also
# takes no lock and touches no watch state, so it can never wedge the thing it
# watches.
#
# Same KeepAlive-plus-sleep shape as triage-loop.sh, because launchd's StartInterval
# delivery is unreliable on this macOS version (that failure is itself one of the
# outages this monitor exists to catch).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="${SIDEQUESTOR_WORKSPACE:-${YAAS_WORKSPACE:-${PWD:-$SCRIPT_DIR/../..}}}"
PYTHON_BIN="${SIDEQUESTOR_PYTHON:-${YAAS_PYTHON:-python3}}"
if [ ! -f "$WORKSPACE_ROOT/.yaas/instance.json" ]; then
  echo "Sidequestor workspace is not set or is missing .yaas/instance.json: $WORKSPACE_ROOT" >&2
  exit 2
fi

# Read only the pacing key, inertly. Never source the workspace .env: inherited
# launchd and `sq` values must remain authoritative, and dotenv values are data.
_dotenv_value() {
  "$PYTHON_BIN" - "$WORKSPACE_ROOT/.env" "$1" <<'PY'
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

if [ -z "${SIDEQUESTOR_HEARTBEAT_INTERVAL:-}" ] && [ -z "${YAAS_HEARTBEAT_INTERVAL:-}" ]; then
  _dotenv_interval="$(_dotenv_value SIDEQUESTOR_HEARTBEAT_INTERVAL)"
  [ -n "$_dotenv_interval" ] || _dotenv_interval="$(_dotenv_value YAAS_HEARTBEAT_INTERVAL)"
  if [ -n "$_dotenv_interval" ]; then
    export SIDEQUESTOR_HEARTBEAT_INTERVAL="$_dotenv_interval"
  fi
fi

INTERVAL="${SIDEQUESTOR_HEARTBEAT_INTERVAL:-${YAAS_HEARTBEAT_INTERVAL:-300}}"
INTERVAL="$("$PYTHON_BIN" - "$INTERVAL" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except (TypeError, ValueError):
    value = 300.0
if not math.isfinite(value) or value <= 0:
    value = 300.0
print(value)
PY
)"
[ -n "$INTERVAL" ] || INTERVAL=300

while true; do
  # Non-zero just means "something is unhealthy" — that is the normal reporting path,
  # not a failure of this loop, so never let it exit.
  "$PYTHON_BIN" "$SCRIPT_DIR/health-monitor.py" --notify >/dev/null 2>&1 || true
  sleep "$INTERVAL"
done
