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

# dashboard-server.test.sh - completed worker streams must not remain live.

set -u

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

REPO="$TMP/repo"
mkdir -p "$REPO/yaas-triage/ops" "$REPO/logs" "$REPO/state/triage"
cp "$SCRIPT_DIR/ops/dashboard-server.py" "$REPO/yaas-triage/ops/"
cp "$SCRIPT_DIR/approval_state.py" "$SCRIPT_DIR/approval_store.py" \
  "$SCRIPT_DIR/tick_check.py" "$SCRIPT_DIR/tick_state.py" "$REPO/yaas-triage/"

python3 - "$REPO" <<'PY'
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

repo = Path(sys.argv[1])
server_path = repo / "yaas-triage" / "ops" / "dashboard-server.py"
sys.argv = [str(server_path)]
spec = importlib.util.spec_from_file_location("dashboard_server", server_path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

log = repo / "logs" / "worker-latest.log"
worker_state = repo / "state" / "triage" / "worker-current.json"


def write_log(*lines):
    log.write_text("\n".join(lines) + "\n")
    os.utime(log, None)


def write_status(state, heartbeat_at, **changes):
    value = {
        "schema": 1,
        "run_ref": "20260821T030337Z-quest-ant",
        "state": state,
        "targets": ["quest-ant"],
        "started_at": "2026-08-21T03:03:37Z",
        "heartbeat_at": heartbeat_at,
        "ended_at": None,
        "exit": None,
        "log": log.name,
    }
    value.update(changes)
    worker_state.write_text(json.dumps(value))


now = datetime.now(timezone.utc)
fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
old = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")


write_log(
    "=== Worker dispatch 2026-08-21T03:03:37Z ===",
    "Target: quest-ant",
    "========================================================",
    "[item.completed] work finished",
)
live = server.build_live_run()
assert live["running"] is True, live
assert live["targets"] == ["quest-ant"], live

write_status("exited", fresh, ended_at=fresh, exit=0)
completed = server.build_live_run()
assert completed["running"] is False, completed
assert completed["state"] == "exited", completed

write_status("exited", fresh, ended_at=fresh, exit=0, log="worker-old.log")
shadowed = server.build_live_run()
assert shadowed["running"] is True, shadowed

write_status("running", fresh)
running = server.build_live_run()
assert running["running"] is True, running
assert running["targets"] == ["quest-ant"], running
assert running["tail"][-1] == "[item.completed] work finished", running

write_status("running", old)
stale = server.build_live_run()
assert stale["running"] is False, stale
assert stale["stale"] is True, stale
assert stale["state"] == "stale", stale
assert stale["targets"] == ["quest-ant"], stale

write_status("running", fresh.removesuffix("Z"))
naive_utc = server.build_live_run()
assert naive_utc["running"] is True, naive_utc

assert server._worker_log_lines("../../etc/passwd") == []

# Missing lifecycle state is the compatibility path for an in-flight old worker.
worker_state.unlink()
write_log(
    "=== Worker dispatch 2026-08-21T03:03:37Z ===",
    "Target: quest-ant",
    "========================================================",
    "[item.completed] work finished",
    "[turn.completed] {\"type\": \"turn.completed\"}",
)
completed_fallback = server.build_live_run()
assert completed_fallback["running"] is False, completed_fallback

write_log(
    "=== Worker dispatch 2026-08-21T03:03:37Z ===",
    "Target: quest-ant",
    "========================================================",
    "=== Tokens: 100 ===",
)
footer_fallback = server.build_live_run()
assert footer_fallback["running"] is False, footer_fallback
PY

status=$?
if [ "$status" -eq 0 ]; then
  printf '  \033[32mPASS\033[0m lifecycle state, heartbeat expiry, and log fallback agree\n'
else
  printf '  \033[31mFAIL\033[0m dashboard worker lifecycle projection is inconsistent\n'
fi
exit "$status"
