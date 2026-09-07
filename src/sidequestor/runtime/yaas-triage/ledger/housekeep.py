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
up forever as a phantom open item. Both have happened. So the retire decisions, which
used to be one jq expression and two inline Python heredocs inside the original shell orchestrator, are pure
predicates here with a unit test each.

THE FOUR RULES (each returns True = retire this entry):

  slack_thread   its latest known activity is older than the quest's retire window. Activity
                 is max(thread_ts, created_ts, last_activity_ts), so adding a watch to an old
                 thread gets a full window and each observed reply extends it. Older entries
                 need no migration: absent optional fields simply fall back to thread_ts.
                 The window is
                 per-quest (meta.json retire_slack_threads_after_days), default 14 days.
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

  ephemeral      a watch its CREATOR marked `ephemeral: true`, created more than
                 YAAS_RETIRE_EPHEMERAL_HOURS ago (default 168, one week). These come from the
                 dual-watch rule: when a worker DMs someone it also watches the whole
                 channel, because the reply usually arrives top-level rather than threaded.
                 That is right in spirit but opens an UNBOUNDED watch to catch a BOUNDED
                 reply, and nothing ever closed it. Two such watches were once still waking
                 on every message in a DM 3 and 12 days after their question was answered;
                 one had logged 15 of 40 such wakes as "not mine, no action", each costing
                 a full agent dispatch, and the other acted on an unrelated request that the
                 reactions path was ALSO handling, sending the same thing twice.

                 The flag is DECLARED, never inferred. An earlier draft of this rule keyed
                 on channel_id starting with "D" as a proxy for "reply-catcher" and was
                 wrong in both directions: a quest that monitors a bot's DMs, and one that
                 monitors a single colleague's DM channel, are standing subscriptions that
                 live on `D…` channels and would have been deleted once the window elapsed,
                 while a reply-catcher opened on a `C…` channel would never expire. A watch that
                 should persist is simply not marked, so permanence is the default and no
                 per-quest exception list is needed.

The write is atomic (temp + os.replace), matching every other watch.json writer. Goldens
assert the surviving watch set, so behaviour here is pinned by the differential harness as
well as the unit tests.

Usage:
    housekeep.py retire <watch.json> <meta.json> <pending-approvals.json> [--now EPOCH]
        rewrites the watch file in place if anything was retired; prints one summary line per
        category actually retired, matching the shell's old log lines.
