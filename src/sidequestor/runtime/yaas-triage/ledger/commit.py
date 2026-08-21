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
commit.py — the commit predicate, as a pure function.

This is the single most safety-critical decision in the system: which watermarks move, and
to what value. Get it wrong in the advancing direction and a message is buried with no error
and no trace. It used to live as inline `jq` inside the original shell orchestrator's commit_quest, where it could
not be unit-tested and where two silent-loss bugs hid long enough to reach production.

`decide()` takes a snapshot and returns a decision. It does NO I/O: it does not read
watch.json, write watermarks, or log. The orchestrator owns the actual write and logging;
only the reasoning lives here, so it can be tested directly.

THE RULE. A watch commits only when ALL THREE hold:
  1. it was dispatched this tick (i.e. it appears in dirty_watches for this quest)
  2. the worker closed it handled or nothing_to_do (i.e. it is in `acked`)
  3. the checker proved it drained its window (i.e. its `complete` is not False)

The "advance_to" a committed watch carries may be null; the orchestrator resolves that to
"now minus the type's lag" at write time. That fallback deliberately stays there so
this function has no clock dependency and its output is exactly comparable across runs.

Usage (from the orchestrator):
    decide '<snapshot json>'
prints:
    {"moves":[{"watch_id","advance_to"}], "committed_ids":[...], "truncated":N}
"""

import json
import sys


def decide(snapshot):
    """Pure commit decision. See module docstring for the contract.

    snapshot keys:
      quest_id        the quest being committed
      acked           watch_ids closed handled|nothing_to_do (already the committable set)
      dirty_watches   this tick's dirty records: [{quest_id, watch_id, type, complete,
                      advance_to}, ...]  (the whole tick's list; this filters by quest_id)
    """
    qid = snapshot["quest_id"]
    acked = set(snapshot.get("acked") or [])

    # Only this quest's dirty records, only the acked ones. Preserve input order so the
    # output is deterministic and diffable.
    mine = [w for w in (snapshot.get("dirty_watches") or [])
            if w.get("quest_id") == qid and w.get("watch_id") in acked]

    # ── The three-condition commit ─────────────────────────────────────────────────────
    # complete is False only when the checker could not prove it drained its window; those
    # are held with a backlog note rather than advanced, so unseen older items are not
    # skipped.
    moves = []
    committed_ids = []
    truncated = 0
    for w in mine:
        wid = w.get("watch_id")
        if w.get("complete") is False:
            truncated += 1
            continue
        moves.append({"watch_id": wid, "advance_to": w.get("advance_to")})
        committed_ids.append(wid)

    # The orchestrator routes gate_dispatch_unacked on whether anything was acked. That is NOT the
    # same as committed_ids being empty, because a
    # truncated (complete=false) watch is still acked and still takes the success path with a
    # backlog note. Sorted for a stable, diffable result.
    return {
        "moves": moves,
        "committed_ids": committed_ids,
        "truncated": truncated,
        "acked": sorted(acked),
    }


def main():
    if len(sys.argv) != 2:
        print("usage: commit.py '<snapshot json>'", file=sys.stderr)
        return 2
    try:
        snap = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"error: invalid snapshot json: {e}", file=sys.stderr)
        return 2
    print(json.dumps(decide(snap)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
