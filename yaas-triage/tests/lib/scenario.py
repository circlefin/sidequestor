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
scenario.py — build a throwaway repo that a full triage tick can run against.

This is the foundation of the differential harness. The point is to run the REAL
orchestrator (the orchestrator) end to end with no network, no
Slack, no agent and no launchd, so its decisions can be recorded and compared.

It needs zero instrumentation of the orchestrator, because every external call the
orchestrator makes already goes through a path held in a variable:

    checkers/<type>.py    every watch check          → replaced with a canned verdict
    run-agent.py          the agent invocation       → replaced with a scripted ack writer
    mcp-call.sh           Slack health + reactions   → replaced with a scripted responder
    notify.py             desktop notifications      → replaced with a no-op

So we copy the tree, drop four stubs in, and the orchestrator cannot tell the
difference. Anything NOT stubbed (ack-watch.py, watch-guard.py, spend-window.py,
checker-health.py, the commit logic) is the real code under test.

A scenario is one JSON file. See tests/differential/scenarios/ for examples:

    {
      "name": "one_dirty_watch_acked_handled",
      "now": 1770000000,
      "quests": {
        "quest-demo": {
          "meta":    {"status": "active", "allow_send": false},
          "context": "Demo quest.",
          "watches": [{"watch_id": "w1", "type": "slack_thread",
                       "channel_id": "C1", "thread_ts": "1.0",
                       "last_checked_ts": "1769990000"}]
        }
      },
      "checkers": {"w1": {"outcome": "dirty", "count": 2, "preview": "2 new"}},
      "agent":    {"exit": 0, "acks": {"w1": "handled"}},
      "env":      {"YAAS_TICK_DISPATCH_BUDGET": "600"}
    }

Usage:
    python3 scenario.py build <scenario.json> <dest_dir>
"""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
# YAAS_TRIAGE_SRC lets a caller build fixtures from a COPY of the tree instead of the
# live one. mutations.sh needs that: it deliberately breaks the orchestrator, and doing
# that to the real file means any other test running at the same time silently tests
# broken code (which happened once), and a hard kill leaves the repo mutated.
TRIAGE_DIR = Path(os.environ.get("YAAS_TRIAGE_SRC") or TESTS_DIR.parent).resolve()
REPO_ROOT = TRIAGE_DIR.parent


def watch_id_for(alias):
    """Mint a watch_id that passes triage's validator, from a readable alias.

    triage requires `watch-<16 hex>[-<digits>]` and holds the watermark of anything
    else as a misconfig. Scenarios stay readable by naming watches "w1"/"wa"; this maps
    that to a conforming id deterministically, so goldens are stable across runs.
    """
    return "watch-" + hashlib.sha256(alias.encode()).hexdigest()[:16]


def resolve_times(obj, now):
    """Expand "@now-3600" style tokens into real epoch seconds.

    Scenarios cannot hardcode timestamps: triage compares them against the real clock,
    so a fixed epoch drifts into "stale" and the retire rules silently delete the
    watches before anything can be tested. Every time in a scenario is relative.
    """
    if isinstance(obj, dict):
        return {k: resolve_times(v, now) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_times(v, now) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"@now(?:([+-])(\d+))?(\.\d+)?", obj)
        if m:
            delta = int(m.group(2) or 0) * (-1 if m.group(1) == "-" else 1)
            return f"{now + delta}{m.group(3) or ''}"
    return obj


def _write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ── Stubs ───────────────────────────────────────────────────────────────────────
# Each stub reads the scenario from $YAAS_SCENARIO, so one file drives every seam and
# a scenario stays readable as a single object.

CHECKER_STUB = '''#!/usr/bin/env python3
"""Canned checker. Looks up this watch's verdict in the scenario and prints it.

Keyed by watch_id first, then by type, so a scenario can set one verdict for a whole
class of watches without naming each one. An unlisted watch is clean, which keeps
scenarios short: only the interesting watches need mentioning.
"""
import json, os, sys

entry = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
sc = json.load(open(os.environ["YAAS_SCENARIO"]))
verdicts = sc.get("checkers", {})
wid = entry.get("watch_id")
alias = sc.get("_aliases", {}).get(wid, wid)
v = verdicts.get(alias) or verdicts.get(wid) or verdicts.get(entry.get("type")) or {}

out = {"outcome": v.get("outcome", "clean"), "count": v.get("count", 0),
       "preview": v.get("preview", ""), "reason": v.get("reason", "")}
for k in ("advance_to", "complete"):
    if k in v:
        out[k] = v[k]
print(json.dumps(out))
sys.exit(v.get("exit", 0))
'''

RUN_AGENT_STUB = '''#!/usr/bin/env python3
"""Scripted agent. Writes the acks the scenario dictates, then reports an exit code.

