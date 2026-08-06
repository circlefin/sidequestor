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
rotate-logs.py — daily rotation of the append-only state files.

Previously rotate-logs.sh: 139 lines, of which 68 were three separate Python heredocs
with three separate hand-rolled atomic writes. The shell contributed a sentinel check
and a glob.

Self-gated: called on every tick, does nothing unless 23 hours have passed. That is
deliberate, so nothing needs to schedule it.

Rotates:
  state/run-log.ndjson             keep 7 days  -> run-log-archive-YYYY-MM.ndjson
  state/quests/*/timeline.ndjson   keep last 100 -> timeline.archive.ndjson
  state/pending-approvals.json     drop executed/cancelled older than 30 days

Env:
  YAAS_ROTATE_REPO_ROOT   point at a fixture tree (tests)
  YAAS_ROTATE_FORCE=1     ignore the 23-hour sentinel (tests)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

INTERVAL_SEC     = 82800   # 23h, so a daily tick never misses by drift
RUNLOG_KEEP_DAYS = 7
TIMELINE_KEEP    = 100
APPROVAL_KEEP_DAYS = 30


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp}  rotate-logs: {msg}", file=sys.stderr)


def write_atomic(path: Path, text: str):
    """The one durable write. Three separate copies of this used to live in this file
    alone, which is the same duplication that made the locking story inconsistent
    across the wider codebase."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_lines(path: Path, lines):
    if not lines:
        return
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def rotate_runlog(state: Path):
    path = state / "run-log.ndjson"
    if not path.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RUNLOG_KEEP_DAYS)).strftime("%Y-%m-%dT")
    keep, archive = [], []
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line:
            continue
        try:
            ts = json.loads(line).get("ts", "")
        except Exception:
            keep.append(line)      # unparseable lines are kept, never silently dropped
            continue
        (archive if ts < cutoff else keep).append(line)
    if not archive:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m")
    append_lines(state / f"run-log-archive-{stamp}.ndjson", archive)
    write_atomic(path, "\n".join(keep) + ("\n" if keep else ""))
    log(f"run-log: kept {len(keep)}, archived {len(archive)}")


def rotate_timelines(state: Path):
    for bucket in ("active", "completed"):
        for timeline in (state / "quests" / bucket).glob("*/timeline.ndjson"):
            lines = [l for l in timeline.read_text().splitlines() if l.strip()]
            if len(lines) <= TIMELINE_KEEP:
                continue
            overflow, keep = lines[:-TIMELINE_KEEP], lines[-TIMELINE_KEEP:]
            append_lines(timeline.parent / "timeline.archive.ndjson", overflow)
            write_atomic(timeline, "\n".join(keep) + "\n")
            log(f"{timeline}: kept {len(keep)}, archived {len(overflow)}")


def prune_approvals(state: Path):
    path = state / "pending-approvals.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        log("pending-approvals.json unreadable, left alone")
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=APPROVAL_KEEP_DAYS)).isoformat()
    items = data.get("items", [])
    kept = [i for i in items
            if not (isinstance(i, dict)
                    and i.get("status") in ("executed", "cancelled")
                    and i.get("created_at", "9999") < cutoff)]
    if len(kept) == len(items):
        return
    data["items"] = kept
    write_atomic(path, json.dumps(data, indent=2))
    log(f"pending-approvals: kept {len(kept)}, pruned {len(items) - len(kept)}")


def main():
    repo = Path(os.environ.get("YAAS_ROTATE_REPO_ROOT")
                or Path(__file__).resolve().parent.parent)
    state = repo / "state"
    sentinel = state / "last_rotation.ts"
    now = int(datetime.now(timezone.utc).timestamp())

    if os.environ.get("YAAS_ROTATE_FORCE") != "1":
        try:
            last = int(float(sentinel.read_text().strip()))
        except (OSError, ValueError):
            last = 0
        if now - last < INTERVAL_SEC:
            return 0

    log("starting")
    for step in (rotate_runlog, rotate_timelines, prune_approvals):
        try:
            step(state)
        except Exception as exc:
            # One failing rotation must not block the others or the sentinel, or a
            # single bad file wedges rotation forever and the logs grow unbounded.
            log(f"{step.__name__} failed: {type(exc).__name__}: {exc}")

    state.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(str(now))
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
