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
    Also appends an `approval` watch to the quest's watch.json (additive,
    idempotent) so triage re-dispatches the worker when the reviewer approves
    or sends the item back. This is done as part of `write` on purpose: an
    approval with no watch is invisible to triage and never re-surfaces.

enqueue-instruction <json>
    Add a dashboard-authorized manual instruction with status reviewed. Every
    call creates a distinct item; its generated id is the uniqueness boundary.
    The next locked tick arms its approval watch. Prints a JSON result.

arm-pending-instructions
    Arm every queued manual instruction. Called by tick.py only after it has
    acquired the global triage lock.

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

abandon <id> <reason>
    Terminally cancel an executing manual instruction whose outcome cannot be
    reconciled safely. This prevents an expired lease from dispatching forever.
"""

import fcntl
import json
import os
import random
import string
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


REPO_ROOT      = _repo_root(__file__)
APPROVALS_FILE = REPO_ROOT / "state" / "pending-approvals.json"
# Locking a sidecar, not the data file: see _write_queue().
_LOCK_FILE     = REPO_ROOT / "state" / "pending-approvals.json.lock"


# Every command now opens the sidecar lock, so any of them can be the first to run on a
# fresh install, whereas only cmd_write used to create this directory. Done once here rather
# than at each of the five call sites, which sit at different indentation levels.
APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
QUESTS_DIR     = REPO_ROOT / "state" / "quests"

# Long enough to cover the 30-minute worker watchdog plus slack, short enough that a
# lost approval is recovered the same hour.
LEASE_MINUTES  = int(os.environ.get("YAAS_APPROVAL_LEASE_MIN", "45"))


def _find_watch_json(quest_id: str) -> Path | None:
    """Locate a quest's watch.json across active/completed/archived."""
    for bucket in ("active", "completed", "archived"):
        p = QUESTS_DIR / bucket / quest_id / "watch.json"
        if p.exists():
            return p
    return None