This is where the evidence-based commit gets exercised: the scenario decides which
items the "worker" closes and how, and the real ack-watch.py records them. A scenario
that acks nothing must leave every watermark untouched, which is the single most
important regression to protect.
"""
import json, os, subprocess, sys

args = sys.argv[1:]
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None

run_id = ""
for i, a in enumerate(args):
    if a == "--header" and i + 1 < len(args) and args[i + 1].startswith("Run ID: "):
        run_id = args[i + 1][len("Run ID: "):]

sc = json.load(open(os.environ["YAAS_SCENARIO"]))
agent = sc.get("agent", {})
target = opt("--label") or ""
# Per-target override, else the default block, so a multi-target scenario can make one
# target succeed and another stall.
spec = agent.get("per_target", {}).get(target, agent)

ack_bin = os.path.join(os.environ["YAAS_TRIAGE_DIR"], "ledger", "ack-watch.py")
# Scenarios ack by alias; the manifest holds real watch_ids.
to_real = {a: w for w, a in sc.get("_aliases", {}).items()}
for item, status in (spec.get("acks") or {}).items():
    item = to_real.get(item, item)
    subprocess.run(["python3", ack_bin, "ack", run_id, item, status, "scenario"],
                   capture_output=True)

# Let a scenario simulate a worker that appends a watch (the one mutation the rules
# allow) or one that illegally rewrites an existing entry (watch-guard must revert it).
for extra in spec.get("append_watches", []):
    subprocess.run(["python3", os.path.join(os.environ["YAAS_TRIAGE_DIR"], "ledger", "add-watch.py"),
                    target, json.dumps(extra)], capture_output=True)

# A real event stream for Slack-tooling blocker recovery. `reads` lists successful reads;
# `failed_reads` lists attempts that must not count as proof that worker Slack access recovered.
ndjson_path = "stub.ndjson"
reads = spec.get("reads")
if reads is not None:
    lines = []
    for i, ch in enumerate(reads):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"ok{i}", "name": "mcp__slack__slack_read_thread",
             "input": {"channel_id": ch}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"ok{i}",
             "content": [{"type": "text", "text": "Alice: a message"}]}]}}))
    for i, ch in enumerate(spec.get("failed_reads") or []):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"bad{i}", "name": "mcp__slack__slack_read_thread",
             "input": {"channel_id": ch}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"bad{i}", "is_error": True,
             "content": "ratelimited"}]}}))
    ndjson_path = os.path.join(os.environ.get("REPO_ROOT", "."), "logs", "stub-worker.ndjson")
    os.makedirs(os.path.dirname(ndjson_path), exist_ok=True)
    # chr(10) rather than a newline escape: this file is a Python literal that
    # GENERATES Python, so "\\n" here is interpreted one level too early and puts a
    # real newline inside the generated source, which then fails to parse.
    with open(ndjson_path, "w") as f:
        for line in lines:
            f.write(line + chr(10))

print(json.dumps({"exit": spec.get("exit", 0), "wall_sec": spec.get("wall_sec", 1),
                  "log": "stub.log", "ndjson": ndjson_path,
                  "timed_out": bool(spec.get("timed_out", False))}))
sys.exit(spec.get("exit", 0))
'''

MCP_STUB = '''#!/usr/bin/env python3
"""Scripted Slack bridge. Covers the health gate and the reactions sweep.

`slack.healthy: false` in a scenario makes the health gate fail exactly as a real
outage does (non-zero exit), which is how we test that Slack-dependent targets are
gated while Jira/email/schedule targets still dispatch.
"""
import json, os, sys

sc = json.load(open(os.environ["YAAS_SCENARIO"]))
slack = sc.get("slack", {})
if not slack.get("healthy", True):
    sys.stderr.write("scenario: slack down\\n")
    sys.exit(1)
print(json.dumps(slack.get("response", {"ok": True, "text": ""})))
'''

REACTIONS_STUB = '''#!/usr/bin/env python3
"""Canned reactions sweep. Writes pending_reactions.json from the scenario.

