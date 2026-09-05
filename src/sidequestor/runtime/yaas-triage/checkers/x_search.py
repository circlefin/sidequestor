#!/usr/bin/env python3
"""Watch new X posts matching a recent-search query through the official API."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x


def main():
    entry = json.loads(sys.argv[1])
    query = str(entry.get("query", "")).strip()
    if not query or len(query) > 1024:
        raise x.Misconfig("query must contain 1 to 1024 characters")
    x.cli(entry, path="/2/tweets/search/recent",
          fixed={"query": query, "tweet.fields": "id,text,author_id,created_at"},
          lag=x.lag_for("x_search"), max_age=7 * 24 * 60 * 60)


if __name__ == "__main__":
    try:
        main()
    except (x.Misconfig, KeyError, ValueError, TypeError) as exc:
        x.result.misconfig(str(exc))
    except Exception as exc:
        x.result.error(f"{type(exc).__name__}: {exc}")
