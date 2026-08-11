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
snapshot.py — reduce a finished tick to a comparable verdict.

The whole differential harness rests on this file. If it compares too little, a
regression slips through; if it compares raw bytes, every run differs because of
timestamps and pids and the harness cries wolf until someone stops trusting it.

So it captures WHAT THE TICK DECIDED, not what it wrote:

  watches    per watch: did its watermark hold, advance to an exact value, or advance
             to "now"? Plus whether the entry was appended, removed or rewritten.
  dispatches which targets ran, in order.
  acks       per run: which items were closed and how.
  events     the run-log event sequence (gate decisions, skips, breaches).
  exit       the orchestrator's exit code.

Time is the hard part. A watermark set to "now minus the type's lag" differs between
two runs by construction, so comparing the number is useless. What matters is the
CLASSIFICATION: held, advanced to a value the checker supplied, or advanced to now.
That is time-independent and is exactly the property the commit predicate decides.

Usage:
    python3 snapshot.py <fixture_dir> [--exit N] > snapshot.json
    python3 snapshot.py --diff <a.json> <b.json>
"""

import json
import re
import sys
from pathlib import Path

# Fields whose values legitimately differ between two identical runs.
VOLATILE = {"ts", "run_id", "holder_pid", "pid", "wall_sec", "wall", "started",
            "tick_started_utc", "completed_utc", "duration_sec", "cost_usd",
            "log", "ndjson", "stamp", "opened_utc", "acked_utc", "now"}


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _classify(before, after, advance_to):
    """How did this watermark move? Time-independent by design."""
    if after == before:
        return "held"
    if advance_to is not None and str(after) == str(advance_to):
        # Deliberately WITHOUT the number. Scenario times are relative to the real
        # clock, so embedding the value would make the golden differ on every run.
        # That it equals what the checker supplied is the whole property.
        return "advanced_to_checker_value"
    # Anything else is the "now minus lag" branch. We deliberately do NOT record the
    # number: it is a function of wall-clock time and would differ every run.
    return "advanced_to_now"


def snapshot(fixture, exit_code=None):
    fixture = Path(fixture)
    sc = _load_json(fixture / "scenario.json", {}) or {}
    verdicts = sc.get("checkers", {})
    # Report by the scenario's readable alias, not the minted hash, so a golden diff is
    # legible and stays stable if the id scheme ever changes.
    alias = sc.get("_aliases", {})

    out = {"watches": {}, "planned": [], "dispatches": [], "acks": {}, "events": [],
           "exit": exit_code, "quests": {}}

    # ── Watches: the only state whose movement is a correctness question ────────
    for qid, q in sorted(sc.get("quests", {}).items()):
        before = {w["watch_id"]: w for w in q.get("watches", [])}
        wj = _load_json(fixture / "state" / "quests" / "active" / qid / "watch.json", {})
        # A quest that moved to completed/ or archived/ is not a missing quest.
        located = "active"
        if wj is None or wj == {}:
            for other in ("completed", "archived"):
                alt = fixture / "state" / "quests" / other / qid / "watch.json"
                if alt.exists():
                    wj = _load_json(alt, {})
                    located = other
                    break
        out["quests"][qid] = located
        after = {w.get("watch_id"): w for w in (wj or {}).get("watches", [])}

        for wid, w in sorted(before.items()):
            a = alias.get(wid, wid)
            v = verdicts.get(a) or verdicts.get(wid) or verdicts.get(w.get("type")) or {}
            if wid not in after:
                out["watches"][f"{qid}/{alias.get(wid, wid)}"] = "REMOVED"
                continue
            moved = _classify(str(w.get("last_checked_ts", "")),
                              str(after[wid].get("last_checked_ts", "")),
                              v.get("advance_to"))
            # A rewritten field other than the watermark is an append-only violation,
            # which watch-guard is supposed to revert. Surface it separately so a
            # regression there cannot hide behind a correct watermark.
            rewritten = sorted(k for k in w
                               if k != "last_checked_ts" and w.get(k) != after[wid].get(k))
            out["watches"][f"{qid}/{a}"] = (
                moved if not rewritten else f"{moved}+REWROTE:{','.join(rewritten)}")
        # Appended watches get a positional key. Their minted id is a hash of their
        # own fields, which include now-relative timestamps, so keying by id would
        # change every run.
        for i, wid in enumerate(sorted(set(after) - set(before))):
            out["watches"][f"{qid}/+appended[{i}]"] = f"APPENDED:{after[wid].get('type')}"

    # ── Run log: the decision trail, minus the volatile fields ─────────────────
    def dealias(o):
        """Swap minted watch_ids back to aliases anywhere they appear in a record."""
        if isinstance(o, dict):
            return {k: dealias(v) for k, v in o.items()}
        if isinstance(o, list):
            return [dealias(v) for v in o]
        return alias.get(o, o) if isinstance(o, str) else o

    run_log = fixture / "state" / "run-log.ndjson"
    for line in (run_log.read_text().splitlines() if run_log.exists() else []):
        rec = _load_json_line(line)
        if not rec:
            continue
        ev = dealias({k: v for k, v in rec.items() if k not in VOLATILE})
        # `gate_dispatch` carries the PLAN (which targets were selected, in order);
        # `gate_dispatch_tokens` fires once per agent actually invoked. Keeping both
        # separates "what we decided to run" from "what really ran", which is exactly
        # where a gate regression hides.
        if rec.get("event") == "gate_dispatch":
            out["planned"] = rec.get("targets") or []
        if rec.get("event") == "gate_dispatch_tokens" and rec.get("targets"):
            t = rec["targets"]
            out["dispatches"].extend(t if isinstance(t, list) else [t])
        out["events"].append(ev)

    # ── Ack manifests: the evidence the commit was based on ────────────────────
    # ack-watch.py writes these as state/triage/dispatch-run-<stamp>-<pid>-<n>.json.
    # An earlier version of this globbed a "manifests/" subdirectory that has never
    # existed, so every golden recorded acks:{} and the whole section verified nothing.
    man_dir = fixture / "state" / "triage"
    for man in sorted(man_dir.glob("dispatch-run-*.json")):
        m = _load_json(man, {}) or {}
        key = f"{m.get('target', '?')}/{m.get('kind', '?')}"
        out["acks"][key] = {alias.get(i.get("item_id"), i.get("item_id")): i.get("status", "open")
                            for i in sorted(m.get("items", []),
                                            key=lambda x: str(x.get("item_id")))}
    return out


def _load_json_line(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def diff(a_path, b_path):
    """Print the first meaningful divergence in a form a human can act on."""
    a = _load_json(a_path, {}) or {}
    b = _load_json(b_path, {}) or {}
    problems = []

    for section in ("watches", "acks", "quests"):
        av, bv = a.get(section, {}), b.get(section, {})
        for k in sorted(set(av) | set(bv)):
            if av.get(k) != bv.get(k):
                problems.append(f"{section}[{k}]: golden={av.get(k)!r} got={bv.get(k)!r}")

    for field in ("planned", "dispatches"):
        if a.get(field) != b.get(field):
            problems.append(f"{field}: golden={a.get(field)} got={b.get(field)}")
    if a.get("exit") != b.get("exit"):
        problems.append(f"exit: golden={a.get('exit')} got={b.get('exit')}")

    # Events are compared as an ordered sequence of names. The full records are noisy
    # and their extra fields are not a behavioural contract; the ORDER of gate
    # decisions is.
    an = [e.get("event") for e in a.get("events", [])]
    bn = [e.get("event") for e in b.get("events", [])]
    if an != bn:
        problems.append(f"event sequence:\n    golden={an}\n    got   ={bn}")
    return problems


def main():
    if sys.argv[1:2] == ["--diff"]:
        problems = diff(sys.argv[2], sys.argv[3])
        for p in problems:
            print("  " + p)
        return 1 if problems else 0

    exit_code = None
    if "--exit" in sys.argv:
        exit_code = int(sys.argv[sys.argv.index("--exit") + 1])
    print(json.dumps(snapshot(sys.argv[1], exit_code), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