reactions.py is still on the legacy pipe contract, so this mirrors that shape rather
than result.py's. When it moves onto the contract, this stub changes with it.
"""
import json, os, sys

pending_path = sys.argv[4] if len(sys.argv) > 4 else None
sc = json.load(open(os.environ["YAAS_SCENARIO"]))
pending = sc.get("reactions", {})
if pending and pending_path:
    os.makedirs(os.path.dirname(pending_path), exist_ok=True)
    with open(pending_path, "w") as f:
        json.dump(pending, f)
n = sum(len(v) for v in pending.values())
print(f"{'dirty' if n else 'clean'}|{n}|{n} pending reaction(s)")
'''

NOTIFY_STUB = '''#!/usr/bin/env python3
"""No-op notifier. A test must never fire a real desktop notification."""
import sys
sys.exit(0)
'''


def build(scenario_path, dest):
    sc = json.loads(Path(scenario_path).read_text())
    # A single clock for the whole scenario, so every relative time in it is consistent.
    now = int(sc.get("now_epoch") or time.time())
    sc = resolve_times(sc, now)
    sc["now_epoch"] = now

    # Rewrite friendly aliases to conforming ids, keeping the map so the stubs can look
    # verdicts up by alias and the snapshot can report by alias.
    aliases = {}
    for q in sc.get("quests", {}).values():
        for w in q.get("watches", []):
            alias = w.get("watch_id")
            if alias and not alias.startswith("watch-"):
                w["watch_id"] = watch_id_for(alias)
                aliases[w["watch_id"]] = alias
    sc["_aliases"] = aliases
    dest = Path(dest).resolve()
    if dest.exists():
        shutil.rmtree(dest)

    # ── The real code under test ────────────────────────────────────────────────
    # Copy rather than symlink: the stubs overwrite four files, and a symlinked tree
    # would write those through into the actual repo.
    tri = dest / "yaas-triage"
    tri.mkdir(parents=True)
    for item in TRIAGE_DIR.iterdir():
        if item.name in ("tests", "skills", "__pycache__"):
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, tri / item.name)

    # ── Stubs over every external seam ──────────────────────────────────────────
    for watch_type in ("slack_thread", "slack_channel", "slack_dm", "slack_mention",
                       "email", "jira", "github_pr", "schedule", "approval"):
        _write(tri / "checkers" / f"{watch_type}.py", CHECKER_STUB, executable=True)
    _write(tri / "checkers" / "reactions.py", REACTIONS_STUB, executable=True)
    _write(tri / "dispatch" / "run-agent.py", RUN_AGENT_STUB, executable=True)
    _write(tri / "surfaces" / "mcp-call.sh", MCP_STUB, executable=True)
    _write(tri / "ops" / "notify.py", NOTIFY_STUB, executable=True)

    # ── State tree ──────────────────────────────────────────────────────────────
    (dest / "state" / "triage").mkdir(parents=True)
    (dest / "logs").mkdir(parents=True)
    active = dest / "state" / "quests" / "active"
    active.mkdir(parents=True)
    for qid, q in sc.get("quests", {}).items():
        qd = active / qid
        qd.mkdir(parents=True)
        meta = {"id": qid, "title": q.get("title", qid), "status": "active",
                "priority": "medium", "allow_send": False}
        meta.update(q.get("meta", {}))
        _write(qd / "meta.json", json.dumps(meta, indent=2) + "\n")
        _write(qd / "watch.json", json.dumps({"watches": q.get("watches", [])}, indent=2) + "\n")
        _write(qd / "context.md", q.get("context", f"# {qid}\n"))
        _write(qd / "timeline.ndjson", "".join(
            json.dumps(e) + "\n" for e in q.get("timeline", [])))

    for name, content in (sc.get("state_files") or {}).items():
        _write(dest / "state" / name, json.dumps(content, indent=2) + "\n")

    # A real CLAUDE.md is not needed (the agent is stubbed) but its absence is a
    # documented failure elsewhere, so keep the tree honest.
    _write(dest / "CLAUDE.md", "# Quest Activation Protocol\n")

    env_lines = ["YAAS_AGENT=stub\n"]
    for k, v in (sc.get("env") or {}).items():
        env_lines.append(f"{k}={v}\n")
    _write(dest / ".env", "".join(env_lines))

    _write(dest / "scenario.json", json.dumps(sc, indent=2) + "\n")
    return dest


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "build":
        print("usage: scenario.py build <scenario.json> <dest_dir>", file=sys.stderr)
        return 2
    print(build(sys.argv[2], sys.argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
