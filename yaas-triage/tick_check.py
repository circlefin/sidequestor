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
tick_check.py — turn a checker's raw result into a per-watch VERDICT.

This is the analyze phase of the original shell orchestrator, and its single subtle decision: given what a checker
reported plus the watch's own history (is it in backoff? has it been dispatched repeatedly with
no progress? is its watch_id even valid?), which of the six verdicts does this watch get —

    misconfig   permanently stuck; hold and page a human (bad watch_id, no checker,
                error/no-progress promoted past its threshold)
    backoff     a transient checker error inside its exponential-backoff window; hold
    skip        a transient/ratelimited upstream; hold and retry next tick, do NOT read as
                dirty (the 2026-07-24 storm came from ratelimited reading as dirty)
    hold        clean, but the checker could not prove it drained its window; cursor must not
                move or unseen older items are skipped
    dirty       genuinely new activity → dispatch
    clean       nothing new, window covered → advance

`classify()` is PURE: it takes the checker result and the watch's health/unacked state as data
and returns the verdict. It runs no checker, reads no file, writes nothing. That is what makes
the six-way routing — where two production incidents hid — unit-testable in isolation, which it
never was inside the shell.

tick.py owns the impure fan-out (running every checker in parallel, capped) and calls classify()
on each result; the backoff/health bookkeeping stays with the existing ledger/checker-health.py.
"""

import json
import sys


# Verdicts. Only `dirty` dispatches; everything else holds or advances. `clean` and `dirty`
# advance the watermark (clean via the lag fallback, dirty via advance_to) IF complete; the
# rest hold it.
MISCONFIG, BACKOFF, SKIP, HOLD, DIRTY, CLEAN = (
    "misconfig", "backoff", "skip", "hold", "dirty", "clean")

# watch_id shape: watch-<hex>[-<digits>]. Anything else is a misconfiguration that holds the
# watermark rather than risk acting on an unidentifiable watch.
import re
_WATCH_ID = re.compile(r"^watch-[0-9a-f]+(-[0-9]+)?$")


def valid_watch_id(wid):
    return bool(wid) and bool(_WATCH_ID.match(wid))


def classify(result, watch, health=None, unacked=0,
             unacked_promote=3, error_promote=6, checker_exists=True):
    """Return a verdict dict for one watch. PURE.

    result   the checker's parsed output: {outcome, count, preview, advance_to, complete}, or
             None if the checker could not be run / produced no parseable output.
    watch    the watch entry: {watch_id, type, ...}.
    health   this watch's checker-health record: {consecutive_errors, next_retry_ts, ...} or
             None if it has never failed.
    unacked  how many times this watch has been dispatched with no progress.
    checker_exists  whether checkers/<type>.py is present and executable.

    Returned dict always has: verdict, and (for dirty/clean) advance_to + complete + count +
    preview; plus reason. It never raises on a malformed result — that routes to misconfig.
    """
    wid = watch.get("watch_id")
    wtype = watch.get("type", "")

    # ── Structural checks first: these hold regardless of what the checker said ─────────
    if not valid_watch_id(wid):
        return _v(MISCONFIG, wtype, wid, reason=f"[{wtype}] invalid or missing watch_id; watermark held")

    if unacked >= unacked_promote:
        return _v(MISCONFIG, wtype, wid,
                  reason=f"[{wtype}] dispatched {unacked} time(s) with no progress; watermark held pending review")

    # In an active backoff window? Hold without running the checker. (The caller decides the
    # window has not yet expired; classify only needs to know it is currently backed off.)
    if health and health.get("in_backoff"):
        return _v(BACKOFF, wtype, wid,
                  reason=f"[{wtype}] in checker backoff until {health.get('next_retry_ts', '?')}")

    if not checker_exists:
        return _v(MISCONFIG, wtype, wid, reason=f"[{wtype}] no executable checker; watermark held")

    # ── Now the checker's own result ────────────────────────────────────────────────────
    if not isinstance(result, dict):
        # Unparseable output: treat as a checker error and route through the backoff/promote
        # logic, so a persistently broken checker is eventually paged, not retried forever.
        result = {"outcome": "error", "preview": "malformed checker result"}

    outcome = result.get("outcome", "error")
    preview = result.get("preview", "")
    advance_to = result.get("advance_to") or None
    complete = result.get("complete", True) is not False
    count = result.get("count", 0)

    if outcome == MISCONFIG:
        return _v(MISCONFIG, wtype, wid, reason=f"[{wtype}] {preview}")

    if outcome == "ratelimited":
        # Transient. Skip — NOT dirty. A ratelimited read reading as dirty is what fed the
        # 2026-07-24 dispatch storm.
        return _v(SKIP, wtype, wid, reason=f"[{wtype}] {preview}")

    if outcome == HOLD:
        # A checker can hold its own watermark directly (github_pr/jira do this on a saturated
        # or tie-boundary page). The shell historically only DERIVED a hold from
        # clean+count=0+complete=false and did not recognize this outcome — so it mismapped to
        # error+backoff. Honour it: hold regardless of count, never advance.
        return _v(HOLD, wtype, wid, complete=False, count=result.get("count", 0),
                  preview=preview, reason=f"[{wtype}] {preview}" if preview else f"[{wtype}] window not drained; cursor held")

    if outcome == "error":
        errn = (health.get("consecutive_errors", 0) if health else 0) + 1
        if errn >= error_promote:
            return _v(MISCONFIG, wtype, wid, error=True, errn=errn,
                      reason=f"[{wtype}] {errn} consecutive checker errors — {preview}")
        return _v(BACKOFF, wtype, wid, error=True, errn=errn,
                  reason=f"[{wtype}] checker error {errn}/{error_promote}, backing off — {preview}")

    # Only clean/dirty route by count. Any other outcome that reached here is unrecognized
    # (the shell mapped it to "unknown outcome" -> error), so route it through the error path
    # rather than let it read as clean and advance the watermark.
    if outcome not in (CLEAN, DIRTY):
        errn = (health.get("consecutive_errors", 0) if health else 0) + 1
        pv = f"unknown outcome '{outcome}'"
        if errn >= error_promote:
            return _v(MISCONFIG, wtype, wid, error=True, errn=errn,
                      reason=f"[{wtype}] {errn} consecutive checker errors — {pv}")
        return _v(BACKOFF, wtype, wid, error=True, errn=errn,
                  reason=f"[{wtype}] checker error {errn}/{error_promote}, backing off — {pv}")

    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 0

    if n == 0 and not complete:
        # Clean, but the window was not drained: cursor must NOT move.
        return _v(HOLD, wtype, wid, complete=False,
                  reason=f"[{wtype}] window saturated with 0 matches; cursor held")

    if n > 0:
        return _v(DIRTY, wtype, wid, advance_to=advance_to, complete=complete,
                  count=n, preview=preview, reason=f"[{wtype}] {n} new — \"{preview}\"")

    return _v(CLEAN, wtype, wid, advance_to=advance_to, complete=complete,
              count=0, preview=preview, reason=f"[{wtype}] clean")


def _v(verdict, wtype, wid, **kw):
    d = {"verdict": verdict, "type": wtype, "watch_id": wid}
    d.update(kw)
    return d


# ── Impure: run the checkers, in parallel, and classify each ────────────────────────────

def rotate_check_order(quest_ids, cursor):
    """Rotate a STABLE list of quest ids forward by `cursor`, so the tick starts somewhere new.

    WHY THIS EXISTS. The Slack token has a finite request budget and a tick spends it in
    order. Whoever is at the back when it runs out gets a rate limit and is skipped — and
    with a fixed order, it is the SAME quests every tick, forever. Measured 2026-08-09: four
    quests were rate-limited on 100% of ticks purely because they sorted last, so messages to
    them went unseen for hours while quests near the front were checked every minute. The
    watermark is held so nothing is lost, but "not lost" is not "noticed".

    Rotation, not randomisation. Random order fixes starvation only on average: a quest can
    lose the draw many times running, so the worst case stays unbounded and irreproducible.
    Rotating by a persisted cursor makes the worst case a GUARANTEE — with N quests, every
    quest reaches the front within N ticks — and keeps a tick replayable, which the
    differential goldens depend on (they replay a recorded sequence and diff the result; a
    random order would make them flap).

    Semantics deliberately mirror dispatch/plan.py rotate(): [a,b,c] with cursor 1 → [b,c,a];
    an unbounded cursor is taken modulo the count; a negative, non-integer or unparseable
    cursor is treated as 0, because the dangerous outcome here is a crashed tick, not a
    slightly unfair order. Returns (order, next_cursor); next_cursor advances by one so the
    starting point walks the list one step per tick.
    """
    n = len(quest_ids)
    if n == 0:
        return [], 0
    try:
        c = int(cursor)
    except (TypeError, ValueError):
        c = 0
    if c < 0:
        c = 0
    offset = c % n
    return [quest_ids[(offset + i) % n] for i in range(n)], offset + 1


def main():
    # CLI shim for tests: classify a single result. args: '<result json>' '<watch json>'
    # [--unacked N] [--errors N] [--in-backoff] [--no-checker]
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: tick_check.py '<result json>' '<watch json>' [--unacked N] "
              "[--errors N] [--in-backoff] [--no-checker]", file=sys.stderr)
        return 2
    result = json.loads(args[0]) if args[0] not in ("", "null") else None
    watch = json.loads(args[1])
    health = None
    if "--errors" in args:
        health = {"consecutive_errors": int(args[args.index("--errors") + 1])}
    if "--in-backoff" in args:
        health = (health or {}); health["in_backoff"] = True; health["next_retry_ts"] = "9999"
    kw = {}
    if "--unacked" in args:
        kw["unacked"] = int(args[args.index("--unacked") + 1])
    if "--no-checker" in args:
        kw["checker_exists"] = False
    print(json.dumps(classify(result, watch, health=health, **kw)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
