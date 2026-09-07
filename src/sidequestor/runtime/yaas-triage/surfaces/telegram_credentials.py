#!/usr/bin/env python3
"""Authorize a Telegram user and keep its StringSession in macOS Keychain."""

import asyncio
import getpass
import json
import sys

from credential_store import CredentialStore
from slack_credentials import CredentialError


SERVICE = "sidequestor-telegram-user"


def load_bundle(credential_id="default", store=None):
    bundle = (store or CredentialStore(SERVICE, credential_id)).load()
    if not bundle:
        raise CredentialError(
            f"Telegram credential {credential_id!r} is missing; run telegram_credentials.py authorize")
    required = ("api_id", "api_hash", "session", "user_id")
    if any(not bundle.get(key) for key in required):
        raise CredentialError(f"Telegram credential {credential_id!r} is incomplete")
    return bundle


async def _authorize(api_id, phone, credential_id, api_hash):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise CredentialError("Telethon is not installed; run pip install 'sidequestor[telegram]'") from exc

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        await client.start(phone=phone)
        me = await client.get_me()
        session = client.session.save()
        bundle = {
            "version": 1,
            "api_id": int(api_id),
            "api_hash": api_hash,
            "session": session,
            "user_id": str(me.id),
            "username": getattr(me, "username", None),
        }
        CredentialStore(SERVICE, credential_id).save(bundle)
        return {"credential_id": credential_id, "user_id": str(me.id),
                "username": getattr(me, "username", None)}
    finally:
        await client.disconnect()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "authorize" and 2 <= len(argv) <= 3:
            api_id = argv[1]
            credential_id = argv[2] if len(argv) == 3 else "default"
            phone = getpass.getpass("Telegram phone number (international format): ").strip()
            if not phone:
                raise CredentialError("Telegram phone number is required")
            api_hash = getpass.getpass("Telegram API hash: ").strip()
            if not api_hash:
                raise CredentialError("Telegram API hash is required")
            summary = asyncio.run(_authorize(api_id, phone, credential_id, api_hash))
        elif argv and argv[0] == "status" and len(argv) <= 2:
            credential_id = argv[1] if len(argv) == 2 else "default"
            bundle = load_bundle(credential_id)
            summary = {"credential_id": credential_id, "configured": True,
                       "user_id": bundle["user_id"], "username": bundle.get("username")}
        else:
            print("usage: telegram_credentials.py authorize API_ID [CREDENTIAL_ID]\n"
                  "       telegram_credentials.py status [CREDENTIAL_ID]", file=sys.stderr)
            return 3
    except (CredentialError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
