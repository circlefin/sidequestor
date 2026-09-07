#!/usr/bin/env python3
"""Run bounded Telegram history queries as the Keychain-authorized user."""

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

from slack_credentials import CredentialError, TransientCredentialError, _state_root
from telegram_credentials import load_bundle


OK, AUTH, ERROR, BAD_ARGS, TRANSIENT = 0, 1, 2, 3, 4


def _cache_file():
    """Reuses slack_credentials._state_root so the cache lands beside the other per-workspace
    state instead of re-deriving a root that helper already resolves (and documents)."""
    return _state_root() / "state" / "telegram-peers.json"


def _cache_read(key):
    try:
        cached = json.loads(_cache_file().read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return cached if isinstance(cached, dict) else None


def _cache_write(key, entry):
    """Best effort. A cache that cannot be written must never fail the poll, because the
    dialog scan below is always still a correct way to answer."""
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            store = json.loads(path.read_text())
            if not isinstance(store, dict):
                store = {}
        except (OSError, ValueError):
            store = {}
        store[key] = entry
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(store, separators=(",", ":"), sort_keys=True))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        pass


def _input_peer(cached):
    """Rebuild an InputPeer from cached identity, so a numeric peer costs zero network calls."""
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser
    kind, ident, access_hash = cached.get("kind"), cached.get("id"), cached.get("access_hash")
    if not isinstance(ident, int):
        return None
    if kind == "chat":
        return InputPeerChat(chat_id=ident)
    if not isinstance(access_hash, int):
        return None
    if kind == "channel":
        return InputPeerChannel(channel_id=ident, access_hash=access_hash)
    if kind == "user":
        return InputPeerUser(user_id=ident, access_hash=access_hash)
    return None


def _cacheable(entity):
    from telethon.tl.types import Channel, Chat, User
    if isinstance(entity, Channel):
        kind = "channel"
    elif isinstance(entity, User):
        kind = "user"
    elif isinstance(entity, Chat):
        kind = "chat"
    else:
        return None
    ident = getattr(entity, "id", None)
    if not isinstance(ident, int):
        return None
    access_hash = getattr(entity, "access_hash", None)
    if kind == "chat":
        return {"kind": kind, "id": ident}
    if not isinstance(access_hash, int):
        return None
    return {"kind": kind, "id": ident, "access_hash": access_hash}


async def _resolve_peer(client, requested, credential_id="default"):
    """Resolve a peer, walking dialogs only when nothing cheaper can answer.

    A numeric peer has no username to resolve, so the only general way to reach its
    access_hash is to enumerate the authorized user's dialogs. Doing that per watch per tick
    — in a fresh process, with a fresh MTProto connect — is the shape most likely to earn a
    FloodWait, and a FloodWait holds the watermark and stalls the watch. So the resolved
    identity is cached and replayed as an InputPeer, which costs no network call at all.
    """
    value = str(requested)
    if not value.lstrip("-").isdigit():
        return await client.get_entity(value)
    key = f"{credential_id}|{value}"
    cached = _cache_read(key)
    if cached:
        peer = _input_peer(cached)
        if peer is not None:
            return peer
    target = int(value)
    from telethon import utils
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == target:
            entry = _cacheable(dialog.entity)
            if entry:
                _cache_write(key, entry)
            return dialog.entity
    raise ValueError(f"Telegram peer {value!r} is not an accessible dialog")


def _kind(message):
    if getattr(message, "poll", None):
        return "poll"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "document", None):
        return "document"
    return "text"


