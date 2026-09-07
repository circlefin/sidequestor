#!/usr/bin/env python3
"""Perform an X action as the authorized user and log quest-owned writes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import mimetypes
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

SURFACES_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SURFACES_DIR.parent
sys.path.insert(0, str(SURFACES_DIR))
sys.path.insert(0, str(RUNTIME_DIR))

import approval_store
from timeline_io import append_timeline, quest_dir, utc_now
from x_credentials import get_access_token, user_identity

_SPEC = importlib.util.spec_from_file_location("sidequestor_x_call", SURFACES_DIR / "x-call.py")
_X_CALL_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_X_CALL_MODULE)
x_call = _X_CALL_MODULE.call

MAX_POST_LENGTH = 280
MAX_THREAD_LENGTH = 25
MAX_MEDIA_BYTES = 15 * 1024 * 1024


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


def _valid_quest_id(value):
    return bool(value) and "/" not in value and value not in (".", "..") and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def _approval_target(payload):
    target = {"surface": "x", "action": str(payload.get("action") or "")}
    for key in ("post_id", "user_id", "participant_id", "conversation_id", "quote_post_id",
                "media_id", "file", "media_category"):
        if payload.get(key) not in (None, ""):
            target[key] = str(payload[key])
    if payload.get("media_ids") is not None:
        target["media_ids"] = [str(value) for value in payload["media_ids"]]
    return target


def _claimed_approval(payload):
    approval_id = str(payload.get("approval_id") or "")
    quest_id = str(payload.get("quest_id") or "")
    if not approval_id or not quest_id:
        return False
    try:
        item = next(
            (row for row in approval_store.read_queue().get("items", [])
             if isinstance(row, dict) and row.get("id") == approval_id),
            None,
        )
        if not item or item.get("quest_id") != quest_id or item.get("status") != "executing":
            return False
        if item.get("action_type") != "remote_request" or item.get("target") != _approval_target(payload):
            return False
        expected_text = payload.get("text")
        if expected_text is None and payload.get("messages") is not None:
            expected_text = "\n\n".join(str(value) for value in payload["messages"])
        if expected_text is not None and item.get("message_text") != expected_text:
            return False
        expiry = datetime.fromisoformat(str(item["lease_expires_at"]).replace("Z", "+00:00"))
        return expiry.timestamp() >= time.time()
    except Exception:
        return False


def send_policy_reason(payload):
    target = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = str(payload.get("quest_id") or "").strip()
    if target and target != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {target!r}"
    if not quest_id:
        return "quest_id is required for a dispatched X action" if target else None
    if not _valid_quest_id(quest_id):
        return f"invalid quest id {quest_id!r}"
    active = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not active.is_dir():
        return f"quest {quest_id} is not in state/quests/active; it may not write to X"
    try:
        meta = json.loads((active / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid meta.json")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    if not meta.get("allow_send") and not _claimed_approval(payload):
        return f"quest {quest_id} has allow_send false and no claimed X approval for this action"
    if target and not str(payload.get("idempotency_key") or "").strip():
        return "idempotency_key is required for a dispatched X action"
    return None


def _required(payload, key):
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required for action {payload.get('action')}")
    return value


def _post_body(payload, reply_to=None):
    text = _required(payload, "text")
    if len(text) > MAX_POST_LENGTH:
        raise ValueError(f"text exceeds X's {MAX_POST_LENGTH}-character limit")
    body = {"text": text}
    reply_id = reply_to or payload.get("post_id") if payload.get("action") == "reply" else reply_to
    if reply_id:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_id)}
    if payload.get("quote_post_id"):
        body["quote_tweet_id"] = str(payload["quote_post_id"])
    media_ids = payload.get("media_ids")
    if media_ids:
        if not isinstance(media_ids, list) or not all(str(value) for value in media_ids):
            raise ValueError("media_ids must be a non-empty list")
        body["media"] = {"media_ids": [str(value) for value in media_ids]}
    return body


def _result(value, action):
    data = value.get("data") if isinstance(value, dict) else None
    data = data if isinstance(data, dict) else {}
    identifier = data.get("id") or data.get("dm_event_id") or data.get("media_id")
    output = {"action": action, "data": data}
    if identifier is not None:
        output["id"] = str(identifier)
    return output


def _upload_media(payload, credential_id):
    path = Path(_required(payload, "file")).expanduser()
    data = path.read_bytes()
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError(f"media file must contain 1 to {MAX_MEDIA_BYTES} bytes")
    media_type = str(payload.get("media_type") or mimetypes.guess_type(path.name)[0] or "")
    if not media_type.startswith(("image/", "video/", "image/gif")):
        raise ValueError("media_type must be an image, GIF, or video MIME type")
    boundary = f"sidequestor-{uuid.uuid4().hex}"
    parts = []
    fields = {"media_category": str(payload.get("media_category") or "tweet_image")}
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"{path.name}\"\r\nContent-Type: {media_type}\r\n\r\n".encode()
        + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        "https://api.x.com/2/media/upload",
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {get_access_token(credential_id)}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        value = json.loads(response.read().decode())
    return _result(value, "upload_media")


def execute(payload):
    action = str(payload.get("action") or "").strip()
    credential_id = str(payload.get("credential_id") or "default")
    identity = user_identity(credential_id)
    owner_id = _required(identity, "id")
    if action in ("post", "reply"):
        return _result(x_call("POST", "/2/tweets", {}, _post_body(payload), credential_id), action)
    if action == "thread":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_THREAD_LENGTH:
            raise ValueError(f"messages must contain 1 to {MAX_THREAD_LENGTH} posts")
        ids = []
        parent = str(payload.get("post_id") or "") or None
        for message in messages:
            body = _post_body({"action": "post", "text": message}, parent)
            value = _result(x_call("POST", "/2/tweets", {}, body, credential_id), "post")
            if not value.get("id"):
                raise RuntimeError("X did not return an id for a thread post")
            parent = value["id"]
            ids.append(parent)
        return {"action": action, "ids": ids, "id": ids[0]}
    if action == "dm":
        text = _required(payload, "text")
        body = {"text": text}
        if payload.get("media_id"):
            body["attachments"] = [{"media_id": str(payload["media_id"])}]
        if payload.get("conversation_id"):
            path = f"/2/dm_conversations/{payload['conversation_id']}/messages"
        else:
            path = f"/2/dm_conversations/with/{_required(payload, 'participant_id')}/messages"
        return _result(x_call("POST", path, {}, body, credential_id), action)
    if action == "upload_media":
        return _upload_media(payload, credential_id)

    post_id = str(payload.get("post_id") or "")
    user_id = str(payload.get("user_id") or "")
    routes = {
        "delete_post": ("DELETE", f"/2/tweets/{post_id}", None, post_id),
        "like": ("POST", f"/2/users/{owner_id}/likes", {"tweet_id": post_id}, post_id),
        "unlike": ("DELETE", f"/2/users/{owner_id}/likes/{post_id}", None, post_id),
        "repost": ("POST", f"/2/users/{owner_id}/retweets", {"tweet_id": post_id}, post_id),
        "unrepost": ("DELETE", f"/2/users/{owner_id}/retweets/{post_id}", None, post_id),
        "bookmark": ("POST", f"/2/users/{owner_id}/bookmarks", {"tweet_id": post_id}, post_id),
        "unbookmark": ("DELETE", f"/2/users/{owner_id}/bookmarks/{post_id}", None, post_id),
        "follow": ("POST", f"/2/users/{owner_id}/following", {"target_user_id": user_id}, user_id),
        "unfollow": ("DELETE", f"/2/users/{owner_id}/following/{user_id}", None, user_id),
        "mute": ("POST", f"/2/users/{owner_id}/muting", {"target_user_id": user_id}, user_id),
        "unmute": ("DELETE", f"/2/users/{owner_id}/muting/{user_id}", None, user_id),
        "block": ("POST", f"/2/users/{owner_id}/blocking", {"target_user_id": user_id}, user_id),
        "unblock": ("DELETE", f"/2/users/{owner_id}/blocking/{user_id}", None, user_id),
    }
    if action not in routes:
        raise ValueError(f"unsupported X action: {action}")
    method, path, body, required_value = routes[action]
    if not required_value:
        raise ValueError(f"{'post_id' if 'post' in action or action in ('like', 'unlike', 'repost', 'unrepost', 'bookmark', 'unbookmark') else 'user_id'} is required for action {action}")
    return _result(x_call(method, path, {}, body, credential_id), action)


def _with_idempotency(payload):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        return execute(payload)
    bound_payload = {
        name: value for name, value in payload.items()
        if name not in ("approval_id", "idempotency_key", "note")
    }
    fingerprint = hashlib.sha256(json.dumps(
        bound_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    state = REPO_ROOT / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "x-idempotency.json"
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        records = json.loads(raw) if raw else {}
        if key in records:
            record = records[key]
            if record.get("fingerprint") != fingerprint:
                raise RuntimeError(f"idempotency key {key!r} belongs to a different X action")
            if record.get("status") == "complete":
                return record["result"]
            raise RuntimeError(f"X action {key!r} has an indeterminate prior attempt; inspect X before retrying")
        records[key] = {"status": "started", "ts": utc_now(), "fingerprint": fingerprint}
        handle.seek(0)
        handle.truncate()
        json.dump(records, handle)
        handle.flush()
        os.fsync(handle.fileno())
        result = execute(payload)
        records[key] = {"status": "complete", "ts": utc_now(), "fingerprint": fingerprint,
                        "result": result}
        handle.seek(0)
        handle.truncate()
        json.dump(records, handle)
        handle.flush()
        os.fsync(handle.fileno())
        return result


def _parse_args(argv):
    if len(argv) == 1 and not argv[0].startswith("-"):
        return json.loads(argv[0])
    parser = argparse.ArgumentParser(description="Perform an X action as the authorized user.")
    parser.add_argument("action")
    parser.add_argument("--text")
    parser.add_argument("--post-id")
    parser.add_argument("--user-id")
    parser.add_argument("--participant-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--credential-id")
    parser.add_argument("--quest-id")
    parser.add_argument("--approval-id")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--file")
    parser.add_argument("--note")
    return {key: value for key, value in vars(parser.parse_args(argv)).items() if value is not None}


def main(argv=None):
    try:
        payload = _parse_args(list(sys.argv[1:] if argv is None else argv))
        reason = send_policy_reason(payload)
        if reason:
            raise ValueError(f"send denied: {reason}")
        result = _with_idempotency(payload)
        quest_id = str(payload.get("quest_id") or "")
        logged = False
        if quest_id:
            directory = quest_dir(REPO_ROOT, quest_id)
            if directory:
                entry = {
                    "ts": utc_now(), "event": "x_action_sent", "surface": "x",
                    "action": payload.get("action"), "message_text": payload.get("text", ""),
                    "result": result,
                }
                if payload.get("note"):
                    entry["note"] = payload["note"]
                append_timeline(directory, entry)
                logged = True
        result["logged"] = logged
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
