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
    Add a new pending_review item. Missing quest_id and the reserved reactions
    source are normalized to quest-inbox; an unknown explicit quest is rejected.
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
    Arm queued manual instructions and retry explicitly unarmed approvals.
    Called by tick.py only after it has acquired the global triage lock.

ensure-inbox
    Idempotently create the permanent Inbox quest. Called by setup.sh.

migrate-inbox
    One-time migration of legacy blank/reaction approvals into Inbox.

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

done <id> [response_ts | result_url] [report]
  report: one line per action the instruction produced; lands in the review trail
    Transition executing → executed. Optionally records the Slack response_ts,
    or — for a Jira comment / GitHub PR comment / Gmail reply — pass the URL of
    the posted reply instead and it is stored as result_url so the dashboard can
    link to it. Prints "ok" on success.

abandon <id> <reason>
    Terminally cancel an executing manual instruction whose outcome cannot be
    reconciled safely. This prevents an expired lease from dispatching forever.

fail <id> <reason>
    Return an approval the worker could not process to pending_review and record
    the error for the operator. The approval will not dispatch again until reviewed.
"""

from __future__ import annotations  # PEP 604 unions below must not be
# evaluated at def time: this file has to import on Python < 3.10.

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
    override = (os.environ.get("SIDEQUESTOR_WORKSPACE")
                or os.environ.get("YAAS_WORKSPACE"))
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT      = _repo_root(__file__)
# Workspace state and packaged runtime are separate roots after installation.
# Resolve imports from this file so direct helper execution works without PYTHONPATH.
RUNTIME_ROOT   = Path(os.environ.get("YAAS_RUNTIME_ROOT", Path(__file__).resolve().parents[1]))
if (RUNTIME_ROOT / "yaas-triage").is_dir():
    RUNTIME_ROOT = RUNTIME_ROOT / "yaas-triage"
sys.path.insert(0, str(RUNTIME_ROOT))
import approval_state
import approval_store
import tick_state

approval_state.configure(tick_state.load_environment(REPO_ROOT))

APPROVALS_FILE = approval_store.APPROVALS_FILE
QUESTS_DIR     = REPO_ROOT / "state" / "quests"


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
    helper = RUNTIME_ROOT / "ledger" / "add-watch.py"
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
        approval_store.mutate_item(approval_id, lambda item: {
            "watch_armed": False,
            "watch_arm_error": reason[:200],
        })
    except Exception:
        pass


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


# Every approval has one dispatch owner. Unlinked work is filed into the permanent Inbox quest;
# `source` records where it came from without creating a second routing identity.
REACTIONS_TARGET = "reactions"
INBOX_QUEST = "quest-inbox"
LEGACY_HOST_QUEST = "quest-reactions-approvals"

_INBOX_CONTEXT = """# Inbox

This permanent quest owns one-off work that did not originate in another quest. Each approval
is independent and must be processed from the exact `approval` watch named in the dispatch.

Rules for the worker dispatched here:
- Execute the reviewed approval item(s) per §3d (`approval-helper.py start` -> send -> `done`).
  Process every fired approval watch separately; one failure must not block its siblings.
- Reactions still use their fast path unless they require review. Only reviewed reaction work
  lands here.
- If an item becomes ongoing work, create or adopt a dedicated quest rather than growing Inbox
  into a project.