def _arm_approval_watch(quest_id: str, approval_id: str):
    """Append an `approval` watch so triage re-dispatches the worker when the
    reviewer approves or sends the item back (needs_reply). Additive only, and
    idempotent on approval_id. This is coupled to item creation on purpose: an
    approval with no watch is invisible to triage and sits forever (an item that
    gets queued off this path, with no watch, strands in needs_reply). Failure
    here must not lose the already-written approval, so any error is reported to
    stderr and swallowed."""
    entry = {
        "type": "approval",
        "approval_id": approval_id,
        "last_checked_ts": str(int(datetime.now(timezone.utc).timestamp())),
        "reason": f"execute reviewed approval {approval_id}",
    }
    helper = REPO_ROOT / "yaas-triage" / "ledger" / "add-watch.py"
    cp = subprocess.run(
        ["python3", str(helper), quest_id, json.dumps(entry)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if cp.returncode == 0:
        return True
    reason = (cp.stderr or cp.stdout or "approval watch append failed").strip()[:200]
    print(f"warn:arm_watch_failed:{approval_id}:{reason}", file=sys.stderr)
    _flag_unarmed(approval_id, reason)
    return False


def _flag_unarmed(approval_id: str, reason: str):
    """Mark an approval whose watch could not be armed. Swallowing the arming error is
    correct (losing the approval would be worse) but it must not be silent: triage
    cannot see an unarmed approval at all, so the dashboard is the only backstop, and
    it reads pending-approvals.json directly without needing the watch."""
    try:
        with open(_LOCK_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = _read_queue()
                item = next((i for i in data.get("items", []) if i["id"] == approval_id), None)
                if item is not None:
                    item["watch_armed"] = False
                    item["watch_arm_error"] = reason[:200]
                    _write_queue(data)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


def _read_queue() -> dict:
    """Read the queue by path. The caller must hold the sidecar lock."""
    try:
        d = json.loads(APPROVALS_FILE.read_text())
        return d if isinstance(d, dict) else {"version": 1, "items": []}
    except Exception:
        return {"version": 1, "items": []}


def _write_queue(data: dict):
    """Write the queue atomically. The caller must hold the sidecar lock.

    The old path did seek(0) + truncate() + dump, so a crash between the truncate and the
    write left an empty file and every pending approval was gone — including drafts a human
    had already reviewed. temp + fsync + os.replace cannot leave a partial file.

    Because os.replace swaps the inode, the LOCK cannot live on this file: a second writer
    blocked on it would wake holding a deleted inode, read stale contents through its old
    descriptor, and write them back over the first writer's change. Hence _LOCK_FILE. That
    lost-update trap was already fixed once in add-watch.py.
    """
    tmp = APPROVALS_FILE.with_name(APPROVALS_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, APPROVALS_FILE)


def _uid() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _new_id(data: dict) -> str:
    """Mint an existing-style id that is unique within the durable ledger."""
    existing = {i.get("id") for i in data.get("items", []) if isinstance(i, dict)}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    while True:
        approval_id = f"appr-{stamp}-{_uid()}"
        if approval_id not in existing:
            return approval_id


# Reaction-sourced drafts arrive with quest_id "reactions", which is the fast-path target and
# has no quest folder — so the approval watch could never arm and the item stranded at
# "reviewed" forever (a real incident). They are routed to a durable executor-only host quest
# instead. This is a SPECIFIC map for the one known non-quest target, NOT a generic
# "missing quest -> fallback" (that would hide typos and funnel unrelated broken approvals
# into the host, per review).
REACTIONS_TARGET = "reactions"
HOST_QUEST = "quest-reactions-approvals"

_HOST_CONTEXT = """# Reaction approval executor

This quest exists for ONE job: hold the `approval` watches for drafts that originated from a
Slack `draft` reaction, which has no quest of its own, and let the normal
reviewed-approval execution path (CLAUDE.md §3d) send them once a human approves.

Rules for the worker dispatched here:
- Execute the reviewed approval item(s) per §3d (`approval-helper.py start` -> send -> `done`).
  That is the whole task.
- Do NOT append a `slack_thread` watch afterward. The conversation lives in its original
  thread, not here; this quest must stay an executor and never accrue follow-up watches.
- Never mark this quest completed or archived. It is permanent and usually empty.
"""


def _ensure_host_quest():
    """Create the durable executor-only host quest if absent. Returns its watch.json path.

    Self-bootstrapping so a fresh install and this machine both work with no manual step. The
    quest starts with an empty watches[] (so it is dispatch-inert until an approval arms one)
    and allow_send:false (executing a reviewed approval is already the human-authorized action;
    the quest itself opens no new sends).
    """
    qdir = QUESTS_DIR / "active" / HOST_QUEST
    watch = qdir / "watch.json"
    if watch.exists():
        return watch
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "meta.json").write_text(json.dumps({
        "id": HOST_QUEST,
        "title": "Reaction approval executor",
        "status": "active",
        "priority": "normal",
        "allow_send": False,
        "retire_slack_threads_after_days": "never",
    }, indent=2) + "\n")
    watch.write_text('{"watches": []}\n')
    (qdir / "context.md").write_text(_HOST_CONTEXT)
    (qdir / "timeline.ndjson").touch()
    return watch


def cmd_write(payload_json: str):
    payload = json.loads(payload_json)

    quest_id   = payload["quest_id"]
    source     = None
    if quest_id == REACTIONS_TARGET:
        # Route to the durable host quest; keep the origin in `source` for provenance.
        _ensure_host_quest()
        source = REACTIONS_TARGET
        quest_id = HOST_QUEST
    target     = payload.get("target", {})
    channel_id = target.get("channel_id")
    thread_ts  = target.get("thread_ts")

    APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not APPROVALS_FILE.exists():
        APPROVALS_FILE.write_text('{"version": 1, "items": []}')

    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
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

            new_id = None
            now = datetime.now(timezone.utc).isoformat()
            item = {
                "id":           _new_id(data),
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
            if source:
                item["source"] = source
            data.setdefault("items", []).append(item)
            _write_queue(data)
            new_id = item["id"]
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    # Arm the tracking watch OUTSIDE the approvals lock (different file, its own
    # flock). Coupled here so an approval can never be created without its watch.
    if new_id:
        _arm_approval_watch(quest_id, new_id)
        print(new_id)


def cmd_enqueue_instruction(payload_json: str) -> int:
    """Persist one operator-authorized instruction without target deduplication.

    The approval watch is armed by `arm-pending-instructions` at the start of the
    next tick, while that tick owns the global triage lock. Writing watch.json
    here would race the tick whose lock this queue exists to wait behind.
    """
    payload = json.loads(payload_json)
    quest_id = str(payload.get("quest_id") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    if not quest_id or not instruction:
        print("error:quest_id and instruction are required", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
            item = {
                "id":           _new_id(data),
                "quest_id":     quest_id,
                "quest_title":  payload.get("quest_title", quest_id),
                "created_at":   now,
                "status":       "reviewed",
                "action_type":  "manual_instruction",
                "target":       {},
                "message_text": instruction,
                "context":      payload.get("context", "Submitted from the quest dashboard."),
                "risk_reason":  "",
                "reviewed_at":  now,
                "watch_armed":  False,
                "watch_arm_pending": True,
            }
            data.setdefault("items", []).append(item)
            _write_queue(data)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    print(json.dumps({"approval_id": item["id"], "queued": True}))
    return 0


def cmd_arm_pending_instructions() -> int:
    """Arm queued manual instructions while the caller owns the triage lock."""
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
            pending = [
                (i.get("id"), i.get("quest_id"))
                for i in data.get("items", [])
                if i.get("action_type") == "manual_instruction"
                and i.get("status") == "reviewed"
                and i.get("watch_armed") is not True
            ]
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    armed_count = cancelled_count = 0
    for approval_id, quest_id in pending:
        active_watch = QUESTS_DIR / "active" / str(quest_id) / "watch.json"
        armed = active_watch.exists() and _arm_approval_watch(str(quest_id), str(approval_id))
        with open(_LOCK_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = _read_queue()
                queued = next((i for i in data.get("items", [])
                               if i.get("id") == approval_id), None)
                if queued is not None:
                    if armed:
                        queued["watch_armed"] = True
                        queued.pop("watch_arm_pending", None)
                        queued.pop("watch_arm_error", None)
                        armed_count += 1
                    else:
                        queued["status"] = "cancelled"
                        queued["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                        queued["cancel_reason"] = "quest is no longer active or dispatch watch could not be armed"
                        queued.pop("watch_arm_pending", None)
                        cancelled_count += 1
                    _write_queue(data)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    print(json.dumps({"pending": len(pending), "armed": armed_count,
                      "cancelled": cancelled_count}))
    return 0 if cancelled_count == 0 else 3


def cmd_start(approval_id: str):
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
            item = next((i for i in data.get("items", []) if i["id"] == approval_id), None)
            if item is None:
                print(f"error:not_found:{approval_id}", file=sys.stderr)
                sys.exit(1)
            if item["status"] in ("executing", "executed", "cancelled"):
                print(f"skip:{item['status']}")
                return
            now = datetime.now(timezone.utc)
            item["status"]       = "executing"
            item["executing_at"] = now.isoformat()
            # A claim without an expiry is a claim forever. If the worker dies between
            # `start` and `done` (watchdog kill, mac sleep, MCP failure mid-send) the
            # item used to sit in `executing` where the checker reads it as clean and
            # the dashboard did not render it at all — an approved message lost with no
            # surface anywhere. The lease lets approval.py re-surface it.
            item["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
            _write_queue(data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_answer(approval_id: str, payload_json: str):
    payload = json.loads(payload_json)
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
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
            _write_queue(data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_done(approval_id: str, response_ts: str | None = None):
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
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
            _write_queue(data)
            print("ok")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cmd_abandon(approval_id: str, reason: str):
    reason = reason.strip()
    if not reason:
        print("error:reason_required", file=sys.stderr)
        sys.exit(1)
    with open(_LOCK_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read_queue()
            item = next((i for i in data.get("items", []) if i.get("id") == approval_id), None)
            if item is None:
                print(f"error:not_found:{approval_id}", file=sys.stderr)
                sys.exit(1)
            if item.get("action_type") != "manual_instruction":
                print("error:not_manual_instruction", file=sys.stderr)
                sys.exit(1)
            if item.get("status") != "executing":
                print(f"skip:{item.get('status', 'unknown')}")
                return
            item["status"] = "cancelled"
            item["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            item["cancel_reason"] = reason[:1000]
            _write_queue(data)
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
    elif cmd == "enqueue-instruction":
        sys.exit(cmd_enqueue_instruction(sys.argv[2]))
    elif cmd == "arm-pending-instructions":
        sys.exit(cmd_arm_pending_instructions())
    elif cmd == "start":
        cmd_start(sys.argv[2])
    elif cmd == "answer":
        cmd_answer(sys.argv[2], sys.argv[3])
    elif cmd == "done":
        cmd_done(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "abandon":
        cmd_abandon(sys.argv[2], sys.argv[3])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
