#!/usr/bin/env python3
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private-channel creation and channel management using the rotating Slack user token.

Usage: sq slack-channel create|invite|topic|purpose|approval-spec '<json>'
Creation is always private. Each command performs exactly one Slack mutation.
Exit codes: 0 success, 1 bad arguments, 2 denied/API error, 3 outcome unknown or
successful action with failed logging (inspect Slack before retry), 4 rate limited.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
import approval_store
import client
from timeline_io import append_timeline, utc_now

METHODS = {"create": "conversations.create", "invite": "conversations.invite",
           "topic": "conversations.setTopic", "purpose": "conversations.setPurpose"}


def workspace_root():
    override = os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")
    return Path(override).expanduser().resolve() if override else SCRIPT_DIR.parent.parent


def prepare(action, payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if action not in METHODS:
        raise ValueError("action must be create, invite, topic, or purpose")
    common = {"quest_id", "approval_id", "interactive", "action"}
    allowed = {"create": {"name"}, "invite": {"channel_id", "users"},
               "topic": {"channel_id", "topic"}, "purpose": {"channel_id", "purpose"}}
    unexpected = set(payload) - common - allowed[action]
    if unexpected:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unexpected))}")
    if "action" in payload and payload["action"] != action:
        raise ValueError("action does not match command")
    if "interactive" in payload and not isinstance(payload["interactive"], bool):
        raise ValueError("interactive must be a boolean")
    for key in ("quest_id", "approval_id"):
        if key in payload and not isinstance(payload[key], str):
            raise ValueError(f"{key} must be a string")
    quest_id = payload.get("quest_id", "")
    if quest_id and (quest_id in (".", "..") or "/" in quest_id or "\\" in quest_id
                     or any(ord(c) < 32 or ord(c) == 127 for c in quest_id)):
        raise ValueError("invalid quest_id")
    if action == "create":
        name = payload.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", name):
            raise ValueError("name must be 1–80 lowercase letters, numbers, hyphens or underscores")
        if not re.search(r"[a-z0-9]", name):
            raise ValueError("name must contain a letter or number")
        body = {"name": name, "is_private": True}
    else:
        channel = payload.get("channel_id")
        if not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
            raise ValueError("channel_id must be a Slack channel ID (C… or G…)")
        body = {"channel": channel}
        if action == "invite":
            users = payload.get("users")
            if (not isinstance(users, list) or not 1 <= len(users) <= 1000 or
                    any(not isinstance(u, str) or not re.fullmatch(r"[UW][A-Z0-9]{8,}", u)
                        for u in users)):
                raise ValueError("users must be a list of 1–1000 Slack user IDs")
            body["users"] = ",".join(sorted(set(users)))
        else:
            value = payload.get(action)
            if not isinstance(value, str) or len(value) > 250:
                raise ValueError(f"{action} must be a string of at most 250 characters")
            body[action] = value
    return {"action": action, "body": body, "quest_id": quest_id,
            "approval_id": payload.get("approval_id", ""),
            "interactive": payload.get("interactive", False)}


def approval_spec(job):
    body = job["body"]
    if job["action"] == "create":
        description = f"Create private Slack channel #{body['name']}"
    elif job["action"] == "invite":
        description = f"Invite {body['users']} to Slack channel {body['channel']}"
    else:
        description = f"Set Slack channel {body['channel']} {job['action']}: {body[job['action']]}"
    return {"action_type": "remote_request",
            "target": {"surface": "slack", "action": "channel_" + job["action"],
                       **job["body"]}, "message_text": description}


