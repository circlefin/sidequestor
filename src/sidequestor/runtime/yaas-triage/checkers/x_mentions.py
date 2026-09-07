#!/usr/bin/env python3
"""Watch posts mentioning the authorized X user."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x


def main():
    entry = json.loads(sys.argv[1])
    user_id = str(entry.get("user_id") or x.current_user_id(entry))
    x.cli(
        entry,
        path=f"/2/users/{user_id}/mentions",
        fixed={"tweet.fields": "id,text,author_id,created_at,conversation_id,referenced_tweets"},
        lag=x.lag_for("x_mentions"),
    )


if __name__ == "__main__":
    try:
        main()
    except (x.Misconfig, KeyError, ValueError, TypeError) as exc:
        x.result.misconfig(str(exc))
    except Exception as exc:
        x.result.error(f"{type(exc).__name__}: {exc}")
