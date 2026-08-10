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
INTERVAL="${YAAS_HEARTBEAT_INTERVAL:-300}"

while true; do
  # Non-zero just means "something is unhealthy" — that is the normal reporting path,
  # not a failure of this loop, so never let it exit.
  python3 "$SCRIPT_DIR/health-monitor.py" --notify >/dev/null 2>&1 || true
  sleep "$INTERVAL"
done