"""

import fcntl
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


def resolve_retire_hours(meta, default_hours):
    """The ephemeral-watch retire window in hours, or None meaning 'never retire them'.

    Deliberately the same grammar as resolve_retire_days — never / 0 / false / null / "" /
    any non-integer → None — so there is one vocabulary to learn for both windows, and the
    strict-integer gate that started life as a shell-injection guard is not quietly weaker
    on the newer knob.
    """
    raw = meta.get("retire_ephemeral_after_hours", default_hours)
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "0", "false", "never", "null"):
        return None
    if not s.isdigit():
        return None
    hours = int(s)
    return hours if hours > 0 else None


def _thread_epoch(w):
    try:
        return float(w.get("thread_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _finite_epoch(value):
    """A positive finite epoch, or 0.0 when absent/malformed."""
    try:
        epoch = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not (epoch > 0 and epoch == epoch and epoch not in (float("inf"), float("-inf"))):
        return 0.0
    return epoch


def _thread_activity_epoch(w):
    """Newest durable evidence that this thread watch is still wanted."""
    return max(
        _thread_epoch(w),
        _finite_epoch(w.get("created_ts")),
        _finite_epoch(w.get("last_activity_ts")),
    )


def malformed_thread(w):
    """A slack_thread with no usable parent ts. Keep it, but surface it."""
    return w.get("type") == "slack_thread" and _thread_epoch(w) <= 0


def retire_thread(w, cutoff_epoch):
    """A slack_thread whose latest known activity is older than the cutoff."""
    if cutoff_epoch is None:
        return False
    if malformed_thread(w):
        return False
    return w.get("type") == "slack_thread" and _thread_activity_epoch(w) < cutoff_epoch


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


def _created_epoch(w):
    """created_ts as a finite epoch, or 0.0 meaning 'unknown, backfill it'.

    NaN and the infinities are screened out explicitly, not for tidiness: every comparison
    against NaN is False, so a "nan" created_ts would fail BOTH the `<= 0` backfill test and
    the `< cutoff` retire test, and the watch would live forever — the exact immortality this
    rule exists to end. Folding non-finite into 0.0 routes it through the backfill instead.
    """
    try:
        v = float(w.get("created_ts") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v not in (float("inf"), float("-inf")) else 0.0


def retire_ephemeral(w, cutoff_epoch, now):
    """A watch explicitly MARKED ephemeral, older than the window. cutoff_epoch None → never.

    Keyed on a declared `ephemeral: true` flag, NOT on the channel looking like a DM. That
    distinction is the whole rule. The first draft inferred intent from channel_id starting
    with "D", which is wrong in both directions and would have deleted two live quests:
    one quest monitors a bot's DMs and another monitors a single colleague's DM channel —
    both are the quest's actual job, both sit on a `D…` channel, and both would have vanished
    once the window elapsed. Meanwhile a reply-catcher on a real `C…` channel would never
    have expired at all. Intent is not derivable from the channel id, so the creator
    records it and this rule only reads it.

    Applies to any watch type: what expires is the reply-catcher role, not a Slack surface.

    An entry with no created_ts predates that field. Its age is UNKNOWABLE — last_checked_ts
    is a watermark, not a birth date — so it is backfilled to now and retired on a later run
    rather than deleted on a guess. Deleting a live watch is the worst outcome this file can
    produce, so unknown age fails safe toward keeping.
    """
    if cutoff_epoch is None:
        return False
    if w.get("ephemeral") is not True:
        return False
    created = _created_epoch(w)
    if created <= 0:
        w["created_ts"] = f"{now:.6f}"   # start the clock; never retire blind
        return False
    return created < cutoff_epoch


def partition(watches, cutoff_epoch, done_ids, ephemeral_cutoff_epoch=None, now=0.0):
    """Split watches into kept and a per-reason count of retired.

    Not pure in one narrow, deliberate way: retire_ephemeral backfills a missing created_ts
    onto the entry. `mutated` reports that so the caller writes the file even when nothing
    was retired, otherwise the clock restarts every run and the watch never ages.

    A watch is retired for at most one reason; the reasons are disjoint by type, so order
    does not matter, but they are checked in a fixed order for a stable count.
    """
    kept = []
    counts = {
        "slack_thread": 0,
        "approval": 0,
        "schedule": 0,
        "ephemeral": 0,
        "malformed_slack_thread": 0,
    }
    before = [w.get("created_ts") for w in watches]
    for w in watches:
        if malformed_thread(w):
            counts["malformed_slack_thread"] += 1
            kept.append(w)
        elif retire_thread(w, cutoff_epoch):
            counts["slack_thread"] += 1
        elif retire_approval(w, done_ids):
            counts["approval"] += 1
        elif retire_schedule(w):
            counts["schedule"] += 1
        elif retire_ephemeral(w, ephemeral_cutoff_epoch, now):
            counts["ephemeral"] += 1
        else:
            kept.append(w)
    mutated = before != [w.get("created_ts") for w in watches]
    return kept, counts, mutated


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
    default_days = int(os.environ.get("YAAS_RETIRE_DEFAULT_DAYS") or 14)
    meta = _load(meta_path, {})
    days = resolve_retire_days(meta, default_days)
    cutoff = None if days is None else (now - days * 86400)

    approvals = _load(approvals_path, {})
    done_ids = {i.get("id") for i in approvals.get("items", [])
                if isinstance(i, dict) and i.get("status") in ("executed", "cancelled")}

    # The ephemeral window. Same vocabulary as the thread window (never/0/false/non-integer
    # → never). A quest rarely needs to override this: a watch that should persist simply is
    # not marked ephemeral, so intent lives on the watch rather than in a per-quest exception.
    eph_default = os.environ.get("YAAS_RETIRE_EPHEMERAL_HOURS") or 168
    eph_hours = resolve_retire_hours(meta, eph_default)
    eph_cutoff = None if eph_hours is None else (now - eph_hours * 3600)

    lock_path = watch_path + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        watch = _load(watch_path, None)
        if not isinstance(watch, dict):
            return 0
        watches = watch.get("watches") or []
        kept, counts, mutated = partition(watches, cutoff, done_ids, eph_cutoff, now)

        if len(kept) == len(watches) and not mutated:
            if counts["malformed_slack_thread"]:
                print(f"Kept {counts['malformed_slack_thread']} malformed slack_thread watch(es) "
                      f"(missing or invalid thread_ts)")
            return 0  # nothing retired and no clock started; leave the file untouched

        watch["watches"] = kept
        _write_atomic(watch_path, watch)

    # One line per category, echoing the shell's phrasing so operators see no change in logs.
    if counts["slack_thread"]:
        dd = "?" if days is None else days
        print(f"Retired {counts['slack_thread']} stale slack_thread watch(es) "
              f"(last activity older than {dd}d)")
    if counts["approval"]:
        print(f"Retired {counts['approval']} completed approval watch(es)")
    if counts["schedule"]:
        print(f"Retired {counts['schedule']} fired one-shot schedule watch(es)")
    if counts["ephemeral"]:
        print(f"Retired {counts['ephemeral']} expired ephemeral watch(es) "
              f"(created more than {eph_hours}h ago)")
    if counts["malformed_slack_thread"]:
        print(f"Kept {counts['malformed_slack_thread']} malformed slack_thread watch(es) "
              f"(missing or invalid thread_ts)")
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
