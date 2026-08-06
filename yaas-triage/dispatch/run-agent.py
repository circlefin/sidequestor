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
run-agent.py — run ONE headless agent invocation and return its exit code.

This is the pipeline that triage.sh and manual-dispatch.sh each used to implement
separately: launch dispatch-agent.sh, tee the raw event stream to an ndjson file, pipe
it through format-stream.py into a human transcript, symlink worker-latest.*, and kill
the whole process tree if it runs past its timeout. Two copies meant a fix to the
watchdog or the log pipeline had to be made twice, and half of it had no test.

What it does NOT do is decide anything. The ack manifest, the Slack infra guard, the
source-evidence check, token extraction and the commit all stay with the caller, because
those are policy and this is plumbing.

Usage:
  run-agent.py --prompt <text> --label <slug> [--timeout 1800] [--log-dir DIR]

Prints one JSON line to stdout describing the run:
  {"exit": 0, "wall_sec": 42, "log": "...", "ndjson": "...", "timed_out": false}

Exit code is the AGENT's exit code, with 124 for a watchdog kill (matching timeout(1)
convention, which triage already treats specially: acks written before the kill are
still trustworthy, so they still commit).

Env passed through to dispatch-agent.sh: YAAS_AGENT, YAAS_CLAUDE_*, YAAS_CODEX_*,
REPO_ROOT.
"""

import json
import os
import signal
import subprocess
import threading
import sys
import time
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


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _repo_root(__file__)
DEFAULT_TIMEOUT = 1800


def slugify(label):
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in label) or "run"


def kill_tree(pid, sig=signal.SIGTERM):
    """Kill a process and its descendants.

    The agent spawns children that hold the pipe open; killing only the parent leaves
    the pipeline unable to close and the wait never returns. The shell version walked
    the tree with pgrep for exactly this reason.
    """
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
        for child in out.stdout.split():
            kill_tree(int(child), sig)
    except Exception:
        pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(prompt, label, timeout=DEFAULT_TIMEOUT, log_dir=None, header=None):
    log_dir = Path(log_dir or REPO_ROOT / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = slugify(label)
    human = log_dir / f"worker-{stamp}-{slug}.log"
    ndjson = log_dir / f"worker-{stamp}-{slug}.ndjson"

    # worker-latest.* points at the invocation in flight; the dashboard's live panel
    # follows these, so they must be updated before the agent starts, not after.
    for link, target in ((log_dir / "worker-latest.log", human),
                         (log_dir / "worker-latest.ndjson", ndjson)):
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target.name)
        except OSError:
            pass

    with open(human, "w") as f:
        f.write(f"=== Worker dispatch {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")
        for line in (header or []):
            f.write(line + "\n")
        f.write("=" * 56 + "\n")

    env = dict(os.environ, REPO_ROOT=str(REPO_ROOT))
    started = time.time()
    state = {"timed_out": False}

    agent = subprocess.Popen(
        ["bash", str(SCRIPT_DIR / "dispatch-agent.sh"), prompt],
        stdout=subprocess.PIPE,
        stderr=open(str(ndjson) + ".err", "w"),
        env=env, start_new_session=True)

    fmt = subprocess.Popen(
        ["python3", str(SCRIPT_DIR / "format-stream.py")],
        stdin=subprocess.PIPE, stdout=open(human, "a"),
        stderr=subprocess.DEVNULL, env=env)

    # A real timer, not a clock check inside the read loop. An agent that emits one
    # line and then hangs leaves the loop blocked in readline forever, so the check
    # never runs — which is exactly what the first version of this did. The shell
    # version used a background subshell with `sleep` for the same reason.
    def on_timeout():
        state["timed_out"] = True
        kill_tree(agent.pid, signal.SIGTERM)
        time.sleep(3)
        kill_tree(agent.pid, signal.SIGKILL)

    watchdog = threading.Timer(timeout, on_timeout)
    watchdog.daemon = True
    watchdog.start()

    # tee: every line goes to the raw ndjson AND to the formatter, streaming, so the
    # live panel updates while the agent is still working.
    try:
        with open(ndjson, "w") as raw:
            for line in agent.stdout:
                raw.write(line.decode("utf-8", "replace"))
                raw.flush()
                try:
                    fmt.stdin.write(line)
                    fmt.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
    except Exception:
        pass

    try:
        fmt.stdin.close()
    except Exception:
        pass

    try:
        agent.wait(timeout=max(1, timeout - (time.time() - started)) + 10)
    except subprocess.TimeoutExpired:
        state["timed_out"] = True
        kill_tree(agent.pid, signal.SIGKILL)
    watchdog.cancel()

    try:
        fmt.wait(timeout=10)
    except Exception:
        kill_tree(fmt.pid, signal.SIGKILL)

    # 124 matches timeout(1). triage treats it specially: an ack is written only AFTER
    # its item's work completed, so acks banked before the kill still commit.
    code = 124 if state["timed_out"] else (agent.returncode if agent.returncode is not None else 1)
    return {"exit": code, "wall_sec": int(time.time() - started),
            "log": str(human), "ndjson": str(ndjson), "timed_out": state["timed_out"]}


def main():
    args = sys.argv[1:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    prompt = opt("--prompt")
    label = opt("--label")
    if not prompt or not label:
        print("usage: run-agent.py --prompt <text> --label <slug> [--timeout N] [--log-dir DIR]",
              file=sys.stderr)
        return 3
    try:
        timeout = int(opt("--timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    header = []
    for i, a in enumerate(args):
        if a == "--header" and i + 1 < len(args):
            header.append(args[i + 1])

    result = run(prompt, label, timeout, opt("--log-dir"), header)
    print(json.dumps(result))
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