def claimed_approval(job):
    if not job["approval_id"]:
        return False
    try:
        spec = approval_spec(job)
        item = next((row for row in approval_store.read_queue().get("items", [])
                     if isinstance(row, dict) and row.get("id") == job["approval_id"]), None)
        expiry = datetime.fromisoformat(str((item or {}).get("lease_expires_at", "")).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return False
        return bool(item and item.get("quest_id") == job["quest_id"]
                    and item.get("status") == "executing"
                    and item.get("action_type") == spec["action_type"]
                    and item.get("target") == spec["target"]
                    and (item.get("message_text") or "") == spec["message_text"]
                    and expiry.timestamp() >= time.time())
    except Exception:
        return False


def policy_reason(job):
    dispatch = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    if os.environ.get("YAAS_FORCE_DRAFT") == "1":
        return "force-draft mode is active; channel changes have no draft state"
    if job["interactive"]:
        if dispatch or job["quest_id"] or job["approval_id"]:
            return "interactive changes require a manual session without a quest or approval"
        return None
    qid = job["quest_id"]
    if not qid or (dispatch and dispatch != qid) or dispatch == "reactions":
        return "an active quest matching the dispatch target is required"
    quest = workspace_root() / "state" / "quests" / "active" / qid
    if not quest.is_dir():
        return "quest is not active"
    try:
        meta = json.loads((quest / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid quest metadata")
    except Exception as exc:
        return f"cannot read quest policy: {exc}"
    if (meta.get("allow_send") is not True or job["approval_id"]) and not claimed_approval(job):
        return "exact claimed approval required; run approval-spec and queue it for review"
    return None


def mutate(job):
    """Never retry an ambiguous write. Shared client retries only proven auth rejection."""
    method = METHODS[job["action"]]
    try:
        status, raw = client.slack_request(f"{client.SLACK_WEB_URL}/{method}", "POST",
            {"Content-Type": "application/json; charset=utf-8"}, json.dumps(job["body"]))
    except client.SlackCredentialError as exc:
        return 2, {"error": str(exc)}
    except Exception as exc:
        return 3, {"error": f"{type(exc).__name__}: {exc}", "outcome_unknown": True}
    if status == 429:
        return 4, {"error": "ratelimited"}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if status >= 500 or not isinstance(data, dict):
        return 3, {"error": "unexpected Slack response", "outcome_unknown": True}
    if data.get("ok") is not True:
        error = data.get("error")
        if error in ("ratelimited", "rate_limited"):
            return 4, {"error": error}
        if not error or error in ("internal_error", "fatal_error", "request_timeout", "service_unavailable", "failed_for_some_users"):
            return 3, {"error": error, "outcome_unknown": True,
                       **{k: data[k] for k in ("needed", "provided", "errors") if k in data}}
        return 2, {"error": error, **{k: data[k] for k in ("needed", "provided", "errors") if k in data}}
    if status != 200:
        return 3, {"error": "unexpected Slack HTTP status", "outcome_unknown": True}
    if job["action"] == "create":
        channel = data.get("channel")
        if (not isinstance(channel, dict) or not channel.get("id")
                or channel.get("is_private") is not True):
            return 3, {"error": "cannot confirm created private channel", "outcome_unknown": True,
                       "channel_id": channel.get("id") if isinstance(channel, dict) else None}
        return 0, {"channel_id": channel["id"], "name": channel.get("name"), "is_private": True}
    return 0, {"channel_id": job["body"]["channel"]}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in (*METHODS, "approval-spec"):
        print(__doc__, file=sys.stderr)
        return 1
    try:
        payload = json.loads(argv[1])
        action = payload.get("action") if isinstance(payload, dict) and argv[0] == "approval-spec" else argv[0]
        job = prepare(action, payload)
    except (ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    if argv[0] == "approval-spec":
        print(json.dumps(approval_spec(job)))
        return 0
    reason = policy_reason(job)
    if reason:
        print(json.dumps({"error": reason, "denied": True}))
        return 2
    code, result = mutate(job)
    result["action"] = job["action"]
    if code == 0:
        result["logged"] = False
        if job["quest_id"]:
            try:
                append_timeline(workspace_root() / "state" / "quests" / "active" / job["quest_id"],
                    {"ts": utc_now(), "event": "executed", **approval_spec(job),
                     **{k: v for k, v in result.items() if k != "logged"},
                     "approval_id": job["approval_id"] or None})
                result["logged"] = True
            except Exception as exc:
                result.update({"log_error": str(exc), "remote_succeeded": True})
                code = 3
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