async def query(params):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise CredentialError("Telethon is not installed; run pip install 'sidequestor[telegram]'") from exc

    deadline = time.monotonic() + 18
    credential_id = params.get("credential_id", "default")
    bundle = load_bundle(credential_id)
    client = TelegramClient(
        StringSession(bundle["session"]), int(bundle["api_id"]), bundle["api_hash"],
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=max(0.1, deadline - time.monotonic()))
        if not await asyncio.wait_for(
                client.is_user_authorized(), timeout=max(0.1, deadline - time.monotonic())):
            raise CredentialError("Telegram session is no longer authorized")
        peer = await asyncio.wait_for(
            _resolve_peer(client, params["peer"], credential_id),
            timeout=max(0.1, deadline - time.monotonic()),
        )
        since = float(params["after_ts"])
        upper = float(params["before_ts"])
        limit = max(1, min(int(params.get("limit", 100)), 500))
        from_user = (await asyncio.wait_for(
            _resolve_peer(client, params["from_user"], credential_id),
            timeout=max(0.1, deadline - time.monotonic()),
        ) if params.get("from_user") else None)

        async def fetch(boundary):
            kwargs = {
                "limit": limit,
                # Telegram offset_date is exclusive and message dates have one-second
                # precision. Adding one includes the complete boundary second.
                "offset_date": datetime.fromtimestamp(boundary + 1, timezone.utc),
            }
            if params.get("query"):
                kwargs["search"] = str(params["query"])
            if from_user is not None:
                kwargs["from_user"] = from_user
            found = []
            async for message in client.iter_messages(peer, **kwargs):
                if message.date is None:
                    raise RuntimeError("Telegram returned a message without a timestamp")
                sender = getattr(message, "sender_id", None)
                found.append({
                    "id": str(message.id),
                    "ts": message.date.timestamp(),
                    "sender_id": str(sender) if sender is not None else "",
                    "outgoing": bool(getattr(message, "out", False)),
                    "kind": _kind(message),
                    "text": (getattr(message, "message", None) or ""),
                })
            return found

        for _attempt in range(16):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"messages": [], "complete": False,
                        "reason": "Telegram slice budget exhausted; cursor held"}
            messages = await asyncio.wait_for(fetch(upper), timeout=remaining)
            timestamps = []
            for message in messages:
                value = message.get("ts")
                if value is None or value == "":
                    raise RuntimeError("Telegram message is missing its timestamp")
                timestamps.append(float(value))
            saturated = len(messages) >= limit
            reached_low = any(value <= since for value in timestamps)
            if not saturated or reached_low:
                return {"messages": messages, "complete": True, "advance_to": upper}
            narrowed = math.floor(since + ((upper - since) / 2.0))
            if narrowed <= since or upper - since <= 1:
                return {"messages": [], "complete": False, "permanent": True,
                        "reason": (f"more than {limit} Telegram messages share the smallest "
                                   f"coverable time slice for peer {params['peer']}; "
                                   "the maximum supported limit is 500")}
            upper = narrowed
        return {"messages": [], "complete": False,
                "reason": f"Telegram backlog for peer {params['peer']} could not be sliced safely"}
    finally:
        await client.disconnect()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: telegram-call.py QUERY_JSON", file=sys.stderr)
        return BAD_ARGS
    try:
        params = json.loads(argv[0])
        if (not isinstance(params, dict) or not params.get("peer")
                or "after_ts" not in params or "before_ts" not in params):
            raise ValueError("query requires peer, after_ts, and before_ts")
        output = asyncio.run(query(params))
    except TransientCredentialError as exc:
        # Must precede CredentialError, which it subclasses. A locked Keychain (-25308) or a
        # Keychain read timeout is the machine being briefly unavailable, not a bad credential:
        # AUTH here maps to Misconfig in the checker, which parks the watch for a human over
        # something that resolves itself on the next unlock. client.py already draws this same
        # line via classify_credential_exception; that helper is not reused directly because it
        # routes a plain CredentialError (credential absent or incomplete) to ERROR, whereas a
        # never-authorized connector should stay AUTH -> Misconfig and ask for a human.
        print(f"ERROR: {exc}", file=sys.stderr)
        return TRANSIENT
    except CredentialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return AUTH
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return BAD_ARGS
    except Exception as exc:  # Telethon's retryable hierarchy is optional at import time.
        name = type(exc).__name__.lower()
        code = TRANSIENT if any(part in name for part in (
            "floodwait", "servererror", "timeout", "timedout", "connection")) else ERROR
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return code
    print(json.dumps(output, separators=(",", ":")))
    return OK


if __name__ == "__main__":
    sys.exit(main())
