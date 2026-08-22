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
plan.py — the pure dispatch-planning decisions.

The eligibility layer of a tick is mostly glue and I/O: the spend ceilings already live in
dispatch/spend-window.py, the Slack health gate is a live network probe, and the fan-out and
budget admission is genuinely stateful on runtime dispatch DURATIONS (TICK_SPENT only exists
after each worker returns). None of that can honestly be made pure.

Two pieces can, and they are the two that were inline in the original shell orchestrator and untested:

  rotate()        the fairness rotation. Which order to try the dirty targets in, so that a
                  dirty set permanently larger than the fan-out cap does not starve the
                  targets at the back. A persisted cursor walks the start position forward.
                  This is where a real bug hid: with an UNSORTED input the rotation shuffled
                  rather than distributed, because the base order was nondeterministic
                  (quests are checked in parallel). The caller must pass a stable order; this
                  function assumes it and rotates it.

  breaker_open()  the per-target hourly circuit breaker. The rolling spend cap stops a runaway
                  but does not say which target caused it; this bounds any single target that
                  is looping, whatever the reason.

Both are tiny. They live here so they are callable and testable in isolation, and so the
cursor arithmetic that the rotation depends on is in one place rather than smeared across the
shell.

Usage:
    plan.py rotate '<targets json array>' <cursor>
        → {"order": [...], "offset": N, "next_cursor": M}
    plan.py breaker-open <recent_1h> <cap>
        → prints "true" / "false"; exit 0 if open (blocked), 1 if not
"""

import json
import sys


def rotate(targets, cursor):
    """Rotate a STABLE list of targets forward by cursor positions.

    Returns the rotated order, the offset used, and the next cursor to persist.

    The input order matters: this distributes fairly only if `targets` is already stable
    (triage sorts it). Given [a,b,c] and cursor 1 → [b,c,a]; cursor 4 with 3 targets → offset
    1 → [b,c,a] again (the cursor is unbounded and taken modulo the count). An empty list
    rotates to empty with offset 0. A negative or non-integer cursor is treated as 0, because
    the dangerous outcome is a crash in the tick, not a slightly unfair order.
    """
    n = len(targets)
    if n == 0:
        return {"order": [], "offset": 0, "next_cursor": 0}
    try:
        c = int(cursor)
    except (TypeError, ValueError):
        c = 0
    if c < 0:
        c = 0
    offset = c % n
    order = [targets[(offset + i) % n] for i in range(n)]
    # next_cursor is advanced past the targets this tick will actually try, so the next tick
    # starts further along. The shell passes how many it dispatched; here we expose the
    # starting offset and let the caller add its dispatched count, matching the old
    # `_OFFSET + DISPATCHED` arithmetic. We return offset for exactly that.
    return {"order": order, "offset": offset, "next_cursor": offset}


def breaker_open(recent_1h, cap):
    """True when a single target has been dispatched at or above `cap` times in the last hour.

    A tripped breaker holds the target this tick, whatever the cause — a looping quest, a
    misfiring checker — and complements the per-item no-progress counter. Non-integer inputs
    fail CLOSED (breaker open / blocked): if we cannot read the recent count, the safe move is
    to withhold the dispatch rather than let a possible loop run.
    """
    try:
        recent = int(recent_1h)
        limit = int(cap)
    except (TypeError, ValueError):
        return True
    return recent >= limit


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if args[0] == "rotate":
        if len(args) != 3:
            print("usage: plan.py rotate '<targets json>' <cursor>", file=sys.stderr)
            return 2
        try:
            targets = json.loads(args[1])
            if not isinstance(targets, list):
                raise ValueError("targets must be a JSON array")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps(rotate(targets, args[2])))
        return 0

    if args[0] == "breaker-open":
        if len(args) != 3:
            print("usage: plan.py breaker-open <recent_1h> <cap>", file=sys.stderr)
            return 2
        is_open = breaker_open(args[1], args[2])
        print("true" if is_open else "false")
        return 0 if is_open else 1

    print(f"unknown command: {args[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
