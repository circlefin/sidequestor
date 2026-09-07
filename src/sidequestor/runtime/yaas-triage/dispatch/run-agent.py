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

This is the shared pipeline for the triage orchestrator: launch dispatch-agent.sh,
tee the raw event stream to an ndjson file, pipe
it through format-stream.py into a human transcript, symlink worker-latest.*, and kill
the whole process tree if it runs past its timeout. Two copies meant a fix to the
watchdog or the log pipeline had to be made twice, and half of it had no test.

What it does NOT do is decide anything. The ack manifest, the Slack infra guard, the
Slack-read recovery check, token extraction and the commit all stay with the caller, because
those are policy and this is plumbing.

Usage:
  run-agent.py --prompt <text> --label <slug> [--timeout 1800] [--log-dir DIR]

Prints one JSON line to stdout describing the run:
  {"exit": 0, "wall_sec": 42, "log": "...", "ndjson": "...", "timed_out": false}

Exit code is the AGENT's exit code, with 124 for a watchdog kill (matching timeout(1)
convention, which triage already treats specially: acks written before the kill are
still trustworthy, so they still commit).

Env passed through to dispatch-agent.sh: YAAS_AGENT, YAAS_CLAUDE_*, YAAS_CODEX_*, YAAS_CURSOR_*,
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
    a subdirectory, producing a parallel state/ tree nothing reads. NOT keyed on a worker
    instruction file and NOT on .git (two git dirs here, none in
    fixtures). Ambient $REPO_ROOT is deliberately ignored: a stale value pointing at another
    checkout would pass any marker check and silently redirect writes. Test fixtures copy
    the whole tree, so the walk-up finds the fixture on its own.

    Kept byte-identical across every file that needs it; tests/behaviour/repo-root.test.sh
    asserts that, because a shared module would need sys.path handling whose own path is
    depth-dependent, which is the bug being fixed.
    """
    override = (os.environ.get("SIDEQUESTOR_WORKSPACE")
                or os.environ.get("YAAS_WORKSPACE"))
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _repo_root(__file__)
DEFAULT_TIMEOUT = 1800
WORKER_STATE_FILE = REPO_ROOT / "state" / "triage" / "worker-current.json"
DEFAULT_HEARTBEAT_SECONDS = 15


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json_atomic(path, value):
    """Publish one complete lifecycle observation or leave the old one intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(value, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"worker lifecycle write failed: {exc}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


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


def kill_agent_tree(pid, sig=signal.SIGTERM):
    """Kill the agent's isolated process group, with a recursive fallback."""
    try:
        # start_new_session=True makes the agent PID its process-group ID. Signalling
        # the group closes the race where a shell exits while its child is being walked.
        os.killpg(pid, sig)
    except (AttributeError, ProcessLookupError, PermissionError):
        kill_tree(pid, sig)


def run(prompt, label, timeout=DEFAULT_TIMEOUT, log_dir=None, header=None):
    log_dir = Path(log_dir or REPO_ROOT / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = slugify(label)
    human = log_dir / f"worker-{stamp}-{slug}.log"
    ndjson = log_dir / f"worker-{stamp}-{slug}.ndjson"

    # Keep the operator-facing aliases on the invocation in flight. The dashboard uses
    # the lifecycle record below, but pre-upgrade readers still follow these symlinks.
    for link, target in ((log_dir / "worker-latest.log", human),
                         (log_dir / "worker-latest.ndjson", ndjson)):
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target.name)
        except OSError:
            pass

    started_at = utc_now()
    with open(human, "w") as f:
        f.write(f"=== Worker dispatch {started_at} ===\n")
        for line in (header or []):
            f.write(line + "\n")
        f.write("=" * 56 + "\n")

    env = dict(os.environ, REPO_ROOT=str(REPO_ROOT))
    # Sanctioned write surfaces use this to bind side effects to the one target
    # named by the dispatch. The model cannot bypass quest policy by omitting or
    # substituting a quest_id in a helper payload.
    env["SIDEQUESTOR_DISPATCH_TARGET"] = label
    # When YAAS_CLAUDE_DEBUG is set, give the worker a per-invocation debug file beside its
    # logs so dispatch-agent.sh routes claude --debug-file there (root-cause view of a stall:
    # API request/retry/timing, MCP traffic). Harmless when debug is off — the var is only
    # consumed if YAAS_CLAUDE_DEBUG is also set.
    debug = log_dir / f"worker-{stamp}-{slug}.debug"
    env["YAAS_WORKER_DEBUG_FILE"] = str(debug)
    started = time.time()
    # last_line is the wall-clock of the most recent stream line. The stall monitor
    # (below) compares against it; the read loop stamps it on every line.
    state = {"timed_out": False, "stalled": False, "last_line": started}

    lifecycle_lock = threading.Lock()
    lifecycle = {
        "schema": 1,
        "run_ref": f"{stamp}-{slug}",
        "state": "running",
        "targets": [label],
        "agent": os.environ.get("YAAS_AGENT", "codex"),
        "supervisor_pid": os.getpid(),
        "started_at": started_at,
        "heartbeat_at": started_at,
        "ended_at": None,
        "exit": None,
        "timed_out": False,
        "stalled": False,
        "log": human.name,
        "ndjson": ndjson.name,
    }

    def publish_lifecycle(**changes):
        with lifecycle_lock:
            lifecycle.update(changes)
            write_json_atomic(WORKER_STATE_FILE, lifecycle)

    # This record, not the transcript format, is the dashboard's lifecycle truth.
    publish_lifecycle()

    # Inactivity watchdog threshold. A model/transport round-trip can hang after emitting
    # a tool_use with no further stream output, in which case the only thing that fires is
    # the outer `timeout` watchdog, so a worker silent after one tool_use burns the whole
    # slot up to the 1800s ceiling. This catches that gap sooner AND timestamps it. Must
    # exceed the longest LEGITIMATE silence between stream lines, which is a single tool
    # call's runtime — the Bash tool's own cap
    # is 600s — so the default (900s) sits safely above that and well under the 1800s ceiling.
    # 0 disables it (falls back to the outer watchdog only).
    try:
        stall_seconds = int(os.environ.get("YAAS_WORKER_STALL_SECONDS", "900") or "900")
    except ValueError:
        stall_seconds = 900

    try:
        agent = subprocess.Popen(
            ["bash", str(SCRIPT_DIR / "dispatch-agent.sh"), prompt],
            stdout=subprocess.PIPE,
            stderr=open(str(ndjson) + ".err", "w"),
            env=env, start_new_session=True)
    except Exception:
        publish_lifecycle(state="exited", heartbeat_at=utc_now(), ended_at=utc_now(), exit=1)
        raise

    try:
        heartbeat_seconds = max(
            0.1, float(os.environ.get("YAAS_WORKER_HEARTBEAT_SECONDS",
                                      DEFAULT_HEARTBEAT_SECONDS)))
    except (TypeError, ValueError):
        heartbeat_seconds = DEFAULT_HEARTBEAT_SECONDS
    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        while not heartbeat_stop.wait(heartbeat_seconds):
            publish_lifecycle(heartbeat_at=utc_now())

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()

    fmt = subprocess.Popen(
        ["python3", str(SCRIPT_DIR / "format-stream.py")],
        stdin=subprocess.PIPE, stdout=open(human, "a"),
        stderr=subprocess.DEVNULL, env=env)

    def _mark(msg):
        """Append a timestamped diagnostic line to the human-readable worker log."""
        try:
            with open(human, "a") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg} ===\n")
        except Exception:
            pass

    # A real timer, not a clock check inside the read loop. An agent that emits one
    # line and then hangs leaves the loop blocked in readline forever, so the check
    # never runs — which is exactly what the first version of this did. The shell
    # version used a background subshell with `sleep` for the same reason.
    def on_timeout():
        state["timed_out"] = True
        _mark(f"WORKER TIMEOUT — no completion within {timeout}s ceiling; killing tree")
        kill_agent_tree(agent.pid, signal.SIGTERM)
        time.sleep(3)
        kill_agent_tree(agent.pid, signal.SIGKILL)

    watchdog = threading.Timer(timeout, on_timeout)
    watchdog.daemon = True
    watchdog.start()

    # Inactivity monitor: a light polling thread (not a per-line Timer, which would churn
    # once per streamed token). It wakes periodically and kills the tree if no stream line
    # has arrived for stall_seconds. Reuses the timed_out flag so triage sees the same 124
    # recovery path, but logs a distinct STALL marker so the cause is unambiguous in the log.
    stop_event = threading.Event()

    def stall_monitor():
        if stall_seconds <= 0:
            return
        # Poll frequently enough to fire close to the threshold without busy-waiting.
        tick = max(5, min(30, stall_seconds // 4))
        while not stop_event.wait(tick):
            idle = time.time() - state["last_line"]
            if idle >= stall_seconds:
                state["stalled"] = True
                state["timed_out"] = True
                _mark(f"WORKER STALL — no stream output for {int(idle)}s "
                      f"(threshold {stall_seconds}s); killing tree for fast recovery")
                kill_agent_tree(agent.pid, signal.SIGTERM)
                time.sleep(3)
                kill_agent_tree(agent.pid, signal.SIGKILL)
                return

    monitor = threading.Thread(target=stall_monitor, daemon=True)
    monitor.start()

    # tee: every line goes to the raw ndjson AND to the formatter, streaming, so the
    # live panel updates while the agent is still working.
    try:
        with open(ndjson, "w") as raw:
            for line in agent.stdout:
                state["last_line"] = time.time()
                raw.write(line.decode("utf-8", "replace"))
                raw.flush()
                try:
                    fmt.stdin.write(line)
                    fmt.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
    except Exception:
        pass

    stop_event.set()

    try:
        fmt.stdin.close()
    except Exception:
        pass

    try:
        agent.wait(timeout=max(1, timeout - (time.time() - started)) + 10)
    except subprocess.TimeoutExpired:
        state["timed_out"] = True
        kill_agent_tree(agent.pid, signal.SIGKILL)
    watchdog.cancel()
    stop_event.set()

    try:
        fmt.wait(timeout=10)
    except Exception:
        kill_tree(fmt.pid, signal.SIGKILL)

    # 124 matches timeout(1). triage treats it specially: an ack is written only AFTER
    # its item's work completed, so acks banked before the kill still commit. A stall kill
    # uses the same code/path — the STALL marker in the human log is what distinguishes it.
    code = 124 if state["timed_out"] else (agent.returncode if agent.returncode is not None else 1)
    heartbeat_stop.set()
    heartbeat.join(timeout=max(1, heartbeat_seconds + 1))
    ended_at = utc_now()
    publish_lifecycle(state="exited", heartbeat_at=ended_at, ended_at=ended_at,
                      exit=code, timed_out=state["timed_out"], stalled=state["stalled"])
    return {"exit": code, "wall_sec": int(time.time() - started),
            "log": str(human), "ndjson": str(ndjson),
            "timed_out": state["timed_out"], "stalled": state["stalled"]}


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
