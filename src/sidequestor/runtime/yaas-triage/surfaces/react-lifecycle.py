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
react-lifecycle.py — reconcile a message's visible lifecycle, with retryable failures.

A reaction the agent acts on carries a configurable three-state lifecycle on the Slack message:
    trigger (process | draft | adopt)
      → loading   (picked up)
      → done      (finished)
Only one lifecycle emoji should ever be on the message at once.

Until now this was hand-composed prose: CLAUDE.md told the worker to remove one emoji and add
another at three separate points, via slack-react.sh. Nothing verified it, nothing logged it,
and it was decoupled from the state file that records "acted" — so the worker could mark a
reaction replied while the emoji stayed stuck at the trigger, and no one could see the drift.
That is the same failure class the stale-reply guard fixed: a rule the model can forget is not
a guard.

Slack cannot swap reactions atomically. Add the target first, then remove the others.
Both adding and cleanup must succeed before reporting success. The runtime outbox
retries failed transitions without repeating business actions. An absent reaction
is normalized to success by the Slack client.

Usage:
    react-lifecycle.py advance <channel_id> <message_ts> <loading|done|blocked> [--react BIN] [--log FILE]
Exit: 0 reconciliation succeeded; 1 add or cleanup failed.
"""

import subprocess
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from reaction_config import load_reaction_emojis


def _react(react_bin, verb, channel, ts, emoji):
    """Run slack-react.sh <add|remove>. Returns True on success (exit 0)."""
    try:
        r = subprocess.run([react_bin, verb, channel, ts, emoji],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def advance(channel, ts, state, react_bin, log=None, env=None):
    """Add the target, then remove other lifecycle emojis; report complete cleanup."""
    emojis = load_reaction_emojis(env)
    target = emojis[state]
    lifecycle = (emojis["process"], emojis["draft"], emojis["adopt"],
                 emojis["loading"], emojis["done"])

    # Slack has no atomic swap. Add first so a failed add cannot erase the only
    # visible status. A missing reaction returns success in client.py; any other
    # removal failure needs reconciliation, not a false success.
    added = _react(react_bin, "add", channel, ts, target)
    removed = []
    failed = []
    if added:
        for e in (*lifecycle, emojis["blocked"]):
            if e == target:
                continue
            if _react(react_bin, "remove", channel, ts, e):
                removed.append(e)
            else:
                failed.append(e)
    complete = added and not failed

    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
            f"REACTION LIFECYCLE {channel}/{ts} -> :{target}: "
            f"({'added' if added else 'ADD FAILED'}; removed {removed or 'none'}; failed {failed or 'none'})")
    if log:
        try:
            with open(log, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line)
    root = os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")
    if root and os.environ.get("SIDEQUESTOR_DISPATCH_TARGET") == "reactions":
        from inbox import try_log_inbox
        try_log_inbox(root, f"reaction-lifecycle:{channel}:{ts}:{state}:{added}", {
            "event": "reaction_started" if state == "loading" else "reaction_finished",
            "channel_id": channel, "thread_ts": ts, "message_ts": ts,
            "note": f"Reaction {state}: {'confirmed' if complete else 'failed'}", "succeeded": complete,
        })
    return complete


def main():
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != "advance" or args[3] not in ("loading", "done", "blocked"):
        print("usage: react-lifecycle.py advance <channel_id> <message_ts> <loading|done|blocked>",
              file=sys.stderr)
        return 2
    channel, ts, state = args[1], args[2], args[3]

    react_bin = str(SCRIPT_DIR / "slack-react.sh")
    if "--react" in args:
        react_bin = args[args.index("--react") + 1]
    log = None
    if "--log" in args:
        log = args[args.index("--log") + 1]

    try:
        return 0 if advance(channel, ts, state, react_bin, log) else 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
