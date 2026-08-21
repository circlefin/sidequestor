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
notify.py — desktop notifications for new YAAS activity since the last run.

Previously notify.sh: 184 lines, of which 143 were this program inside a heredoc. The
shell resolved two paths and did nothing else.

On first run the watermark is initialised to now and nothing fires, so installing this
does not replay history at you. Every run advances the watermark even when delivery
fails, because the alternative is re-notifying the same events forever.

Env, all optional; the defaults are production behaviour:
  YAAS_NOTIFY_REPO_ROOT   point at a fixture tree instead of the real repo
  YAAS_NOTIFY_WATERMARK   point at a throwaway watermark file
  YAAS_NOTIFY_CMD         delivery hook, invoked as <cmd> <title> <subtitle> <body>.
                          Unset in production, where delivery is osascript.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reaction_config import load_reaction_emojis

MAX_PER_RUN = 10          # a banner pile-up is worse than a missed banner

TIMELINE_EVENTS = {
    "message_sent": "message sent",
    "draft_posted": "draft created — review needed",
    "executed":     "action executed",
}

# The "quietly stopped working" signals. Each of these previously reached exactly one
# place: a line in logs/triage.log that nobody reads.
RUNLOG_EVENTS = {
    "gate_watch_misconfigured": (
        "watch misconfigured — needs a human",
        lambda e: f"{e.get('quest', '?')} [{e.get('type', '?')}] {e.get('reason', '')}"),
    "gate_budget_exceeded": (
        "BUDGET CAP HIT — dispatch withheld",
        lambda e: e.get("reason", "")),
    "gate_watch_backlog": (
        "saturated window — cursor held",
        lambda e: f"{e.get('quest', '?')}: {e.get('watches', '?')} watch(es) had more than one page"),
    "gate_target_breaker_open": (
        "target breaker open — dispatch stopped",
        lambda e: f"{e.get('target', '?')} ran {e.get('dispatches_1h', '?')}x in an hour"),
    "gate_ack_manifest_unreadable": (
        "ack manifest unreadable — work held",
        lambda e: f"{e.get('quest', '?')} run {e.get('run_id', '?')}"),
}

def parse_ts(raw):
    """ISO-8601 to epoch float. None if unparseable."""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def iter_ndjson(path):
    try:
        text = Path(path).read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            yield entry


def from_timelines(repo, watermark):
    """One notification per outbound action a quest recorded."""
    for timeline in (repo / "state" / "quests" / "active").glob("*/timeline.ndjson"):
        meta = read_json(timeline.parent / "meta.json", {}) or {}
        title = meta.get("title", timeline.parent.name)
        for entry in iter_ndjson(timeline):
            label = TIMELINE_EVENTS.get(entry.get("event", ""))
            ts = parse_ts(entry.get("ts"))
            if label and ts and ts > watermark:
                yield ts, f"YAAS — {label}", title, (entry.get("note") or "")[:60]


def from_runlog(repo, watermark):
    """Health events, collapsed to one per KIND per run.

    Without the collapse a persistent misconfiguration fires every tick forever, and a
    notification stream you learn to ignore is worse than none. Across runs the
    watermark handles dedup; this handles within a single run.
    """
    seen = {}
    for entry in iter_ndjson(repo / "state" / "run-log.ndjson"):
        kind = entry.get("event", "")
        if kind not in RUNLOG_EVENTS:
            continue
        ts = parse_ts(entry.get("ts"))
        if not ts or ts <= watermark:
            continue
        label, detail = RUNLOG_EVENTS[kind]
        try:
            body = str(detail(entry))[:110]
        except Exception:
            body = ""
        seen[kind] = (ts, f"YAAS — {label}", "triage health", body)
    return list(seen.values())


def from_reactions(repo, watermark):
    emojis = load_reaction_emojis()
    reaction_files = {
        "claude_intensifies_replied.json":
            ("replied_timestamps", f"replied to :{emojis['process']}"),
        "writing_hand_replied.json":
            ("replied_timestamps", f"drafted reply for :{emojis['draft']}"),
    }
    for fname, (key, label) in reaction_files.items():
        data = read_json(repo / "state" / fname)
        if not isinstance(data, dict):
            continue
        for raw in data.get(key) or []:
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                continue
            if ts > watermark:
                yield ts, f"YAAS — {label}", "", ""


def deliver(title, subtitle, body, cmd):
    """Send one. Guarded: a single delivery failure must not abort the run, because
    that would skip the watermark advance and re-notify everything next tick."""
    try:
        if cmd:
            subprocess.run([cmd, title, subtitle, body], capture_output=True, timeout=5)
        else:
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            if subtitle:
                script += f" subtitle {json.dumps(subtitle)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


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


def main():
    repo = Path(os.environ.get("YAAS_NOTIFY_REPO_ROOT")
                or _repo_root(__file__))
    wm_file = Path(os.environ.get("YAAS_NOTIFY_WATERMARK")
                   or repo / "state" / "last_notified.ts")
    now = datetime.now(timezone.utc).timestamp()

    # First run, or an unreadable watermark: set it and fire nothing.
    if not wm_file.exists():
        wm_file.parent.mkdir(parents=True, exist_ok=True)
        wm_file.write_text(str(now))
        return 0
    try:
        watermark = float(wm_file.read_text().strip())
    except (OSError, ValueError):
        wm_file.write_text(str(now))
        return 0

    events = sorted(
        list(from_timelines(repo, watermark))
        + list(from_runlog(repo, watermark))
        + list(from_reactions(repo, watermark)),
        key=lambda e: e[0])

    cmd = (os.environ.get("YAAS_NOTIFY_CMD") or "").strip()
    for _ts, title, subtitle, body in events[:MAX_PER_RUN]:
        deliver(title, subtitle, body, cmd)

    wm_file.write_text(str(datetime.now(timezone.utc).timestamp()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
