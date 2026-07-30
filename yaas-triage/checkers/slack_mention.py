#!/usr/bin/env python3
"""
checkers/slack_mention.py — check for new Slack messages that @mention a watched user
since the watermark, anywhere the searcher can see (public + private).

Input:  watch entry JSON as argv[1]
        {"type":"slack_mention","user_id":"U...","last_checked_ts":"1234.567","reason":"..."}

Output: count|preview   (preview = snippet of newest new mention)
        error|reason    (on MCP failure — triage treats this as dirty/retry)

Env:    MCP_CALL  path to mcp-call.sh (falls back to ../mcp-call.sh)

Notes:
  - Query is the raw mention token `<@USER_ID>`; Slack search indexes mentions, so this
    surfaces messages that @mention the user across channels (verified 2026-07-19).
  - Skips [BOT] authors and the watched user's own messages, so the bot posting *as*
    the watched user can never re-trigger this watch.
"""
import sys
import os
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(SCRIPT_DIR, "mcp-call.sh"))


def parse_search_results(text, since, self_user_id):
    """Parse Slack MCP search result text (### Result N of M / Message_ts: format).
    Returns (count, preview) — preview from the first (most recent) new result block.
    Skips bot messages and messages authored by self_user_id (the watched user).
    """
    blocks = re.split(r"### Result \d+ of \d+", text)
    count = 0
    preview = ""
    for b in blocks[1:]:
        first_from = re.search(r"^From: [^\n]*", b, re.MULTILINE)
        if first_from:
            from_line = first_from.group(0)
            if "[BOT]" in from_line:
                continue
            if self_user_id and f"(ID: {self_user_id})" in from_line:
                continue
        m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
        if m and float(m.group(1)) > since:
            count += 1
            if not preview:
                cm = re.search(r"Text:\s*(.+)", b)
                preview = cm.group(1).strip()[:100] if cm else ""
    return count, preview


def main():
    entry = json.loads(sys.argv[1])
    user_id = entry["user_id"]
    since = float(entry.get("last_checked_ts", "0"))

    since_dt = datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(days=1)
    since_date = since_dt.strftime("%Y-%m-%d")
    query = f"<@{user_id}> after:{since_date}"

    r = subprocess.run(
        [MCP_CALL, "slack_search_public_and_private",
         json.dumps({"query": query, "limit": 20,
                     "sort": "timestamp", "sort_dir": "desc"})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"error|mcp slack_search_public_and_private failed (exit {r.returncode})")
        return

    try:
        d = json.loads(r.stdout)
    except Exception:
        body = r.stdout.strip()
        if "ratelimited" in body:
            # Transient: rate limits clear on their own. Never treat as clean
            # (a "0" advances the watermark past unseen mentions — silent
            # burial), but never treat as `error` either — `error` marks the
            # quest dirty and burns a full Opus dispatch that finds nothing,
            # and the rate-limit was likely caused by the checker volume in the
            # first place. Distinct `ratelimited` outcome: triage skips the
            # quest this tick and holds the watermark. Retries next tick.
            print("ratelimited|slack ratelimited (transient); skipping tick, watermark held")
        else:
            print(f"error|non-json response: {body[:80]}")
        return

    text = d.get("results", "")
    count, preview = parse_search_results(text, since, user_id)
    print(f"{count}|{preview}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error|{e}")
