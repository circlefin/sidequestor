#!/usr/bin/env python3
"""Save a Telegram cloud draft or explicitly send through the authorized user session.

Drafting is the default. Passing ``--send`` or ``"send": true`` delivers the
message and activates the send-policy and idempotency safeguards.

Usage:
    python3 yaas-triage/surfaces/telegram-send.py '<json>'
    python3 yaas-triage/surfaces/telegram-send.py --peer @name --message "hello"

<json> fields:
    peer                 (required)  dialog id, @username, or public link
    message              (required)  the verbatim draft body
    credential_id        (optional)  named Telegram credential (default "default")
    reply_to_message_id  (optional)  reply to this message id in the peer
    send                 (optional)  true to deliver; omitted/false saves a draft
    quest_id             (optional)  quest to log under
    approval_id          (optional)  claimed exact remote_request approval
    idempotency_key      (optional)  required for dispatched sends
    note                 (optional)  short human summary for the timeline `note`

Output (stdout): compact JSON, e.g.
    {"peer":"@chat","draft_saved":true,"logged":true}

Exit codes:
    0  draft saved or message sent successfully
    1  bad arguments or action denied by quest policy
    2  Telegram operation failed
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SURFACES_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SURFACES_DIR.parent
for directory in (str(SURFACES_DIR), str(RUNTIME_DIR)):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import approval_store
from slack_credentials import CredentialError
from telegram_credentials import load_bundle
from timeline_io import append_timeline, quest_dir, utc_now


def _repo_root(start):
    override = (os.environ.get("SIDEQUESTOR_WORKSPACE")
                or os.environ.get("YAAS_WORKSPACE"))
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT = _repo_root(__file__)
MAX_DRAFT_LENGTH = 4096


def _parse_args(argv):
    if not argv:
        print(__doc__)
        sys.exit(1)
    if argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if len(argv) == 1 and not argv[0].startswith("-"):
        try:
            return json.loads(argv[0])
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON argument: {exc}", file=sys.stderr)
            sys.exit(1)

    parser = argparse.ArgumentParser(add_help=True, description="Save a native Telegram draft.")
    parser.add_argument("--peer", required=True)
    parser.add_argument("--message", "--text", dest="message", required=True)
    parser.add_argument("--credential-id")
    parser.add_argument("--reply-to-message-id")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--quest-id")
    parser.add_argument("--approval-id")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--note")
    ns = parser.parse_args(argv)
    payload = {"peer": ns.peer, "message": ns.message}
    if ns.credential_id:
        payload["credential_id"] = ns.credential_id
    if ns.reply_to_message_id:
        payload["reply_to_message_id"] = ns.reply_to_message_id
    if ns.send:
        payload["send"] = True
    if ns.quest_id:
        payload["quest_id"] = ns.quest_id
    if ns.approval_id:
        payload["approval_id"] = ns.approval_id
    if ns.idempotency_key:
        payload["idempotency_key"] = ns.idempotency_key
    if ns.note:
        payload["note"] = ns.note
    return payload


def _approval_target(payload):
    target = {
        "surface": "telegram",
        "action": "send",
        "peer": str(payload.get("peer") or ""),
    }
    if payload.get("reply_to_message_id") not in (None, ""):
        target["reply_to_message_id"] = str(payload["reply_to_message_id"])
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
        if item.get("message_text") != str(payload.get("message") or ""):
            return False
        expiry = datetime.fromisoformat(str(item["lease_expires_at"]).replace("Z", "+00:00"))
        return expiry.timestamp() >= time.time()
    except Exception:
        return False


def _policy_reason(payload):
    target = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = str(payload.get("quest_id") or "").strip()
    sending = bool(payload.get("send"))

    if not quest_id:
        return "quest_id is required for a dispatched Telegram action" if target else None
    if target and target != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {target!r}"
    if "/" in quest_id or quest_id in (".", "..") or any(ord(ch) < 32 or ord(ch) == 127 for ch in quest_id):
        return f"invalid quest id '{quest_id}'"
    qdir = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not qdir.is_dir():
        return (f"quest {quest_id} is not in state/quests/active; a completed or "
                f"archived quest may not use Telegram")
    try:
        meta = json.loads((qdir / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid meta.json")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    if sending and not meta.get("allow_send") and not _claimed_approval(payload):
        return (f"quest {quest_id} has allow_send false and no claimed Telegram approval "
                f"for this message")
    if sending and target and not str(payload.get("idempotency_key") or "").strip():
        return "idempotency_key is required for a dispatched Telegram send"
    return None


async def _resolve_peer(client, requested):
    value = str(requested).strip()
    from telethon import utils
    if value.lstrip("-").isdigit():
        target = int(value)
    else:
        try:
            target = utils.get_peer_id(await client.get_entity(value))
        except (TypeError, ValueError):
            target = None
    title_matches = []
    async for dialog in client.iter_dialogs():
        if target is not None and utils.get_peer_id(dialog.entity) == target:
            return dialog.entity
        if target is None and str(getattr(dialog, "name", "")).casefold() == value.casefold():
            title_matches.append(dialog.entity)
    if len(title_matches) == 1:
        return title_matches[0]
    if len(title_matches) > 1:
        raise ValueError(f"Telegram dialog title {value!r} is ambiguous; use its numeric id")
    raise ValueError(f"Telegram peer {value!r} is not an accessible dialog")


async def _save_draft(payload):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.functions.messages import SaveDraftRequest
        from telethon.tl.types import InputReplyToMessage
    except ImportError as exc:
        raise CredentialError("Telethon is not installed; run pip install 'sidequestor[telegram]'") from exc

    credential_id = str(payload.get("credential_id") or "default")
    bundle = load_bundle(credential_id)
    client = TelegramClient(
        StringSession(bundle["session"]), int(bundle["api_id"]), bundle["api_hash"],
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CredentialError("Telegram session is no longer authorized")
        peer = await _resolve_peer(client, payload["peer"])
        reply_to = None
        if payload.get("reply_to_message_id") not in (None, ""):
            reply_to = InputReplyToMessage(
                reply_to_msg_id=int(payload["reply_to_message_id"]),
            )
        saved = await client(SaveDraftRequest(
            peer=peer,
            message=str(payload["message"]),
            reply_to=reply_to,
        ))
        if not saved:
            raise RuntimeError("Telegram did not confirm the draft save")
        return {
            "peer": str(payload["peer"]),
            "draft_saved": True,
        }
    finally:
        await client.disconnect()


async def _send_message(payload):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise CredentialError("Telethon is not installed; run pip install 'sidequestor[telegram]'") from exc

    credential_id = str(payload.get("credential_id") or "default")
    bundle = load_bundle(credential_id)
    client = TelegramClient(
        StringSession(bundle["session"]), int(bundle["api_id"]), bundle["api_hash"],
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CredentialError("Telegram session is no longer authorized")
        peer = await _resolve_peer(client, payload["peer"])
        reply_to = None
        if payload.get("reply_to_message_id") not in (None, ""):
            reply_to = int(payload["reply_to_message_id"])
        message = await client.send_message(
            peer,
            str(payload["message"]),
            reply_to=reply_to,
        )
        message_id = getattr(message, "id", None)
        if message_id is None:
            raise RuntimeError("Telegram did not return a sent message id")
        return {
            "peer": str(payload["peer"]),
            "delivered": True,
            "message_id": str(message_id),
        }
    finally:
        await client.disconnect()


def _send_with_idempotency(payload):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        return asyncio.run(_send_message(payload))
    bound_payload = {
        name: value for name, value in payload.items()
        if name not in ("approval_id", "idempotency_key", "note")
    }
    fingerprint = hashlib.sha256(json.dumps(
        bound_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    state = REPO_ROOT / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "telegram-idempotency.json"
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        records = json.loads(raw) if raw else {}
        if key in records:
            record = records[key]
            if record.get("fingerprint") != fingerprint:
                raise RuntimeError(
                    f"idempotency key {key!r} belongs to a different Telegram send")
            if record.get("status") == "complete":
                return record["result"]
            raise RuntimeError(
                f"Telegram send {key!r} has an indeterminate prior attempt; inspect Telegram before retrying")
        records[key] = {"status": "started", "ts": utc_now(), "fingerprint": fingerprint}
        handle.seek(0)
        handle.truncate()
        json.dump(records, handle)
        handle.flush()
        os.fsync(handle.fileno())
        try:
            result = asyncio.run(_send_message(payload))
        except (CredentialError, ValueError):
            records.pop(key, None)
            handle.seek(0)
            handle.truncate()
            json.dump(records, handle)
            handle.flush()
            os.fsync(handle.fileno())
            raise
        records[key] = {"status": "complete", "ts": utc_now(), "fingerprint": fingerprint,
                        "result": result}
        handle.seek(0)
        handle.truncate()
        json.dump(records, handle)
        handle.flush()
        os.fsync(handle.fileno())
        return result


def main():
    payload = _parse_args(sys.argv[1:])
    peer = str(payload.get("peer") or "").strip()
    message = payload.get("message")
    if not peer or message is None or not str(message).strip():
        print("error: peer and message are required", file=sys.stderr)
        sys.exit(1)
    if len(str(message)) > MAX_DRAFT_LENGTH:
        print(f"error: message exceeds Telegram's {MAX_DRAFT_LENGTH}-character draft limit",
              file=sys.stderr)
        sys.exit(1)

    if not isinstance(payload.get("send", False), bool):
        print("error: send must be true or false", file=sys.stderr)
        sys.exit(1)
    sending = payload.get("send", False)
    policy_reason = _policy_reason(payload)
    if policy_reason:
        print(f"error: Telegram action denied: {policy_reason}", file=sys.stderr)
        sys.exit(1)

    try:
        result = _send_with_idempotency(payload) if sending else asyncio.run(_save_draft(payload))
    except (CredentialError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)

    quest_id = payload.get("quest_id")
    logged = False
    if quest_id:
        qdir = quest_dir(REPO_ROOT, quest_id)
        if qdir is None:
            print(f"warning: quest '{quest_id}' not found; draft saved but not logged", file=sys.stderr)
        else:
            entry = {
                "ts": utc_now(),
                "event": "message_sent" if sending else "draft_posted",
                "surface": "telegram",
                "channel_id": peer,
                "peer": peer,
                "message_text": str(message),
            }
            if result.get("message_id") is not None:
                entry["message_id"] = str(result["message_id"])
            if payload.get("reply_to_message_id") not in (None, ""):
                entry["reply_to_message_id"] = str(payload["reply_to_message_id"])
            if payload.get("note"):
                entry["note"] = payload["note"]
            append_timeline(qdir, entry)
            logged = True

    print(json.dumps({
        "peer": peer,
        "delivered": bool(result.get("delivered")),
        "draft_saved": bool(result.get("draft_saved")),
        **({"message_id": result["message_id"]} if result.get("message_id") is not None else {}),
        "logged": logged,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
