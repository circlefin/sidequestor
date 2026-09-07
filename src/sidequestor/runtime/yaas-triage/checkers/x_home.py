#!/usr/bin/env python3
"""Watch the authorized X user's reverse-chronological home timeline."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x


def main():
    entry = json.loads(sys.argv[1])
    user_id = x.current_user_id(entry)
    x.cli(
        entry,
        path=f"/2/users/{user_id}/timelines/reverse_chronological",
        fixed={"tweet.fields": "id,text,author_id,created_at,conversation_id,referenced_tweets"},
        lag=x.lag_for("x_home"),
    )


if __name__ == "__main__":
    try:
        main()
    except (x.Misconfig, KeyError, ValueError, TypeError) as exc:
        x.result.misconfig(str(exc))
    except Exception as exc:
        x.result.error(f"{type(exc).__name__}: {exc}")
