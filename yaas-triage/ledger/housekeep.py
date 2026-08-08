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
housekeep.py — retire watch entries that can never fire again.

This is the DELETE side of watch.json, and deletion is the highest-consequence housekeeping
there is: drop a rule that should have stayed and you silently stop tracking a live thread;
keep one that should have gone and watch.json grows without bound and a fired backstop shows
up forever as a phantom open item. Both have happened. So the three retire decisions, which
used to be one jq expression and two inline Python heredocs inside the original shell orchestrator, are pure
predicates here with a unit test each.

THE THREE RULES (each returns True = retire this entry):

  slack_thread   its parent thread_ts is older than the quest's retire window. The window is
                 per-quest (meta.json retire_slack_threads_after_days), default 30 days.
                 "never" / 0 / false / null / missing-as-default and, crucially, any
                 NON-INTEGER value all mean "do not retire" — the non-integer case matters
                 because the shell fed this into arithmetic where a poisoned value could
                 execute a command, so a strict integer gate is a safety property, not a
                 nicety. Only slack_thread is ever retired by age; other types have semantic
                 permanence.

  approval       its pending-approvals item reached a terminal status (executed / cancelled),
                 so the watch will never fire again.

  schedule       a ONE-SHOT schedule (has next_fire_ts, no cron) whose watermark has already
                 passed next_fire_ts. Recurring cron schedules are never retired — they fire
                 by design.

The write is atomic (temp + os.replace), matching every other watch.json writer. Goldens
assert the surviving watch set, so behaviour here is pinned by the differential harness as
well as the unit tests.

Usage:
    housekeep.py retire <watch.json> <meta.json> <pending-approvals.json> [--now EPOCH]
        rewrites the watch file in place if anything was retired; prints one summary line per
        category actually retired, matching the shell's old log lines.
"""

import json
import os
import sys
import time


def resolve_retire_days(meta, default_days):
    """The thread-retire window for a quest, or None meaning 'never retire threads'.

    Mirrors the shell exactly: never / 0 / false / null / "" / missing → None, and ANY
    non-integer string → None (the strict-integer gate that was a shell-injection guard).
    A positive integer → that many days.
    """
    raw = meta.get("retire_slack_threads_after_days", default_days)
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "0", "false", "never", "null"):
        return None
    if not s.isdigit():          # any non-integer (incl. "1.5", "30d", "1[$(cmd)]") → never
        return None
    days = int(s)
    return days if days > 0 else None


def _thread_epoch(w):
    try:
        return float(w.get("thread_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def retire_thread(w, cutoff_epoch):
    """A slack_thread whose parent is older than the cutoff. cutoff_epoch None → never."""
    if cutoff_epoch is None:
        return False
    return w.get("type") == "slack_thread" and _thread_epoch(w) < cutoff_epoch


def retire_approval(w, done_ids):
    """An approval watch whose item has reached a terminal status."""
    return w.get("type") == "approval" and w.get("approval_id") in done_ids


def retire_schedule(w):
    """A one-shot schedule (next_fire_ts, no cron) whose watermark passed its fire time."""
    if w.get("type") != "schedule" or "cron" in w or "next_fire_ts" not in w:
        return False
    try:
        return float(w.get("last_checked_ts") or 0) >= float(w["next_fire_ts"])
    except (TypeError, ValueError):
        return False


def partition(watches, cutoff_epoch, done_ids):
    """Split watches into kept and a per-reason count of retired. Pure.

    A watch is retired for at most one reason; the reasons are disjoint by type, so order
    does not matter, but they are checked in a fixed order for a stable count.
    """
    kept = []
    counts = {"slack_thread": 0, "approval": 0, "schedule": 0}
    for w in watches:
        if retire_thread(w, cutoff_epoch):
            counts["slack_thread"] += 1
        elif retire_approval(w, done_ids):
            counts["approval"] += 1
        elif retire_schedule(w):
            counts["schedule"] += 1
        else:
            kept.append(w)
    return kept, counts


def _load(path, default):
    try:
        return json.loads(open(path).read())
    except Exception:
        return default


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cmd_retire(watch_path, meta_path, approvals_path, now):
    default_days = int(os.environ.get("YAAS_RETIRE_DEFAULT_DAYS") or 30)
    meta = _load(meta_path, {})
    days = resolve_retire_days(meta, default_days)
    cutoff = None if days is None else (now - days * 86400)

    approvals = _load(approvals_path, {})
    done_ids = {i.get("id") for i in approvals.get("items", [])
                if isinstance(i, dict) and i.get("status") in ("executed", "cancelled")}

    watch = _load(watch_path, None)
    if not isinstance(watch, dict):
        return 0
    watches = watch.get("watches") or []
    kept, counts = partition(watches, cutoff, done_ids)

    if len(kept) == len(watches):
        return 0  # nothing retired; leave the file untouched

    watch["watches"] = kept
    _write_atomic(watch_path, watch)

    # One line per category, echoing the shell's phrasing so operators see no change in logs.
    if counts["slack_thread"]:
        dd = "?" if days is None else days
        print(f"Retired {counts['slack_thread']} stale slack_thread watch(es) "
              f"(thread_ts older than {dd}d)")
    if counts["approval"]:
        print(f"Retired {counts['approval']} completed approval watch(es)")
    if counts["schedule"]:
        print(f"Retired {counts['schedule']} fired one-shot schedule watch(es)")
    return 0


def main():
    args = sys.argv[1:]
    if len(args) >= 4 and args[0] == "retire":
        watch_path, meta_path, approvals_path = args[1], args[2], args[3]
        now = time.time()
        if "--now" in args:
            try:
                now = float(args[args.index("--now") + 1])
            except (ValueError, IndexError):
                pass
        return cmd_retire(watch_path, meta_path, approvals_path, now)
    print("usage: housekeep.py retire <watch.json> <meta.json> <approvals.json> [--now EPOCH]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
