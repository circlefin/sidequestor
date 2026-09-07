#!/usr/bin/env python3
"""Watch posts from one selected X account."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x


def main():
    entry = json.loads(sys.argv[1])
    user_id = str(entry.get("user_id") or "").strip()
    if not user_id:
        raise x.Misconfig("user_id is required")
    fixed = {"tweet.fields": "id,text,author_id,created_at,conversation_id,referenced_tweets"}
    exclusions = []
    if entry.get("exclude_replies"):
        exclusions.append("replies")
    if entry.get("exclude_reposts"):
        exclusions.append("retweets")
    if exclusions:
        fixed["exclude"] = ",".join(exclusions)
    x.cli(entry, path=f"/2/users/{user_id}/tweets", fixed=fixed, lag=x.lag_for("x_user_posts"))


if __name__ == "__main__":
    try:
        main()
    except (x.Misconfig, KeyError, ValueError, TypeError) as exc:
        x.result.misconfig(str(exc))
    except Exception as exc:
        x.result.error(f"{type(exc).__name__}: {exc}")
