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
watch.json, does not write watermarks, does not log. the original shell orchestrator still owns the actual write
(_advance_watches) and the logging, so the golden-observable behaviour is unchanged. The only
thing that moved here is the *reasoning*, so it can be tested against every case directly.

THE RULE. A watch commits only when ALL THREE hold:
  1. it was dispatched this tick (i.e. it appears in dirty_watches for this quest)
  2. the worker closed it handled or nothing_to_do (i.e. it is in `acked`)
  3. the checker proved it drained its window (i.e. its `complete` is not False)

Plus the observe-only evidence veto: a `nothing_to_do` ack on a Slack watch whose channel the
worker's event stream does not show it reading is always flagged, and is additionally dropped
from the committed set when `enforce` is true.

The "advance_to" a committed watch carries may be null; the original shell orchestrator resolves that to
"now minus the type's lag" at write time. That fallback deliberately stays in the shell so
this function has no clock dependency and its output is exactly comparable across runs.

Usage (from the original shell orchestrator, one call replacing several jq expressions):
    decide '<snapshot json>'
prints:
    {"moves":[{"watch_id","advance_to"}], "committed_ids":[...],
     "truncated":N, "unverified":[...], "unverified_enforced":bool}
"""

import json
import sys


def decide(snapshot):
    """Pure commit decision. See module docstring for the contract.

    snapshot keys:
      quest_id        the quest being committed
      acked           watch_ids closed handled|nothing_to_do (already the committable set)
      acked_ntd       subset of `acked` closed specifically nothing_to_do
      dirty_watches   this tick's dirty records: [{quest_id, watch_id, type, complete,
                      advance_to}, ...]  (the whole tick's list; this filters by quest_id)
      watch_entries   the quest's watch.json entries, for channel_id lookup on the veto:
                      [{watch_id, type, channel_id}, ...]
      read_channels   channel ids the worker's stream shows it successfully read
      evidence_available  whether a worker event stream existed to judge reads against; when
                      false the veto is skipped entirely (mirrors the shell's DISPATCH_NDJSON
                      existence guard). Defaults true.
      enforce         when true, drop unverified nothing_to_do watches from the commit. Strict:
                      only JSON true / 1 / "1"; anything else is observe-mode.
    """
    qid = snapshot["quest_id"]
    acked = set(snapshot.get("acked") or [])
    acked_ntd = set(snapshot.get("acked_ntd") or [])
    read_channels = set(snapshot.get("read_channels") or [])
    # STRICT: only a JSON true or the number/string 1 enforces. A stray "0" from a shell
    # caller must fail to OBSERVE mode (advance, just flag), never to enforce (hold). The
    # dangerous direction is holding on a caller typo, so the default leans to advancing.
    _enf = snapshot.get("enforce")
    enforce = _enf is True or _enf == 1 or _enf == "1"
    # The shell only runs the evidence check when the worker's event stream exists. Without
    # it, read_channels is empty for lack of data, NOT because nothing was read — vetoing then
    # would hold legitimate work. Mirror that guard explicitly. Absent key defaults True so an
    # older caller that does not set it keeps the pre-existing behaviour.
    evidence_available = snapshot.get("evidence_available", True)

    # Only this quest's dirty records, only the acked ones. Preserve input order so the
    # output is deterministic and diffable.
    mine = [w for w in (snapshot.get("dirty_watches") or [])
            if w.get("quest_id") == qid and w.get("watch_id") in acked]

    # Channel per watch, for the evidence veto. Taken from watch.json (the dirty record does
    # not carry it), matching the shell, which joined to the watch file.
    chan = {}
    wtype = {}
    for e in (snapshot.get("watch_entries") or []):
        if isinstance(e, dict) and e.get("watch_id"):
            chan[e["watch_id"]] = e.get("channel_id") or ""
            wtype[e["watch_id"]] = e.get("type") or ""

    # ── Evidence veto (observe-only unless enforce) ────────────────────────────────────
    # A nothing_to_do ack on a slack_* watch with a channel the worker did not demonstrably
    # read. Flagged always; removed from the committed set only under enforce. slack_mention
    # has no channel to attribute, so a watch with no channel_id is never vetoed.
    unverified = []
    if evidence_available:
        for wid in (w for w in acked_ntd if w in acked):
            if not wtype.get(wid, "").startswith("slack_"):
                continue
            c = chan.get(wid, "")
            if c and c not in read_channels:
                unverified.append(wid)
    unverified.sort()  # deterministic for logging and goldens; shell ordered by watch.json

    vetoed = set(unverified) if enforce else set()

    # ── The three-condition commit ─────────────────────────────────────────────────────
    # complete is False only when the checker could not prove it drained its window; those
    # are held with a backlog note rather than advanced, so unseen older items are not
    # skipped.
    moves = []
    committed_ids = []
    truncated = 0
    for w in mine:
        wid = w.get("watch_id")
        if wid in vetoed:
            continue
        if w.get("complete") is False:
            truncated += 1
            continue
        moves.append({"watch_id": wid, "advance_to": w.get("advance_to")})
        committed_ids.append(wid)

    # The committable set AFTER the enforced veto. The shell routes gate_dispatch_unacked on
    # exactly this being empty — which is NOT the same as committed_ids being empty, because a
    # truncated (complete=false) watch is still acked and still takes the success path with a
    # backlog note. Sorted for a stable, diffable result.
    acked_after_veto = sorted(acked - vetoed)

    return {
        "moves": moves,
        "committed_ids": committed_ids,
        "truncated": truncated,
        "unverified": unverified,
        "unverified_enforced": enforce and bool(unverified),
        "acked_after_veto": acked_after_veto,
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
