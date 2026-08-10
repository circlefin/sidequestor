#!/usr/bin/env python3
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

"""
checkers/schedule.py — check whether a schedule is due to fire.

Two flavours of schedule watch are supported:

  1. Recurring cron:
     {"type":"schedule","cron":"0 9 * * 1","tz":"Asia/Singapore",
      "last_checked_ts":"1234.567","reason":"..."}

  2. One-shot next_fire_ts (no cron). Used by §3b deferred commitments:
     {"type":"schedule","next_fire_ts":"1781575200",
      "last_checked_ts":"1234.567","reason":"..."}
     Due once when now >= next_fire_ts and last_checked_ts has not yet
     passed next_fire_ts. After it fires, triage advances last_checked_ts
     to NOW (> next_fire_ts), so it never re-fires.

Output: 1|schedule due '<cron> (<tz>)'   when due (cron)
        1|one-shot schedule due           when due (next_fire_ts)
        0|                               when not due
        error|reason                     on bad cron expression

Imports cron_due.is_due (same directory) for the actual cron evaluation logic.
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cron_due
import result

def main():
    entry = json.loads(sys.argv[1])

    # One-shot schedule: fire once when next_fire_ts has passed.
    if "cron" not in entry and "next_fire_ts" in entry:
        next_fire = float(entry["next_fire_ts"])
        now = time.time()
        last = float(entry.get("last_checked_ts") or 0)
        if now >= next_fire and last < next_fire:
            result.counted(1, "one-shot schedule due")
        else:
            result.counted(0, "")
        return

    cron_expr = str(entry["cron"])
    tz = str(entry.get("tz", "UTC"))
    since = str(entry.get("last_checked_ts", ""))

    try:
        due = cron_due.is_due(cron_expr, tz, since)
    except ValueError as e:
        result.misconfig(f"bad cron expression: {e}")
        return

    if due:
        result.counted(1, f"schedule due '{cron_expr} ({tz})'")
    else:
        result.counted(0, "")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