- Never mark Inbox completed or archived. It is permanent and usually empty.
"""


def _ensure_inbox():
    """Create the permanent Inbox quest. Setup and explicit migrations call this."""
    qdir = QUESTS_DIR / "active" / INBOX_QUEST
    watch = qdir / "watch.json"
    qdir.mkdir(parents=True, exist_ok=True)
    with open(QUESTS_DIR / ".quest-inbox.lock", "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        meta_path = qdir / "meta.json"
        if watch.exists():
            return watch
        meta_path.write_text(json.dumps({
            "id": INBOX_QUEST,
            "title": "Inbox",
            "status": "active",
            "priority": "normal",
            "allow_send": False,
            "system_role": "inbox",
            "retire_slack_threads_after_days": "never",
        }, indent=2) + "\n")
        (qdir / "context.md").write_text(_INBOX_CONTEXT)
        (qdir / "timeline.ndjson").touch()
        # Publish watch.json last so tick.py cannot dispatch a half-built quest.
        watch.write_text('{"watches": []}\n')
    return watch


def cmd_write(payload_json: str):
    payload = json.loads(payload_json)

    requested_quest_id = str(payload.get("quest_id") or "").strip()
    source = str(payload.get("source") or "").strip() or None
    if not requested_quest_id or requested_quest_id == REACTIONS_TARGET:
        source = source or (requested_quest_id or "unlinked")
        quest_id = INBOX_QUEST
    else:
        quest_id = requested_quest_id
    active_watch = QUESTS_DIR / "active" / quest_id / "watch.json"
    if not active_watch.exists():
        print(f"error:approval quest is not active:{quest_id}", file=sys.stderr)
        raise SystemExit(2)
    target     = payload.get("target", {})
    channel_id = target.get("channel_id")
    thread_ts  = target.get("thread_ts")

    def _write(data):
        data.setdefault("items", [])
        # Inbox can receive distinct review requests for the same thread from different
        # producers. Ordinary quest dedup remains unchanged.
        origin = source if quest_id == INBOX_QUEST else quest_id
        duplicate = any(
            i for i in data.get("items", [])
            if (i.get("quest_id") or "") == quest_id
            and ((i.get("source") or "") if quest_id == INBOX_QUEST else quest_id) == (origin or "")
            and i.get("status") == "pending_review"
            and i.get("target", {}).get("channel_id") == channel_id
            and i.get("target", {}).get("thread_ts") == thread_ts
        )
        if duplicate:
            return approval_store.NO_WRITE

        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id":           _new_id(data),
            "quest_id":     quest_id,
            "quest_title":  "Inbox" if quest_id == INBOX_QUEST else (payload.get("quest_title") or quest_id),
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
        data["items"].append(item)
        return item["id"]

    new_id = approval_store.mutate_queue(_write)
    if new_id is approval_store.NO_WRITE:
        return

    # Arm the tracking watch OUTSIDE the approvals lock (different file, its own
    # flock). Coupled here so an approval can never be created without its watch.
    if new_id:
        cmd_arm(new_id, emit=False)
        print(new_id)


def cmd_arm(approval_id: str, emit: bool = True) -> int:
    """Ensure a non-terminal approval has a dispatch watch.

    This is the shared re-arm boundary for dashboard transitions.
    """
    item = next((i for i in approval_store.read_queue().get("items", [])
                 if i.get("id") == approval_id), None)
    if item is None:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        return 1
    if item.get("status") in ("executed", "cancelled"):
        if emit:
            print("not-needed")
        return 0

    quest_id = str(item.get("quest_id") or "").strip()
    active_watch = QUESTS_DIR / "active" / quest_id / "watch.json"
    if not quest_id or not active_watch.exists():
        _flag_unarmed(approval_id, "approval quest is not active")
        print("error:approval_quest_not_active", file=sys.stderr)
        return 3
    if not _arm_approval_watch(quest_id, approval_id):
        return 3
    approval_store.mutate_item(approval_id, lambda _current: {
        "watch_armed": True,
        "watch_arm_error": None,
    })
    if emit:
        print("armed")
    return 0


def cmd_ensure_inbox() -> int:
    print(_ensure_inbox())
    return 0


def cmd_migrate_inbox() -> int:
    """One-time migration from blank/reaction/legacy-host approvals into Inbox."""
    _ensure_inbox()

    def _migrate(data):
        migrated = []
        for item in data.get("items", []):
            quest_id = str(item.get("quest_id") or "").strip()
            legacy_executor = str(item.get("executor_quest_id") or "").strip()
            if (quest_id not in ("", REACTIONS_TARGET, LEGACY_HOST_QUEST)
                    and legacy_executor != LEGACY_HOST_QUEST):
                continue
            item["source"] = item.get("source") or (quest_id if quest_id == REACTIONS_TARGET else "unlinked")
            item["quest_id"] = INBOX_QUEST
            item["quest_title"] = "Inbox"
            item.pop("executor_quest_id", None)
            migrated.append(item.get("id"))
        return [x for x in migrated if x]

    migrated_ids = approval_store.mutate_queue(_migrate)
    armed = failed = 0
    for approval_id in migrated_ids:
        item = next((i for i in approval_store.read_queue().get("items", [])
                     if i.get("id") == approval_id), {})
        if item.get("status") in ("executed", "cancelled"):
            continue
        if cmd_arm(approval_id, emit=False) == 0:
            armed += 1
        else:
            failed += 1

    legacy_dir = QUESTS_DIR / "active" / LEGACY_HOST_QUEST
    archived = False
    if legacy_dir.exists() and failed == 0:
        archive_root = QUESTS_DIR / "archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        destination = archive_root / LEGACY_HOST_QUEST
        if destination.exists():
            destination = archive_root / f"{LEGACY_HOST_QUEST}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        try:
            meta_path = legacy_dir / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            meta["status"] = "archived"
            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
            os.replace(legacy_dir, destination)
            archived = True
        except OSError as exc:
            print(f"warn:legacy_host_archive_failed:{exc}", file=sys.stderr)

    print(json.dumps({"migrated": len(migrated_ids), "armed": armed,
                      "failed": failed, "legacy_host_archived": archived}))
    return 0 if failed == 0 else 3


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
    if not (QUESTS_DIR / "active" / quest_id / "watch.json").exists():
        print(f"error:approval quest is not active:{quest_id}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    def _enqueue(data):
        data.setdefault("items", [])
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
        data["items"].append(item)
        return item

    item = approval_store.mutate_queue(_enqueue)

    print(json.dumps({"approval_id": item["id"], "queued": True}))
    return 0


def cmd_arm_pending_instructions() -> int:
    """Arm queued manual instructions while the caller owns the triage lock."""
    data = approval_store.read_queue()
    pending = [
        (i.get("id"), i.get("quest_id"))
        for i in data.get("items", [])
        if i.get("action_type") == "manual_instruction"
        and i.get("status") == "reviewed"
        and i.get("watch_armed") is not True
    ]

    armed_count = cancelled_count = 0
    for approval_id, quest_id in pending:
        active_watch = QUESTS_DIR / "active" / str(quest_id) / "watch.json"
        armed = active_watch.exists() and _arm_approval_watch(str(quest_id), str(approval_id))
        def _update(item):
            nonlocal armed_count, cancelled_count
            if item.get("action_type") != "manual_instruction" or item.get("status") != "reviewed":
                return approval_store.NO_WRITE
            if armed:
                armed_count += 1
                return {
                    "watch_armed": True,
                    "watch_arm_pending": None,
                    "watch_arm_error": None,
                }
            cancelled_count += 1
            return approval_state.apply_transition(item, "auto_cancel", {
                "reason": "quest is no longer active or dispatch watch could not be armed",
            }, datetime.now(timezone.utc))

        approval_store.mutate_item(approval_id, _update)
    # Creation and dashboard review normally arm synchronously. Retry the explicit
    # watch_armed=false cases here so a transient filesystem/subprocess failure cannot
    # leave an approved action stranded forever. Missing flags are legacy successes,
    # not evidence of failure, so they are deliberately excluded.
    retry_ids = [
        i.get("id") for i in approval_store.read_queue().get("items", [])
        if i.get("action_type") != "manual_instruction"
        and i.get("status") not in ("executed", "cancelled")
        and i.get("watch_armed") is False
        and i.get("id")
    ]
    rearmed_count = retry_failed_count = 0
    for approval_id in retry_ids:
        if cmd_arm(str(approval_id), emit=False) == 0:
            rearmed_count += 1
        else:
            retry_failed_count += 1

    print(json.dumps({"pending": len(pending), "armed": armed_count,
                      "cancelled": cancelled_count, "rearmed": rearmed_count,
                      "retry_failed": retry_failed_count}))
    return 0 if cancelled_count == 0 and retry_failed_count == 0 else 3


def cmd_start(approval_id: str):
    item = approval_store.mutate_item(
        approval_id,
        lambda current: approval_state.apply_transition(current, "start", {}, datetime.now(timezone.utc)),
    )
    if item is approval_store.NOT_FOUND:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        sys.exit(1)
    if item is approval_state.ILLEGAL:
        current = next((i for i in approval_store.read_queue().get("items", []) if i.get("id") == approval_id), None)
        print(f"skip:{(current or {}).get('status', 'unknown')}")
        return
    print("ok")


def cmd_answer(approval_id: str, payload_json: str):
    payload = json.loads(payload_json)
    item = approval_store.mutate_item(
        approval_id,
        lambda current: approval_state.apply_transition(current, "answer", payload, datetime.now(timezone.utc)),
    )
    if item is approval_store.NOT_FOUND:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        sys.exit(1)
    if item is approval_state.ILLEGAL:
        current = next((i for i in approval_store.read_queue().get("items", []) if i.get("id") == approval_id), None)
        print(f"skip:{(current or {}).get('status', 'unknown')}")
        return
    print("ok")


def cmd_done(approval_id: str, response_ts: str | None = None, worker_reply: str | None = None):
    item = approval_store.mutate_item(
        approval_id,
        lambda current: approval_state.apply_transition(
            current,
            "done",
            {"response_ts": response_ts, "worker_reply": worker_reply},
            datetime.now(timezone.utc),
        ),
    )
    if item is approval_store.NOT_FOUND:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        sys.exit(1)
    if item is approval_state.ILLEGAL:
        current = next((i for i in approval_store.read_queue().get("items", []) if i.get("id") == approval_id), None)
        print(f"skip:{(current or {}).get('status', 'unknown')}")
        return
    print("ok")


def cmd_abandon(approval_id: str, reason: str):
    reason = reason.strip()
    if not reason:
        print("error:reason_required", file=sys.stderr)
        sys.exit(1)
    try:
        item = approval_store.mutate_item(
            approval_id,
            lambda current: approval_state.apply_transition(
                current, "abandon", {"reason": reason}, datetime.now(timezone.utc)
            ),
        )
    except approval_state.InvalidPayload as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if item is approval_store.NOT_FOUND:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        sys.exit(1)
    if item is approval_state.ILLEGAL:
        current = next((i for i in approval_store.read_queue().get("items", []) if i.get("id") == approval_id), None)
        print(f"skip:{(current or {}).get('status', 'unknown')}")
        return
    print("ok")


def cmd_fail(approval_id: str, reason: str):
    reason = reason.strip()[:1000]
    if not reason:
        print("error:reason_required", file=sys.stderr)
        sys.exit(1)
    try:
        item = approval_store.mutate_item(
            approval_id,
            lambda current: approval_state.apply_transition(
                current, "fail", {"reason": reason}, datetime.now(timezone.utc)
            ),
        )
    except approval_state.InvalidPayload as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if item is approval_store.NOT_FOUND:
        print(f"error:not_found:{approval_id}", file=sys.stderr)
        sys.exit(1)
    if item is approval_state.ILLEGAL:
        current = next((i for i in approval_store.read_queue().get("items", [])
                        if i.get("id") == approval_id), None)
        print(f"skip:{(current or {}).get('status', 'unknown')}")
        return
    print("ok")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "write":
        cmd_write(sys.argv[2])
    elif cmd == "arm":
        sys.exit(cmd_arm(sys.argv[2]))
    elif cmd == "ensure-inbox":
        sys.exit(cmd_ensure_inbox())
    elif cmd == "migrate-inbox":
        sys.exit(cmd_migrate_inbox())
    elif cmd == "enqueue-instruction":
        sys.exit(cmd_enqueue_instruction(sys.argv[2]))
    elif cmd == "arm-pending-instructions":
        sys.exit(cmd_arm_pending_instructions())
    elif cmd == "start":
        cmd_start(sys.argv[2])
    elif cmd == "answer":
        cmd_answer(sys.argv[2], sys.argv[3])
    elif cmd == "done":
        cmd_done(
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else None,
            sys.argv[4] if len(sys.argv) > 4 else None,
        )
    elif cmd == "abandon":
        cmd_abandon(sys.argv[2], sys.argv[3])
    elif cmd == "fail":
        cmd_fail(sys.argv[2], sys.argv[3])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
