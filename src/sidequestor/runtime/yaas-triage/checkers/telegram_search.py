#!/usr/bin/env python3
"""Watch new Telegram messages matching text within one user-visible cloud chat.

Required: ``peer`` and ``query``. Optional fields are the same as telegram_chat.
Search is index-backed and therefore uses a 30-second lag.
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
    query = str(entry.get("query", "")).strip()
    if not query:
        raise telegram.Misconfig("query is required")
    telegram.cli(entry, query=query, lag=telegram.lag_for("telegram_search"))


if __name__ == "__main__":
    try:
        main()
    except (telegram.Misconfig, KeyError, ValueError, TypeError) as exc:
        telegram.result.misconfig(str(exc))
    except Exception as exc:
        telegram.result.error(f"{type(exc).__name__}: {exc}")
