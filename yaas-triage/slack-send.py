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
slack-send.py — send a Slack message (or draft) AND log the verbatim body to a
quest's timeline in one atomic step.

Why this exists
===============
CLAUDE.md requires every outbound send to be logged to timeline.ndjson with a
`message_text` field carrying the exact text sent, because the Sidequestor
dashboard only surfaces a message when that field is present. Relying on the LLM
worker to remember that as a separate step is fragile (and fails outright on the
non-Claude backends). This helper makes body-capture structural: the send and
the timeline write happen together, so a message can never land in Slack without
its body also landing in the timeline.

Send through this helper instead of calling slack_send_message directly whenever
the send belongs to a quest.

Usage
=====
    python3 yaas-triage/slack-send.py '<json>'

<json> fields:
    channel_id       (required)  channel/DM ID (C.../D.../G...) or user_id for a DM
    message          (required)  the verbatim body to send AND log
    thread_ts        (optional)  parent ts to reply in-thread
    reply_broadcast  (optional)  bool; also post a thread reply to the channel
    draft            (optional)  bool; save a draft via slack_send_message_draft
                                 (nothing is sent; logged as draft_posted)
    quest_id         (optional)  quest to log under. If omitted, nothing is
                                 logged (send-only; e.g. reactions fast path).
    event            (optional)  timeline event name. Default "message_sent"
                                 ("draft_posted" when draft=true).
    note             (optional)  short human summary for the timeline `note`.

Output (stdout): compact JSON, e.g.
    {"response_ts":"1784280637.486119","permalink":"https://...","channel_id":"C...","logged":true}
Callers use response_ts to build the follow-up watch.json entry (CLAUDE.md 3a).

Exit codes:
    0  sent (or drafted) successfully
    1  bad arguments
    2  send failed (Slack/MCP error) — nothing sent, nothing logged
"""

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MCP_CALL = os.environ.get("MCP_CALL", str(Path(__file__).parent / "mcp-call.sh"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quest_dir(quest_id: str) -> Path | None:
    """Locate a quest folder across active/completed/archived."""
    base = REPO_ROOT / "state" / "quests"
    for bucket in ("active", "completed", "archived"):
        d = base / bucket / quest_id
        if d.is_dir():
            return d
    return None


def _append_timeline(quest_dir: Path, entry: dict):
    """Append one NDJSON line to the quest timeline under an exclusive lock."""
    path = quest_dir / "timeline.ndjson"
    line = json.dumps(entry, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _call_slack(tool: str, args: dict) -> str:
    r = subprocess.run(
        [MCP_CALL, tool, json.dumps(args)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"{tool} failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()[:200]}"
        )
    body = r.stdout.strip()
    if not body:
        raise RuntimeError(f"{tool} returned empty response")
    return body


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    try:
        p = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON argument: {e}", file=sys.stderr)
        sys.exit(1)

    channel_id = p.get("channel_id")
    message = p.get("message")
    if not channel_id or message is None:
        print("error: channel_id and message are required", file=sys.stderr)
        sys.exit(1)

    is_draft = bool(p.get("draft"))
    thread_ts = p.get("thread_ts")
    quest_id = p.get("quest_id")
    note = p.get("note", "")
    event = p.get("event") or ("draft_posted" if is_draft else "message_sent")

    args = {"channel_id": channel_id, "message": message}
    if thread_ts:
        args["thread_ts"] = thread_ts
    if p.get("reply_broadcast") and not is_draft:
        args["reply_broadcast"] = True

    tool = "slack_send_message_draft" if is_draft else "slack_send_message"

    # 1. Send / draft. On any failure nothing is logged (the message never landed).
    try:
        body = _call_slack(tool, args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    # 2. Parse the response for the sent-message coordinates.
    response_ts = ""
    permalink = ""
    try:
        d = json.loads(body)
        # slack_send_message: {"message_link":..,"message_context":{"message_ts":..,"channel_id":..}}
        ctx = d.get("message_context", {}) if isinstance(d, dict) else {}
        response_ts = ctx.get("message_ts", "") or ""
        permalink = d.get("message_link", "") if isinstance(d, dict) else ""
        # slack_send_message_draft returns {"channel_link":..} — no message_ts.
        if not permalink:
            permalink = d.get("channel_link", "") if isinstance(d, dict) else ""
    except json.JSONDecodeError:
        # Non-JSON body usually means an error string slipped through (e.g. a
        # restricted channel). Surface it and treat the send as failed.
        print(f"error: unexpected response from {tool}: {body[:200]}", file=sys.stderr)
        sys.exit(2)

    # 3. Log the verbatim body to the quest timeline (the whole point).
    logged = False
    if quest_id:
        qdir = _quest_dir(quest_id)
        if qdir is None:
            print(f"warning: quest '{quest_id}' not found; send succeeded but "
                  f"not logged", file=sys.stderr)
        else:
            entry = {
                "ts": _utc_now(),
                "event": event,
                "channel_id": channel_id,
                "message_text": message,
            }
            if thread_ts:
                entry["thread_ts"] = thread_ts
            # For a draft there is no sent ts; fall back to thread_ts so a
            # follow-up watch has a sensible boundary (CLAUDE.md 3a).
            entry["response_ts"] = response_ts or (thread_ts or "")
            if permalink:
                entry["permalink"] = permalink
            if note:
                entry["note"] = note
            _append_timeline(qdir, entry)
            logged = True

    print(json.dumps({
        "response_ts": response_ts,
        "permalink": permalink,
        "channel_id": channel_id,
        "logged": logged,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
