#!/bin/bash
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

# notify.sh — fire macOS notifications for new YAAS actions since last run.
#
# Reads state/last_notified.ts (epoch float). On first run, initialises the
# watermark to now and exits without firing any notifications — avoids a
# flood of historical events.
#
# Scans:
#   - active quest timeline.ndjson  for message_sent / draft_posted / executed
#   - claude_intensifies_replied.json  for new reaction replies
#   - writing_hand_replied.json     for new draft reactions
#   - run-log.ndjson                for triage health events (misconfigured watch,
#                                   budget cap hit, saturated window, breaker open)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# REPO_ROOT and WATERMARK are env-overridable so the unit test can point at a
# throwaway fixture tree instead of real state. Defaults preserve prod behavior.
REPO_ROOT="${YAAS_NOTIFY_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
WATERMARK="${YAAS_NOTIFY_WATERMARK:-$REPO_ROOT/state/last_notified.ts}"

# Delivery is pluggable. If YAAS_NOTIFY_CMD is set, each notification is sent by
# invoking it as:  $YAAS_NOTIFY_CMD <title> <subtitle> <body>  (3 argv items).
# The unit test points this at a recorder; in prod it's unset → osascript.
python3 - "$REPO_ROOT" "$WATERMARK" <<'PYEOF'
import sys, os, json, subprocess
from pathlib import Path
from datetime import datetime, timezone

repo      = Path(sys.argv[1])
wm_file   = Path(sys.argv[2])

# First run: set watermark to now and skip — avoids historical flood
if not wm_file.exists():
    wm_file.write_text(str(datetime.now(timezone.utc).timestamp()))
    sys.exit(0)

watermark = float(wm_file.read_text().strip())
notifications = []

def parse_ts(ts_str):
    """ISO-8601 → epoch float. Returns None on failure."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

# ── Quest timelines ───────────────────────────────────────────────────────────
active_dir = repo / "state" / "quests" / "active"
for timeline in active_dir.glob("*/timeline.ndjson"):
    quest_dir = timeline.parent
    try:
        meta = json.loads((quest_dir / "meta.json").read_text())
        title = meta.get("title", quest_dir.name)
    except Exception:
        title = quest_dir.name

    for raw in timeline.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
            ts = parse_ts(entry.get("ts", ""))
            if ts is None or ts <= watermark:
                continue
            event = entry.get("event", "")
            if event not in ("message_sent", "draft_posted", "executed"):
                continue
            labels = {
                "message_sent": "message sent",
                "draft_posted":  "draft created — review needed",
                "executed":      "action executed",
            }
            note = (entry.get("note") or "")[:60]
            notifications.append((ts, f"YAAS — {labels[event]}", title, note))
        except Exception:
            continue

# ── Health events from the run log ────────────────────────────────────────────
# These are the "the system quietly stopped working" signals. Before this, every
# one of them reached exactly one place: a line in logs/triage.log that nobody
# reads. gate_watch_misconfigured in particular had never fired even once, so the
# path was both invisible and untested.
RUNLOG_EVENTS = {
    "gate_watch_misconfigured": ("watch misconfigured — needs a human",
                                 lambda e: f"{e.get('quest','?')} [{e.get('type','?')}] {e.get('reason','')}"),
    "gate_budget_exceeded":     ("BUDGET CAP HIT — dispatch withheld",
                                 lambda e: e.get("reason", "")),
    "gate_watch_backlog":       ("saturated window — cursor held",
                                 lambda e: f"{e.get('quest','?')}: {e.get('watches','?')} watch(es) had more activity than one page"),
    "gate_target_breaker_open": ("target breaker open — dispatch stopped",
                                 lambda e: f"{e.get('target','?')} ran {e.get('dispatches_1h','?')}x in an hour"),
    "gate_ack_manifest_unreadable": ("ack manifest unreadable — work held",
                                 lambda e: f"{e.get('quest','?')} run {e.get('run_id','?')}"),
}
runlog = repo / "state" / "run-log.ndjson"
if runlog.exists():
    # Collapse to one notification per event type per run, otherwise a persistent
    # misconfiguration would fire on every tick forever. The watermark handles
    # across-run dedup; this handles within-run.
    seen_kinds = {}
    for raw in runlog.read_text().splitlines():
        if not raw.strip().startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("event", "")
        if kind not in RUNLOG_EVENTS:
            continue
        ts = parse_ts(entry.get("ts", ""))
        if ts is None or ts <= watermark:
            continue
        label, detail = RUNLOG_EVENTS[kind]
        try:
            body = detail(entry)[:110]
        except Exception:
            body = ""
        seen_kinds[kind] = (ts, f"YAAS — {label}", "triage health", body)
    notifications.extend(seen_kinds.values())

# ── Reaction state files ──────────────────────────────────────────────────────
REACTION_FILES = {
    "claude_intensifies_replied.json": ("replied_timestamps", "replied to :claude-intensifies:"),
    "writing_hand_replied.json":  ("replied_timestamps", "drafted reply for :writing_hand:"),
}
for fname, (key, label) in REACTION_FILES.items():
    p = repo / "state" / fname
    if not p.exists():
        continue
    try:
        for ts_str in json.loads(p.read_text()).get(key, []):
            ts = float(ts_str)
            if ts > watermark:
                notifications.append((ts, f"YAAS — {label}", "", ""))
    except Exception:
        continue

# ── Delivery (pluggable) ──────────────────────────────────────────────────────
notify_cmd = os.environ.get("YAAS_NOTIFY_CMD", "").strip()

def deliver(title, subtitle, body):
    """Send one notification. Guarded so a single failure never aborts the run
    (which would skip the watermark advance and re-notify everything next tick)."""
    try:
        if notify_cmd:
            subprocess.run([notify_cmd, title, subtitle, body],
                           capture_output=True, timeout=5)
        else:
            script = f'display notification {json.dumps(body)} with title {json.dumps(title)}'
            if subtitle:
                script += f' subtitle {json.dumps(subtitle)}'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass

# ── Fire (cap at 10 per run to avoid banner pile-up) ─────────────────────────
notifications.sort(key=lambda x: x[0])
for _, title, subtitle, body in notifications[:10]:
    deliver(title, subtitle, body)

# ── Advance watermark ─────────────────────────────────────────────────────────
wm_file.write_text(str(datetime.now(timezone.utc).timestamp()))
PYEOF
