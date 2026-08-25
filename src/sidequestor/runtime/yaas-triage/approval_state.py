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

"""Pure approval state-machine rules. No I/O, locks, or subprocesses."""

import os
from datetime import datetime, timedelta, timezone


LEASE_MINUTES = 45


def configure(environment=None):
    """Resolve the approval lease from canonical or legacy environment names."""
    global LEASE_MINUTES
    environment = os.environ if environment is None else environment
    raw = (environment.get("SIDEQUESTOR_APPROVAL_LEASE_MIN")
           or environment.get("YAAS_APPROVAL_LEASE_MIN")
           or "45")
    try:
        lease = int(raw)
    except (TypeError, ValueError):
        lease = 45
    LEASE_MINUTES = lease if lease > 0 else 45


configure()

ILLEGAL = object()

HTTP_ACTIONS = ("review", "revise", "edit", "cancel", "undo", "reclaim")
WORKER_ONLY_ACTIONS = ("start", "answer", "done", "fail", "abandon", "auto_cancel")

TRANSITIONS = {
    ("pending_review", "review"): {"http": True},
    ("needs_reply", "review"): {"http": True},
    ("pending_review", "revise"): {"http": True},
    ("needs_reply", "revise"): {"http": True},
    ("pending_review", "cancel"): {"http": True},
    ("needs_reply", "cancel"): {"http": True},
    ("reviewed", "cancel"): {"http": True},
    ("reviewed", "edit"): {"http": True},
    ("reviewed", "undo"): {"http": True},
    ("cancelled", "undo"): {"http": True},
    ("executing", "reclaim"): {"http": True},
    # `start` is legal only from `reviewed`: a worker must not move an unreviewed
    # item into `executing`, so the human-approval gate is structural, not merely
    # a convention enforced by the checker that decides what is dispatchable.
    ("reviewed", "start"): {"http": False},
    ("needs_reply", "answer"): {"http": False},
    ("executing", "done"): {"http": False},
    ("reviewed", "done"): {"http": False},
    ("reviewed", "fail"): {"http": False},
    ("needs_reply", "fail"): {"http": False},
    ("executing", "fail"): {"http": False},
    ("executing", "abandon"): {"http": False},
    ("reviewed", "abandon"): {"http": False},
    ("reviewed", "auto_cancel"): {"http": False},
}


class InvalidPayload(ValueError):
    pass


def _clear_processing_error(updates: dict) -> dict:
    updates.update({
        "processing_error": None,
        "processing_error_at": None,
        "failed_from_status": None,
    })
    return updates


def _as_dt(now):
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(now):
    return _as_dt(now).isoformat()


def _lease_expired(item, now) -> bool:
    lease = item.get("lease_expires_at")
    if not lease:
        return False
    try:
        return _as_dt(lease) < _as_dt(now)
    except (TypeError, ValueError):
        return False


def is_stalled(item, now) -> bool:
    return item.get("status") == "executing" and _lease_expired(item, now)


def available_actions(item, now) -> list[str]:
    status = item.get("status")
    actions = []
    for action in HTTP_ACTIONS:
        meta = TRANSITIONS.get((status, action))
        if not meta or not meta.get("http"):
            continue
        if action == "reclaim" and not _lease_expired(item, now):
            continue
        actions.append(action)
    return actions


