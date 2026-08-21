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
log-event.py — append one entry to a quest's timeline with a real timestamp.

Why this exists
===============
A timeline entry records something that already happened, so its `ts` must come
from a clock. An LLM worker has no clock. Its only sense of time is the date
line injected into its context, which carries a LOCAL date and no time of day,
so asking it to fill in a `ts` yields things like "2026-08-15T00:00:00Z" —
midnight of the local date, labelled UTC. That reads as up to a day off, and
when the local date is ahead of UTC it lands in the FUTURE, which sorts a log
entry above everything real and pins it to the top of the dashboard.

Slack sends never had this problem because they go through slack-send.py, which
stamps the time itself. This helper closes the same hole for every other event
type. The worker supplies what happened; the clock is not its job.

`ts` is always stamped here. A caller-supplied `ts` is ignored, not honoured and
not rejected: refusing the write would tempt a worker into skipping the log
entirely, which is worse than a corrected timestamp.

Usage
=====
    python3 yaas-triage/surfaces/log-event.py '<json>'
    python3 yaas-triage/surfaces/log-event.py --quest-id <id> --event note --note "..."

<json> fields:
    quest_id      (required)  quest to log under
    event         (required)  prefer one of: message_sent, draft_posted,
                              executed, info_received, status_change, note,
                              blocked. A quest-specific name is accepted with a
                              warning, since real timelines carry many.
    note          (optional)  short human summary
    message_text  (optional)  verbatim body, when the event carries one
    channel_id    (optional)  Slack channel/DM the event relates to
    thread_ts     (optional)  parent ts, when thread-scoped
    link_url      (optional)  permalink to the source
    reason        (optional)  why, for `blocked`

    Any other key is passed through to the entry unchanged, so callers can
    record event-specific detail without a change here. `ts` is the sole
    exception: it is always replaced.

Output (stdout): compact JSON, e.g.
    {"ok":true,"quest_id":"quest-...","event":"note","ts":"2026-08-14T20:41:07Z"}

Exit codes:
    0  appended
    1  bad arguments
    2  quest not found
"""

from __future__ import annotations  # PEP 604 unions below must not be
# evaluated at def time: this file has to import on Python < 3.10.
import argparse
import json
import os
import sys
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
    override = os.environ.get("YAAS_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT = _repo_root(__file__)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from timeline_io import utc_now, quest_dir, append_timeline

# The documented vocabulary, and the only names anything switches on: the
# dashboard classifies these as agent/external/system, and the ack ledger and
# checkers read them.
#
# Advisory, NOT a whitelist. Real timelines carry ~27 distinct event names,
# most of them quest-specific (brief_written, weekly_recap_posted, pr_merged,
# tests_run). Rejecting those would push a worker straight back to hand-writing
# the line, which is the bug this helper exists to remove: a wrong timestamp is
# far more damaging than an unfamiliar event name. So an unknown name is
# written, and merely warned about on stderr.
KNOWN_EVENTS = (
    "message_sent",
    "draft_posted",
    "executed",
    "info_received",
    "status_change",
    "note",
    "blocked",
)

def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _payload_from_argv(argv) -> dict:
    """Accept either one JSON blob or the flag form, matching slack-send.py."""
    if argv and not argv[0].startswith("-"):
        if len(argv) > 1:
            die("pass a single JSON object, or use the --flag form")
        try:
            p = json.loads(argv[0])
        except json.JSONDecodeError as e:
            die(f"payload is not valid JSON: {e}")
        if not isinstance(p, dict):
            die("payload must be a JSON object")
        return p

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--quest-id")
    ap.add_argument("--event")
    ap.add_argument("--note")
    ap.add_argument("--message-text")
    ap.add_argument("--channel-id")
    ap.add_argument("--thread-ts")
    ap.add_argument("--link-url")
    ap.add_argument("--reason")
    args = ap.parse_args(argv)
    return {k: v for k, v in vars(args).items() if v is not None}


def main(argv) -> int:
    p = _payload_from_argv(argv)

    quest_id = str(p.get("quest_id") or "").strip()
    event = str(p.get("event") or "").strip()
    if not quest_id:
        die("quest_id is required")
    if not event:
        die("event is required")
    if event not in KNOWN_EVENTS:
        print(f"warning: '{event}' is outside the documented vocabulary "
              f"({', '.join(KNOWN_EVENTS)}); writing it anyway",
              file=sys.stderr)
    # A quest id is a directory name. Reject traversal before touching the path.
    if "/" in quest_id or quest_id in ("", ".", "..") or any(ord(ch) < 32 or ord(ch) == 127 for ch in quest_id):
        die(f"invalid quest id '{quest_id}'")

    qdir = quest_dir(REPO_ROOT, quest_id)
    if qdir is None:
        die(f"quest '{quest_id}' not found", 2)

    supplied_ts = p.pop("ts", None)
    entry = {"ts": utc_now(), "event": event}
    for key, value in p.items():
        if key in ("quest_id", "event") or value in (None, ""):
            continue
        entry[key] = value

    append_timeline(qdir, entry)

    out = {"ok": True, "quest_id": quest_id, "event": event, "ts": entry["ts"]}
    if supplied_ts is not None:
        # Surfaced, not silent: a caller that passed a time should see that the
        # clock overruled it rather than believe its own value was written.
        out["ts_overridden"] = supplied_ts
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
