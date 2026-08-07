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
    python3 yaas-triage/surfaces/slack-send.py '<json>'

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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
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


REPO_ROOT = _repo_root(__file__)
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


# How old the conversation may be before a reply needs human review, in hours.
STALE_HOURS = float(os.environ.get("YAAS_STALE_REPLY_HOURS", "24"))

SCRIPT_DIR = Path(__file__).resolve().parent


def _thread_last_activity(channel_id: str, thread_ts: str):
    """Epoch of the newest message in the target thread, or None if unreadable.

    This is the staleness signal, and it is deliberately measured HERE rather than
    passed in by triage. A reply is stale when the conversation it answers has already
    gone quiet, which is a property of the thread, not of how the dispatch was
    triggered. Measuring it at the send site means manual dispatches and reaction
    replies are covered by the same rule, with nothing to remember.
    """
    try:
        # slack_read_thread names the parent ts `message_ts`, not `thread_ts` (see
        # checkers/slack_thread.py, the canonical caller). Passing `thread_ts` makes the tool
        # reject the call with "Missing value for parameter message_ts", the read raises, and
        # the guard fails CLOSED — silently holding every threaded reply as "unreadable". That
        # is what stranded a reviewed self-DM draft.
        body = _call_slack("slack_read_thread",
                           {"channel_id": channel_id, "message_ts": thread_ts})
    except Exception:
        return None
    newest = None
    # Message timestamps are Slack ts values: "1785920000.123456". Take the largest
    # one anywhere in the response rather than assuming a shape, since the MCP tool
    # returns prose with embedded ts fields.
    for m in re.finditer(r'\b(1[6-9]\d{8}\.\d{6})\b', body):
        v = float(m.group(1))
        newest = v if newest is None else max(newest, v)
    return newest


def _stale_reason(channel_id, thread_ts, now=None):
    """Should this send be held for review? Returns a reason string, or None to send.

    Fails CLOSED: if the thread cannot be read, we cannot show the conversation is
    still live, so the reply is queued rather than sent. Auto-replying into a thread
    whose state is unknown is the exact failure this exists to prevent.
    """
    if os.environ.get("YAAS_FORCE_DRAFT") == "1":
        return "catch-up mode is active (YAAS_FORCE_DRAFT=1)"
    if not thread_ts:
        # A new top-level message is not a reply to anything, so it has no staleness.
        return None
    now = now if now is not None else time.time()
    newest = _thread_last_activity(channel_id, thread_ts)
    if newest is None:
        return "could not read the thread to confirm it is still live"
    age_h = (now - newest) / 3600.0
    if age_h > STALE_HOURS:
        return (f"newest message in the thread is {age_h:.1f}h old "
                f"(limit {STALE_HOURS:.0f}h), so the conversation has moved on")
    return None


def _queue_for_review(p, reason):
    """Route a would-be send into the approval queue instead of sending it."""
    quest_id = p.get("quest_id") or ""
    payload = {
        "quest_id": quest_id,
        "quest_title": p.get("quest_title", quest_id),
        "action_type": "slack_message",
        "target": {"channel_id": p.get("channel_id"), "thread_ts": p.get("thread_ts")},
        "message_text": p.get("message"),
        "context": (p.get("note") or "") + f" [held automatically: {reason}]",
        "risk_reason": f"stale-reply guard: {reason}",
    }
    out = subprocess.run(
        ["python3", str(SCRIPT_DIR.parent / "ledger" / "approval-helper.py"), "write", json.dumps(payload)],
        capture_output=True, text=True)
    return (out.stdout or "").strip()


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

    # 0. Stale-reply guard. A reply to a conversation that went quiet more than
    #    STALE_HOURS ago is almost never still wanted: after a pause, triage hands the
    #    worker the OLDEST unread slice first, so without this it would march forward
    #    through days of backlog answering questions that were resolved without it.
    #    Enforced here, in the only sanctioned send path, rather than as a rule in
    #    CLAUDE.md, because a rule the model can forget is not a guard.
    if not is_draft:
        reason = _stale_reason(channel_id, thread_ts)
        if reason:
            appr_id = _queue_for_review(p, reason)
            quest_dir = _quest_dir(quest_id) if quest_id else None
            if quest_dir:
                _append_timeline(quest_dir, {
                    "ts": _utc_now(), "event": "draft_posted",
                    "channel_id": channel_id, "thread_ts": thread_ts,
                    "approval_id": appr_id, "held_reason": reason,
                    "note": (note or "") + " [auto-held by the stale-reply guard]"})
            print(json.dumps({"held": True, "reason": reason,
                              "approval_id": appr_id, "response_ts": "", "permalink": ""}))
            return 0

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