def apply_transition(item, action, payload, now):
    payload = payload or {}
    status = item.get("status")
    meta = TRANSITIONS.get((status, action))
    if not meta:
        return ILLEGAL
    if action in ("done", "abandon") and status == "reviewed" and not item.get("needs_reconcile"):
        return ILLEGAL

    now_dt = _as_dt(now)
    now_iso = now_dt.isoformat()

    if action == "review":
        # Approve is a prompt, not a rubber stamp. Whatever the reviewer typed is the
        # governing instruction for the worker; `message_text` is only the default action
        # when no instruction was given. Approve is always terminal: the worker executes
        # the instruction and closes the item. `revise` is the button that iterates.
        note = str(payload.get("review_note") or "").strip()
        edited = "message_text" in payload
        # A bare Approve means "send the draft as it stands", so any instruction left
        # over from an earlier Request change must be cleared rather than inherited.
        updates = _clear_processing_error({
            "status": "reviewed",
            "reviewed_at": now_iso,
            "review_note": note or None,
            "asked_at": now_iso if note else None,
        })
        if edited:
            new_text = str(payload.get("message_text") or "").strip()
            if not new_text:
                raise InvalidPayload("message_text required")
            updates["message_text"] = new_text
            updates["human_edited"] = True
        return updates

    if action == "revise":
        note = str(payload.get("review_note") or "").strip()
        if not note:
            raise InvalidPayload("revision requires an instruction note")
        updates = _clear_processing_error({
            "status": "needs_reply", "review_note": note, "asked_at": now_iso,
        })
        if "message_text" in payload:
            new_text = str(payload.get("message_text") or "").strip()
            if not new_text:
                raise InvalidPayload("message_text required")
            updates["message_text"] = new_text
            updates["human_edited"] = True
        return updates

    if action == "cancel":
        return {"status": "cancelled", "cancelled_at": now_iso}

    if action == "edit":
        new_text = str(payload.get("message_text") or "").strip()
        if not new_text:
            raise InvalidPayload("message_text required")
        return _clear_processing_error({"message_text": new_text, "human_edited": True})

    if action == "undo":
        return _clear_processing_error({
            "status": "pending_review",
            "reviewed_at": None,
            "cancelled_at": None,
            "review_note": None,
            "asked_at": None,
        })

    if action == "reclaim":
        if not _lease_expired(item, now_dt):
            return ILLEGAL
        return _clear_processing_error({
            "status": "pending_review",
            "lease_expires_at": None,
            "needs_reconcile": True,
        })

    if action == "start":
        return {
            "status": "executing",
            "executing_at": now_iso,
            "lease_expires_at": (now_dt + timedelta(minutes=LEASE_MINUTES)).isoformat(),
        }

    if action == "fail":
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise InvalidPayload("error:reason_required")
        return {
            "status": "pending_review",
            "processing_error": reason[:1000],
            "processing_error_at": now_iso,
            "failed_from_status": status,
            "lease_expires_at": None,
            "needs_reconcile": True if status == "executing" else None,
        }

    if action == "answer":
        reply = str(payload.get("worker_reply") or "")
        hist = list(item.get("review_history") or [])
        if item.get("review_note"):
            hist.append({"from": "reviewer", "note": item["review_note"], "at": item.get("asked_at")})
        hist.append({"from": "worker", "reply": reply, "at": now_iso})
        updates = {
            "worker_reply": reply,
            "review_history": hist,
            "status": "pending_review",
            "answered_at": now_iso,
            "review_note": None,
            "asked_at": None,
        }
        if payload.get("message_text"):
            updates["message_text"] = payload["message_text"]
            updates["revised_by_worker"] = True
        return updates

    if action == "done":
        updates = {
            "status": "executed",
            "sent_at": now_iso,
            "needs_reconcile": None,
        }
        # An instruction given at approve time is consumed here, not by `answer`, so it
        # has to land in the trail on the way out or it disappears from the conversation.
        if item.get("review_note"):
            hist = list(item.get("review_history") or [])
            hist.append({"from": "reviewer", "note": item["review_note"], "at": item.get("asked_at")})
            updates["review_history"] = hist
            updates["review_note"] = None
        if payload.get("worker_reply"):
            hist = list(updates.get("review_history") or item.get("review_history") or [])
            hist.append({"from": "worker", "reply": str(payload["worker_reply"]), "at": now_iso})
            updates["review_history"] = hist
            updates["worker_reply"] = str(payload["worker_reply"])
        response = payload.get("response_ts")
        if response:
            if str(response).startswith("https://"):
                updates["result_url"] = response
            else:
                updates["response_ts"] = response
        return updates

    if action == "abandon":
        if item.get("action_type") != "manual_instruction":
            raise InvalidPayload("error:not_manual_instruction")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise InvalidPayload("error:reason_required")
        return {
            "status": "cancelled",
            "cancelled_at": now_iso,
            "cancel_reason": reason[:1000],
        }

    if action == "auto_cancel":
        return {
            "status": "cancelled",
            "cancelled_at": now_iso,
            "cancel_reason": str(payload.get("reason") or "")[:1000],
        }

    return ILLEGAL
