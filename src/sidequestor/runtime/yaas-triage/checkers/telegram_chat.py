#!/usr/bin/env python3
"""Watch new messages visible to a Telegram user in one cloud chat.

Required: ``peer`` (numeric dialog ID, @username, or public link).
Optional: ``credential_id``, ``filter_sender_ids``, ``filter_keywords``,
``filter_kinds``, ``from_user`` (an @username), ``include_outgoing``, and ``limit``
(default 100, max 500).
Secret Chats are not available. This checker detects new messages, not later edits,
deletions, reactions, pins, or membership changes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram


def main():
    entry = json.loads(sys.argv[1])
    if not str(entry.get("peer", "")).strip():
        raise telegram.Misconfig("peer is required")
    telegram.cli(entry, lag=telegram.lag_for("telegram_chat"))


if __name__ == "__main__":
    try:
        main()
    except (telegram.Misconfig, KeyError, ValueError, TypeError) as exc:
        telegram.result.misconfig(str(exc))
    except Exception as exc:
        telegram.result.error(f"{type(exc).__name__}: {exc}")
