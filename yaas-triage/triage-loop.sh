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

# triage-loop.sh — long-running KeepAlive driver for triage.sh.
#
# Why this exists: on macOS 26.5.0 (post-update, 2026-06), launchd's
# StartInterval delivery for com.yaas.triage stopped firing — the interval
# spawn shows "pended nondemand spawn = interval" but never delivers, even
# after a clean bootout/bootstrap. kickstart and RunAtLoad fire once but the
# recurring 60s timer is dead. Not power-related (AC, Low Power Mode off).
#
# The robust alternative is KeepAlive + an internal sleep loop: launchd keeps
# exactly one copy of THIS script alive (restarting it if it ever dies), and
# the loop itself paces triage.sh at INTERVAL seconds. No dependency on the
# StartInterval timer.
#
# triage.sh holds its own single-instance flock, so even if launchd briefly
# double-spawns this loop during a reload, the inner runs serialize safely.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${YAAS_TRIAGE_INTERVAL:-60}"

# `|| true` is required — a worker failure legitimately makes triage.sh exit non-zero
# and this loop must not die on it. But discarding the code outright is what let the
# 2026-06-30 crash loop run 6.5 hours while launchd reported a healthy job. So count
# consecutive failures to a file that health-monitor.py reads. The threshold there is
# several ticks, precisely because a single non-zero exit is normal.
FAILFILE="$SCRIPT_DIR/../state/triage/consecutive-tick-failures"
mkdir -p "$(dirname "$FAILFILE")" 2>/dev/null || true

while true; do
  if "$SCRIPT_DIR/triage.sh"; then
    echo 0 > "$FAILFILE" 2>/dev/null || true
  else
    _prev=$(cat "$FAILFILE" 2>/dev/null || echo 0)
    case "$_prev" in ''|*[!0-9]*) _prev=0 ;; esac
    echo $((_prev + 1)) > "$FAILFILE" 2>/dev/null || true
  fi
  sleep "$INTERVAL"
done
