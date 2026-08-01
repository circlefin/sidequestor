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
approval-helper.py — manage entries in state/pending-approvals.json.

All reads and writes use an exclusive flock to prevent races between the
dashboard server (which writes "reviewed"/"cancelled") and the worker (which
writes new items and "executing"/"executed").

Sub-commands
────────────
write <json>
    Add a new pending_review item. <json> must be a JSON object with at least:
      quest_id, quest_title, action_type, target (object), message_text,
      context, risk_reason
    Prints the generated approval ID on success.
    Prints nothing (exit 0) if an identical pending entry already exists
    (same quest_id + target.channel_id + target.thread_ts).

start <id>
    Transition status pending_review|reviewed → executing.
    Prints "ok" on success.
    Prints "skip:<current_status>" if already executing/executed/cancelled.

answer <id> <json>
    Worker's response to a reviewer question (status needs_reply). <json> may
    carry: worker_reply (the answer text), message_text (a revised draft).
    Records both, appends the round to review_history, and flips status back to
    pending_review so the item re-surfaces on the dashboard for another review
    pass. Does NOT send. Prints "ok", or "skip:<status>" if not needs_reply.

done <id> [response_ts | result_url]
    Transition executing → executed. Optionally records the Slack response_ts,
    or — for a Jira comment / GitHub PR comment / Gmail reply — pass the URL of
    the posted reply instead and it is stored as result_url so the dashboard can
    link to it. Prints "ok" on success.
"""

import fcntl
import json
import random
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT      = Path(__file__).parent.parent
APPROVALS_FILE = REPO_ROOT / "state" / "pending-approvals.json"


def _load_locked(f) -> dict:
    f.seek(0)
    return json.load(f)


def _save_locked(f, data: dict):
    f.seek(0)
    f.truncate()
    json.dump(data, f, indent=2)


def _uid() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def cmd_write(payload_json: str):
    payload = json.loads(payload_json)

    quest_id   = payload["quest_id"]
    target     = payload.get("target", {})
    channel_id = target.get("channel_id")
    thread_ts  = target.get("thread_ts")

    APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not APPROVALS_FILE.exists():
        APPROVALS_FILE.write_text('{"version": 1, "items": []}')

    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _load_locked(f)
            # Dedup: same quest + target already pending?
            duplicate = any(
                i for i in data.get("items", [])
                if i.get("quest_id") == quest_id
                and i.get("status") == "pending_review"
                and i.get("target", {}).get("channel_id") == channel_id
                and i.get("target", {}).get("thread_ts") == thread_ts
            )
            if duplicate:
                return  # print nothing — caller treats as no-op

            now = datetime.now(timezone.utc).isoformat()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            item = {
                "id":           f"appr-{stamp}-{_uid()}",
                "quest_id":     quest_id,
                "quest_title":  payload.get("quest_title", quest_id),
                "created_at":   now,
                "status":       "pending_review",
                "action_type":  payload.get("action_type", "slack_message"),
                "target":       target,
                "message_text": payload.get("message_text", ""),
                "context":      payload.get("context", ""),
                "risk_reason":  payload.get("risk_reason", ""),
            }
            data.setdefault("items", []).append(item)
            _save_locked(f, data)
            print(item["id"])
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_start(approval_id: str):
    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _load_locked(f)
            item = next((i for i in data.get("items", []) if i["id"] == approval_id), None)
            if item is None:
                print(f"error:not_found:{approval_id}", file=sys.stderr)
                sys.exit(1)
            if item["status"] in ("executing", "executed", "cancelled"):
                print(f"skip:{item['status']}")
                return
            item["status"]      = "executing"
            item["executing_at"] = datetime.now(timezone.utc).isoformat()
            _save_locked(f, data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_answer(approval_id: str, payload_json: str):
    payload = json.loads(payload_json)
    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _load_locked(f)
            item = next((i for i in data.get("items", []) if i["id"] == approval_id), None)
            if item is None:
                print(f"error:not_found:{approval_id}", file=sys.stderr)
                sys.exit(1)
            if item["status"] != "needs_reply":
                print(f"skip:{item['status']}")
                return
            now = datetime.now(timezone.utc).isoformat()
            hist = item.setdefault("review_history", [])
            if item.get("review_note"):
                hist.append({"from": "reviewer", "note": item["review_note"],
                             "at": item.get("asked_at")})
            reply = payload.get("worker_reply", "")
            hist.append({"from": "worker", "reply": reply, "at": now})
            item["worker_reply"] = reply
            if payload.get("message_text"):
                item["message_text"]     = payload["message_text"]
                item["revised_by_worker"] = True
            # consume the question so the next review round starts clean
            item.pop("review_note", None)
            item["status"]      = "pending_review"
            item["answered_at"] = now
            _save_locked(f, data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_done(approval_id: str, response_ts: str | None = None):
    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _load_locked(f)
            item = next((i for i in data.get("items", []) if i["id"] == approval_id), None)
            if item is None:
                print(f"error:not_found:{approval_id}", file=sys.stderr)
                sys.exit(1)
            item["status"]   = "executed"
            item["sent_at"]  = datetime.now(timezone.utc).isoformat()
            if response_ts:
                # Non-Slack executions (Jira comment, GitHub PR comment, Gmail
                # reply) have a URL, not a ts. Keep it as result_url so the
                # dashboard can link straight to the posted reply.
                if response_ts.startswith("https://"):
                    item["result_url"] = response_ts
                else:
                    item["response_ts"] = response_ts
            _save_locked(f, data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "write":
        cmd_write(sys.argv[2])
    elif cmd == "start":
        cmd_start(sys.argv[2])
    elif cmd == "answer":
        cmd_answer(sys.argv[2], sys.argv[3])
    elif cmd == "done":
        cmd_done(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
