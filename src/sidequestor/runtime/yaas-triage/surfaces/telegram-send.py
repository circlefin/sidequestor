#!/usr/bin/env python3
"""Save a native Telegram draft through the authorized user session and log it.

This surface never sends a message to a recipient. It only uses Telegram's
SaveDraftRequest, which stores the text in the account's cloud draft for the
selected dialog and synchronizes it to the account's Telegram clients. Saving
replaces any existing cloud draft in that dialog.

Usage:
    python3 yaas-triage/surfaces/telegram-send.py '<json>'
    python3 yaas-triage/surfaces/telegram-send.py --peer @name --message "hello"

<json> fields:
    peer                 (required)  dialog id, @username, or public link
    message              (required)  the verbatim draft body
    credential_id        (optional)  named Telegram credential (default "default")
    reply_to_message_id  (optional)  reply to this message id in the peer
    quest_id             (optional)  quest to log under
    note                 (optional)  short human summary for the timeline `note`

Output (stdout): compact JSON, e.g.
    {"peer":"@chat","draft_saved":true,"logged":true}

Exit codes:
    0  draft saved successfully
    1  bad arguments or draft denied by quest policy
    2  draft save failed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

SURFACES_DIR = str(Path(__file__).resolve().parent)
if SURFACES_DIR not in sys.path:
    sys.path.insert(0, SURFACES_DIR)

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
    parser.add_argument("--quest-id")
    parser.add_argument("--note")
    ns = parser.parse_args(argv)
    payload = {"peer": ns.peer, "message": ns.message}
    if ns.credential_id:
        payload["credential_id"] = ns.credential_id
    if ns.reply_to_message_id:
        payload["reply_to_message_id"] = ns.reply_to_message_id
    if ns.quest_id:
        payload["quest_id"] = ns.quest_id
    if ns.note:
        payload["note"] = ns.note
    return payload


def _draft_policy_reason(payload):
    target = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = str(payload.get("quest_id") or "").strip()

    if not quest_id:
        return "quest_id is required for a dispatched Telegram draft" if target else None
    if target and target != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {target!r}"
    if "/" in quest_id or quest_id in (".", "..") or any(ord(ch) < 32 or ord(ch) == 127 for ch in quest_id):
        return f"invalid quest id '{quest_id}'"
    qdir = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not qdir.is_dir():
        return (f"quest {quest_id} is not in state/quests/active; a completed or "
                f"archived quest may not create drafts")
    try:
        meta = json.loads((qdir / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid meta.json")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    return None


async def _resolve_peer(client, requested):
    value = str(requested)
    from telethon import utils
    if value.lstrip("-").isdigit():
        target = int(value)
    else:
        target = utils.get_peer_id(await client.get_entity(value))
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == target:
            return dialog.entity
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

    policy_reason = _draft_policy_reason(payload)
    if policy_reason:
        print(f"error: draft denied: {policy_reason}", file=sys.stderr)
        sys.exit(1)

    try:
        result = asyncio.run(_save_draft(payload))
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
                "event": "draft_posted",
                "surface": "telegram",
                "channel_id": peer,
                "peer": peer,
                "message_text": str(message),
            }
            if payload.get("reply_to_message_id") not in (None, ""):
                entry["reply_to_message_id"] = str(payload["reply_to_message_id"])
            if payload.get("note"):
                entry["note"] = payload["note"]
            append_timeline(qdir, entry)
            logged = True

    print(json.dumps({
        "peer": peer,
        "delivered": False,
        "draft_saved": result["draft_saved"],
        "logged": logged,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
