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
checker-health.py — per-watch exponential backoff for checker failures.

Why this exists
───────────────
A checker `error` used to mark its quest DIRTY, which dispatched the most
expensive component in the system. Anything that fails repeatably — an expired
credential, a changed upstream response shape, a revoked Jira permission, a DNS
failure — therefore woke a paid agent every 60 seconds indefinitely, and the agent
could do nothing about it because the failure was in the checker, not the work.

An LLM dispatch is never the retry mechanism for a checker failure. So an `error`
now holds the watermark (nothing is buried) and backs off, and after enough
consecutive failures it is promoted to `misconfig`, which is permanent, visible,
and stops dispatching until a human intervenes.

Backoff is 60s doubling to a 1h cap: 60, 120, 240, 480, 960, 1920, 3600, 3600 …

State: state/triage/checker-health.json, keyed by watch_id.

    {
      "watch-a1b2c3d4e5f6a7b8": {
        "consecutive_errors": 3,
        "next_retry_ts":      "1785921600.000000",
        "last_error":         "non-json response: <html>502 Bad Gateway",
        "first_error_utc":    "2026-08-05T09:14:02Z",
        "last_error_utc":     "2026-08-05T09:22:11Z"
      }
    }

Sub-commands
────────────
fail <watch_id> [reason]
    Record a failure. Prints the new consecutive_errors count.

ok <watch_id>
    Clear a watch's failure state. Prints "cleared" if there was one, else "noop".
    Triage only calls this when the watch actually has an entry, so the common
    healthy path costs no process at all.

due <watch_id>
    Exit 0 if the watch may be checked now (no entry, or the backoff has elapsed).
    Exit 1 if it is still backing off; prints the remaining seconds.

prune [days]
    Drop entries whose last error is older than `days` (default 30). Guards
    against unbounded growth from watches that have since been deleted.
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _repo_root(start):
    """The repo root is the nearest ancestor directory that contains yaas-triage/.

    NOT counted as `parent.parent`: that is correct only while every script sits directly
    in yaas-triage/, and silently resolves to yaas-triage/ itself once a script moves into
    a subdirectory, producing a parallel state/ tree nothing reads. NOT keyed on CLAUDE.md
    (a fresh clone has only CLAUDE.example.md) and NOT on .git (two git dirs here, none in
    fixtures). Ambient $REPO_ROOT is deliberately ignored: a stale value pointing at another
    checkout would pass any marker check and silently redirect writes. Test fixtures copy
    the whole tree, so the walk-up finds the fixture on its own.

    Kept byte-identical across every file that needs it; tests/behaviour/repo-root.test.sh
    asserts that, because a shared module would need sys.path handling whose own path is
    depth-dependent, which is the bug being fixed.
    """
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT   = _repo_root(__file__)
STATE_DIR   = REPO_ROOT / "state" / "triage"
HEALTH_FILE = STATE_DIR / "checker-health.json"
LOCK_FILE   = STATE_DIR / "checker-health.lock"

BASE_BACKOFF = 60
MAX_BACKOFF  = 3600


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_for(n: int) -> int:
    """60s doubling, capped at an hour. n is the new consecutive-error count."""
    if n < 1:
        return BASE_BACKOFF
    return min(BASE_BACKOFF * (2 ** (n - 1)), MAX_BACKOFF)


def _read() -> dict:
    try:
        with open(HEALTH_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        # An unreadable health file must not wedge checking. Treat as "no known
        # failures" — worst case we retry something that is still broken, which is
        # the pre-existing behaviour and self-corrects on the next failure.
        return {}


def _mutate(fn):
    """Apply fn(data) under an exclusive lock on a SIDECAR lockfile, then write
    atomically. check_quest runs several quests in parallel, so two checkers can
    fail in the same instant; without the lock one increment would be lost."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _read()
            result = fn(data)
            tmp = HEALTH_FILE.with_name(HEALTH_FILE.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, HEALTH_FILE)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def cmd_fail(watch_id: str, reason: str = ""):
    def apply(data):
        rec = data.get(watch_id) or {}
        n = int(rec.get("consecutive_errors", 0)) + 1
        rec["consecutive_errors"] = n
        rec["next_retry_ts"] = f"{time.time() + _backoff_for(n):.6f}"
        rec["last_error"] = (reason or "")[:300]
        rec["first_error_utc"] = rec.get("first_error_utc") or _now_utc()
        rec["last_error_utc"] = _now_utc()
        data[watch_id] = rec
        return n
    print(_mutate(apply))


def cmd_ok(watch_id: str):
    def apply(data):
        return "cleared" if data.pop(watch_id, None) is not None else "noop"
    print(_mutate(apply))


def cmd_due(watch_id: str):
    rec = _read().get(watch_id)
    if not rec:
        return
    try:
        retry_at = float(rec.get("next_retry_ts") or 0)
    except (TypeError, ValueError):
        return
    remaining = retry_at - time.time()
    if remaining > 0:
        print(f"{int(remaining)}s remaining (failure {rec.get('consecutive_errors', '?')})")
        sys.exit(1)


def cmd_prune(days: str = "30"):
    try:
        cutoff = time.time() - float(days) * 86400
    except ValueError:
        print(f"error:bad_days:{days}", file=sys.stderr)
        sys.exit(2)

    def apply(data):
        drop = []
        for k, rec in data.items():
            stamp = (rec or {}).get("last_error_utc") or ""
            try:
                t = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t < cutoff:
                drop.append(k)
        for k in drop:
            data.pop(k, None)
        return len(drop)
    n = _mutate(apply)
    if n:
        print(f"pruned {n} stale checker-health entr(ies)")


def main():
    if len(sys.argv) < 3 and (len(sys.argv) < 2 or sys.argv[1] != "prune"):
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "fail":
        cmd_fail(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    elif cmd == "ok":
        cmd_ok(sys.argv[2])
    elif cmd == "due":
        cmd_due(sys.argv[2])
    elif cmd == "prune":
        cmd_prune(sys.argv[2] if len(sys.argv) > 2 else "30")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
