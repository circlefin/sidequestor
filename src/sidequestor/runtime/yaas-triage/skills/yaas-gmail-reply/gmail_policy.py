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

"""Send authorization shared by gmail-reply.py and send_fresh.py.

Same rule as slack-send.py, slack-file.py and gdoc-comment.py: a real send needs an active
quest matching the dispatch target, and either `allow_send: true` or a claimed approval whose
target and message_text match this exact email. Drafts are not gated.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RUNTIME_DIR))

import approval_store


def _repo_root(start):
    override = os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    path = Path(start).resolve()
    for directory in (path, *path.parents):
        if (directory / "yaas-triage").is_dir():
            return directory
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT = _repo_root(__file__)


def attachment_record(path):
    """Pin an attachment by content, so different bytes cannot ride on an approval."""
    # shortcut: gws re-reads the path after this hash; a swap in that window is not caught.
    data = Path(path).read_bytes()
    return {"path": str(Path(path).expanduser().resolve()), "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def approval_spec(target, message_text):
    return {"action_type": "remote_request", "target": target, "message_text": message_text}


def _valid_quest_id(value):
    return bool(value) and "/" not in value and value not in (".", "..") and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def _claimed_approval(quest_id, approval_id, target, message_text):
    if not approval_id:
        return False
    try:
        item = next(
            (row for row in approval_store.read_queue().get("items", [])
             if isinstance(row, dict) and row.get("id") == approval_id),
            None,
        )
        if not item or item.get("quest_id") != quest_id:
            return False
        if item.get("status") != "executing" or item.get("action_type") != "remote_request":
            return False
        if item.get("target") != target or (item.get("message_text") or "") != message_text:
            return False
        expiry = datetime.fromisoformat(str(item["lease_expires_at"]).replace("Z", "+00:00"))
        return expiry.timestamp() >= time.time()
    except Exception:
        # Approval-state failures must never weaken the send guard.
        return False


def send_policy_reason(quest_id, approval_id, target, message_text):
    """Fail-closed reason to refuse a real send, or None when it is authorized."""
    dispatch = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = (quest_id or "").strip()
    if dispatch == "reactions":
        return "the reactions dispatch cannot send email"
    if dispatch and dispatch != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {dispatch!r}"
    if not quest_id:
        return "quest_id is required for --send; use --draft otherwise"
    if not _valid_quest_id(quest_id):
        return f"invalid quest id {quest_id!r}"
    quest = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not quest.is_dir():
        return f"quest {quest_id} is not in state/quests/active; it may not send"
    try:
        meta = json.loads((quest / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid quest policy file")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    if not meta.get("allow_send") and not _claimed_approval(
            quest_id, approval_id, target, message_text):
        return (f"quest {quest_id} has allow_send false and no claimed approval for this exact "
                "email; run with --approval-spec and queue it for review")
    return None
