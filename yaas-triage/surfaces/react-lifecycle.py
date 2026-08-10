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
react-lifecycle.py — move a reaction through its visible lifecycle, atomically and logged.

A reaction the agent acts on carries a three-state lifecycle on the Slack message:
    trigger (claude-intensifies | writing_hand | incoming_envelope)
      → claudeloading   (picked up)
      → updatedone      (finished)
Only one lifecycle emoji should ever be on the message at once.

Until now this was hand-composed prose: CLAUDE.md told the worker to remove one emoji and add
another at three separate points, via slack-react.sh. Nothing verified it, nothing logged it,
and it was decoupled from the state file that records "acted" — so the worker could mark a
reaction replied while the emoji stayed stuck at the trigger, and no one could see the drift.
That is the same failure class the stale-reply guard fixed: a rule the model can forget is not
a guard.

This makes the transition ONE verb. `advance <channel> <ts> loading|done` removes every OTHER
lifecycle emoji and adds the target, so the message can never show two at once, and it prints
a lifecycle log line so drift is finally visible. It is idempotent: re-advancing to the same
state removes nothing it should keep and re-adds harmlessly (Slack's reactions.add on an
existing reaction is a no-op the client treats as success).

A remove that fails because the emoji was not there is NOT an error — that is the common case
(the trigger may already be gone, or a previous partial run left the target on). Only a
transport/auth failure on the ADD is fatal, because that is the emoji that must end up present.

Usage:
    react-lifecycle.py advance <channel_id> <message_ts> <loading|done> [--react BIN] [--log FILE]
Exit: 0 the target emoji is present; 1 the add failed (caller should not mark done).
"""

import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

TRIGGERS = ("claude-intensifies", "writing_hand", "incoming_envelope")
LOADING = "claudeloading"
DONE = "updatedone"

# The full set of lifecycle emojis, so "advance to X" can remove every other one regardless of
# which state the message was actually in. floppy_disk is NOT here: it has no lifecycle.
ALL_LIFECYCLE = (*TRIGGERS, LOADING, DONE)

TARGETS = {"loading": LOADING, "done": DONE}


def _react(react_bin, verb, channel, ts, emoji):
    """Run slack-react.sh <add|remove>. Returns True on success (exit 0)."""
    try:
        r = subprocess.run([react_bin, verb, channel, ts, emoji],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def advance(channel, ts, state, react_bin, log=None):
    """Move the message to `state` (LOADING or DONE): remove every other lifecycle emoji, add
    the target. Returns True iff the target emoji is present afterwards.

    Removals are best-effort: a missing emoji is the normal case, not a failure. The add is
    what matters — if it fails (auth/transport), the caller must NOT treat the transition as
    complete, so this returns False and the reaction stays at whatever it was.
    """
    target = TARGETS[state]

    # Remove every lifecycle emoji that is not the target. Order does not matter; a failed
    # remove (already absent) is fine.
    removed = []
    for e in ALL_LIFECYCLE:
        if e == target:
            continue
        if _react(react_bin, "remove", channel, ts, e):
            removed.append(e)

    added = _react(react_bin, "add", channel, ts, target)

    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
            f"REACTION LIFECYCLE {channel}/{ts} -> :{target}: "
            f"({'added' if added else 'ADD FAILED'}; removed {removed or 'none'})")
    if log:
        try:
            with open(log, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line)
    return added


def main():
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != "advance" or args[3] not in TARGETS:
        print("usage: react-lifecycle.py advance <channel_id> <message_ts> <loading|done>",
              file=sys.stderr)
        return 2
    channel, ts, state = args[1], args[2], args[3]

    react_bin = str(SCRIPT_DIR / "slack-react.sh")
    if "--react" in args:
        react_bin = args[args.index("--react") + 1]
    log = None
    if "--log" in args:
        log = args[args.index("--log") + 1]

    return 0 if advance(channel, ts, state, react_bin, log) else 1


if __name__ == "__main__":
    sys.exit(main())
