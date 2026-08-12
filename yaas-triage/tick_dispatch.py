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
tick_dispatch.py — the dispatch-phase GATES of the tick.py orchestrator.

Once check_quest has produced the dirty target list, two decisions stand between it and
spending money, and both used to live inline in the original shell orchestrator as untested shell:

  slack_gate    Slack is a hard dependency for some targets and irrelevant to others. If
                Slack is down, drop ONLY the targets that need it (reactions, or a quest with
                a dirty slack_* watch) and let email/Jira/PR targets through. Getting this
                wrong stalls an email-only quest behind a Slack outage it never depended on —
                which is exactly the ~183-events-a-day stall this per-target gate replaced.

  slice_plan    Each target is a full paid agent invocation holding the tick's flock. Three
                caps bound the spend: MAX_FANOUT (how many targets a single tick may dispatch),
                the TICK_BUDGET (wall-seconds the whole dispatch phase may consume), and a
                MIN_SLICE floor (a target that cannot get at least MIN_SLICE seconds of budget
                is deferred rather than started and killed moments later). Each dispatch's
                watchdog is capped at the REMAINING budget so a target launched near the ceiling
                cannot run a further full WORKER_TIMEOUT past it.

Both are PURE: they take the target list, the dirty-watch records, the clock/budget as data
and return a plan. They run no agent, read no file, spend nothing. tick.py executes the plan —
opening the ack manifest, running run-agent.py, and committing via ledger/commit.py + the ack
ledger, none of which this module reimplements (that glue is thin and already covered by the
differential goldens, the same split that kept run_checks() thin in tick_check.py).
"""

import json
import sys


REACTIONS = "reactions"


def needs_slack(target, dirty_watches):
    """Does THIS target need Slack THIS tick?

    Judged on the DIRTY watches that triggered the dispatch, not on every watch the quest owns:
    a quest with one clean Slack watch and a dirty email watch must NOT be gated by a Slack
    outage it does not depend on this tick. `reactions` always needs Slack. A quest needs it iff
    at least one of ITS dirty watches is slack_*. A quest with dirty watches, none slack-shaped,
    does not. (The shell had a whole-quest watch.json fallback for the post-run infra guard when
    no dirty record existed; here the caller always has the dirty list, so the fallback is the
    caller's concern, not this pure gate's.)
    """
    if target == REACTIONS:
        return True
    mine = [w for w in dirty_watches if w.get("quest_id") == target]
    return any(str(w.get("type", "")).startswith("slack_") for w in mine)


def slack_gate(targets, slack_ok, dirty_watches):
    """Split the dispatch targets into (kept, gated) given whether Slack is reachable.

    slack_ok True  → nothing gated; every target kept in order.
    slack_ok False → targets that need Slack are gated (watermarks held, re-surface next tick);
                     the rest are kept. Order is preserved in both lists.

    The gate only pings Slack (the caller's job) when at least one target needs it; when Slack
    is up this returns everything unchanged regardless.
    """
    if slack_ok:
        return list(targets), []
    kept, gated = [], []
    for t in targets:
        (gated if needs_slack(t, dirty_watches) else kept).append(t)
    return kept, gated


def slice_plan(targets, tick_budget, spent, min_slice, max_fanout, worker_timeout):
    """Decide which targets dispatch this tick and with what watchdog timeout each.

    targets         ordered dispatch targets (post slack_gate).
    tick_budget     wall-seconds the whole dispatch phase may consume.
    spent           wall-seconds already consumed by earlier dispatches THIS tick.
    min_slice       a target must get at least this many seconds or it is deferred.
    max_fanout      at most this many targets may dispatch in one tick.
    worker_timeout  the normal per-worker watchdog ceiling.

    Returns {"dispatch": [{"target", "timeout"}...], "deferred": [{"target", "reason"}...]}.
    A target's timeout is min(worker_timeout, remaining budget) so a dispatch cannot run a full
    worker_timeout past the tick budget. `spent` is threaded forward optimistically by each
    granted timeout, which is the conservative direction: it can only defer MORE, never spend
    past the ceiling. Deferred targets keep their watermarks (the caller does not commit them),
    so the next tick re-detects them — deferral is not loss.
    """
    dispatch, deferred = [], []
    running = spent
    granted = 0
    for t in targets:
        if granted >= max_fanout:
            deferred.append({"target": t, "reason": f"fanout cap {max_fanout} reached"})
            continue
        remaining = tick_budget - running
        if remaining < min_slice:
            deferred.append({
                "target": t,
                "reason": f"remaining budget {remaining}s < min slice {min_slice}s"})
            continue
        timeout = min(worker_timeout, remaining)
        dispatch.append({"target": t, "timeout": timeout})
        running += timeout
        granted += 1
    return {"dispatch": dispatch, "deferred": deferred}


def _json_arg(i, default):
    return json.loads(sys.argv[i]) if len(sys.argv) > i and sys.argv[i] not in ("", "null") else default


def main():
    # CLI shim for tests, mirroring tick_check.py's shape.
    #   tick_dispatch.py slack-gate  '<targets>' <slack_ok 0|1> '<dirty_watches>'
    #   tick_dispatch.py slice        '<targets>' <budget> <spent> <min_slice> <max_fanout> <worker_timeout>
    #   tick_dispatch.py needs-slack  <target> '<dirty_watches>'
    if len(sys.argv) < 2:
        print("usage: tick_dispatch.py slack-gate|slice|needs-slack ...", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "needs-slack":
        print(json.dumps(needs_slack(sys.argv[2], _json_arg(3, []))))
    elif cmd == "slack-gate":
        kept, gated = slack_gate(_json_arg(2, []), sys.argv[3] == "1", _json_arg(4, []))
        print(json.dumps({"kept": kept, "gated": gated}))
    elif cmd == "slice":
        print(json.dumps(slice_plan(
            _json_arg(2, []), int(sys.argv[3]), int(sys.argv[4]),
            int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7]))))
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
