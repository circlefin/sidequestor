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

"""Inspect/apply an own-message expansion bound to a dispatched expand reaction.

Read a JSON payload from stdin (or one argument). Fields: action=inspect|apply,
run_id, item_id, channel_id, message_ts, optional thread_ts. Apply additionally
requires expected_text, expected_revision (from inspect), and message.

An edit receipt is separate from the reaction acknowledgement: editing is only
one step of expansion, which may also require actions or a verified quest handoff.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
import urllib.parse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import client
from inbox import write_json
from reaction_config import load_reaction_emojis


class Blocked(ValueError):
    pass


class Rejected(Blocked):
    """Slack explicitly rejected the operation; no uncertain write occurred."""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def persist(path, data):
    write_json(path, data)
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def api(method, parameters=None):
    parameters = parameters or {}
    if method == "chat.update":
        status, body = client.slack_request(f"{client.SLACK_WEB_URL}/{method}", "POST",
                                           {"Content-Type": "application/json; charset=utf-8"},
                                           json.dumps(parameters))
    else:
        query = urllib.parse.urlencode(parameters)
        status, body = client.slack_request(f"{client.SLACK_WEB_URL}/{method}?{query}")
    try:
        response = json.loads(body)
    except (TypeError, ValueError):
        raise Blocked("Slack returned an unreadable response; reconcile before retrying.")
    if not isinstance(response, dict) or status != 200 or response.get("ok") is not True:
        # Do not echo response bodies containing message text or credentials.
        error = response.get("error", "request_failed") if isinstance(response, dict) else "request_failed"
        definitive = {"cant_update_message", "message_not_found", "missing_scope", "not_authed",
                      "invalid_auth", "token_revoked", "token_expired", "is_archived",
                      "edit_window_closed", "no_text", "msg_too_long", "channel_not_found", "ratelimited"}
        if method == "chat.update" and (status not in (200, 429) or error not in definitive):
            raise Blocked(f"Slack {method} outcome is uncertain; reconcile before retrying.")
        raise Rejected(f"Slack {method} failed ({error}).")
    return response


def validate(root, payload, environment):
    run_id = payload.get("run_id", "")
    channel = payload.get("channel_id", "")
    ts = payload.get("message_ts", "")
    thread = payload.get("thread_ts") or ts
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or not re.fullmatch(r"[CDG][A-Z0-9]+", channel):
        raise Blocked("Invalid run or channel.")
    if not re.fullmatch(r"\d+\.\d+", ts) or not re.fullmatch(r"\d+\.\d+", thread):
        raise Blocked("Invalid message timestamp.")
    expected = f"{load_reaction_emojis(environment)['expand']}:{ts}"
    if payload.get("item_id") != expected:
        raise Blocked("Only the dispatched expand reaction authorizes this edit.")
    if environment.get("SIDEQUESTOR_DISPATCH_TARGET") == "quest-inbox":
        queue = json.loads((root / "state/pending-approvals.json").read_text())
        approval = next((a for a in queue.get("items", []) if isinstance(a, dict) and
                         a.get("id") == payload.get("approval_id")), {})
        target = approval.get("target") or {}
        expires = datetime.fromisoformat(str(approval.get("lease_expires_at", "")).replace("Z", "+00:00"))
        recovering = (approval.get("quest_id") == "quest-inbox" and approval.get("status") == "executing" and
                      expires > datetime.now(timezone.utc) and target.get("action") == "reaction_recovery" and
                      target.get("system_key") == "reaction:" + expected and target.get("channel_id") == channel and
                      target.get("message_ts") == ts)
        if not recovering:
            raise Blocked("Expansion recovery requires a live claimed review for this exact message.")
        return expected, channel, ts, thread, True
    if environment.get("SIDEQUESTOR_DISPATCH_TARGET") != "reactions":
        raise Blocked("Expansion requires an active reactions dispatch or claimed Inbox recovery.")
    manifest = json.loads((root / 'state/triage' / f'dispatch-{run_id}.json').read_text())
    items = manifest.get("items", [])
    item = next((item for item in items if isinstance(item, dict) and item.get("item_id") == expected), None)
    if (manifest.get("kind") != "reactions" or manifest.get("target") != "reactions" or
            not item or item.get("channel_id") != channel or item.get("acked_utc")):
        raise Blocked("Edit coordinates do not match an open reaction item. Locate it in the ack ledger first.")
    return expected, channel, ts, thread, False


def read_own_message(channel, ts, thread):
    identity = api("auth.test")
    message = None
    cursor = ""
    seen = set()
    for _ in range(10):
        args = {"channel": channel, "ts": thread, "oldest": ts, "latest": ts,
                "inclusive": "true", "limit": 100}
        if cursor:
            args["cursor"] = cursor
        response = api("conversations.replies", args)
        message = next((m for m in response.get("messages", []) if isinstance(m, dict) and m.get("ts") == ts), None)
        if message:
            break
        cursor = (response.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    if (not identity.get("user_id") or not message or message.get("user") != identity["user_id"] or
            message.get("bot_id") or message.get("subtype") not in (None, "file_share")):
        raise Blocked("Expand in place can edit only a normal message authored by your authenticated Slack user.")
    if not isinstance(message.get("text"), str):
        raise Blocked("Slack message text is unavailable.")
    return message


def revision(message):
    edited = message.get("edited") or {}
    return str(edited.get("ts", ""))


def expand(root, payload, environment):
    iid, channel, ts, thread, recovering = validate(root, payload, environment)
    path = root / "state/triage/reaction-edits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {"items": {}}
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise Blocked("Expansion receipts are unreadable; editing is held.")
        old = data["items"].get(iid)
        if old is not None and (not isinstance(old, dict) or old.get("channel_id") != channel):
            raise Blocked("Expansion receipt does not match this message.")
        current = read_own_message(channel, ts, thread)
        current_hash = digest(current["text"])
        if payload.get("action") == "resolve":
            if (not recovering or not old or old.get("phase") != "prepared" or
                    payload.get("resolution") not in ("accept_current", "abandon")):
                raise Blocked("Resolving an uncertain edit requires a claimed Inbox review and explicit resolution.")
            if time.time() - old.get("prepared_at", 0) < 60:
                raise Blocked("Wait for the original request to drain before resolving its outcome.")
            accept = payload["resolution"] == "accept_current"
            edited = accept and current_hash != old.get("before_hash")
            phase = "applied" if edited else "accepted_unchanged" if accept else "abandoned"
            old.update(phase=phase, after_hash=current_hash)
            persist(path, data)
            return {"resolved": True, "edited": edited, "already_edited": edited,
                    "accepted_unchanged": accept and not edited, "text": current["text"]}
        if old and old.get("phase") == "accepted_unchanged":
            return {"resolved": True, "edited": False, "accepted_unchanged": True, "text": current["text"]}
        if old and old.get("phase") == "abandoned":
            raise Blocked("This expansion was abandoned during review; do not replay it.")
        if old and old.get("phase") in ("prepared", "applied"):
            if current_hash == old.get("after_hash"):
                old["phase"] = "applied"
                persist(path, data)
                return {"edited": True, "already_edited": True, "text": current["text"]}
            if old.get("phase") == "applied":
                return {"edited": True, "already_edited": True, "changed_after_edit": True,
                        "text": current["text"]}
            if not (recovering and current_hash == old.get("before_hash") and
                    revision(current) == old.get("before_revision") and time.time() - old.get("prepared_at", time.time()) >= 60):
                raise Blocked("A previous edit is uncertain. Reconcile through the Inbox reaction recovery review before retrying.")
            # A separately reviewed recovery may retry after the original request
            # has drained and the exact original text is still present.
        if payload.get("action") == "inspect":
            return {"edited": False, "text": current["text"], "revision": revision(current)}
        if payload.get("action") != "apply":
            raise Blocked("Action must be inspect or apply.")
        replacement = payload.get("message")
        if not isinstance(replacement, str) or not replacement.strip() or len(replacement) > 40000:
            raise Blocked("Expanded message must contain between 1 and 40000 characters.")
        if (payload.get("expected_text") != current["text"] or
                payload.get("expected_revision") != revision(current)):
            raise Blocked("Message changed while expansion was prepared; no edit was sent.")
        if replacement == current["text"]:
            return {"edited": False, "nothing_to_do": True, "text": current["text"]}
        # Persist intent before the external write. A crash/timeout can then be
        # reconciled by reading Slack, rather than blindly sending a second edit.
        data["items"][iid] = {"channel_id": channel, "before_hash": current_hash,
                              "after_hash": digest(replacement), "before_revision": revision(current),
                              "phase": "prepared", "prepared_at": time.time()}
        persist(path, data)
        try:
            response = api("chat.update", {"channel": channel, "ts": ts, "text": replacement,
                                "link_names": False, "parse": "none"})
        except Rejected:
            data["items"][iid]["phase"] = "rejected"
            persist(path, data)
            raise
        canonical = response.get("text") or (response.get("message") or {}).get("text")
        if isinstance(canonical, str):
            data["items"][iid]["after_hash"] = digest(canonical)
        data["items"][iid]["phase"] = "applied"
        persist(path, data)
        return {"edited": True, "already_edited": False, "text": replacement}


def main():
    try:
        payload = json.loads(sys.argv[1] if len(sys.argv) == 2 else sys.stdin.read())
        root = Path(os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ["YAAS_WORKSPACE"])
        print(json.dumps(expand(root, payload, os.environ)))
        return 0
    except (ValueError, OSError, KeyError, TypeError, client.SlackCredentialError) as exc:
        print(f"Expansion blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
