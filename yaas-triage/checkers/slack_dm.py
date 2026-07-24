#!/usr/bin/env python3
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
checkers/slack_dm.py — check for new DMs from a watched Slack user since watermark.

Input:  watch entry JSON as argv[1]
        {"type":"slack_dm","user_id":"U...","last_checked_ts":"1234.567","reason":"..."}

Output: count|preview   (preview = snippet of newest new message)
        error|reason    (on MCP failure — triage treats this as dirty/retry)

Env:    MCP_CALL  path to mcp-call.sh (falls back to ../mcp-call.sh)
"""
import sys
import os
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(SCRIPT_DIR, "mcp-call.sh"))


def parse_search_results(text, since):
    """Parse Slack MCP search result text (### Result N of M / Message_ts: format).
    Returns (count, preview) — preview from the first (most recent) new result block.
    Skips bot messages (From: line tagged [BOT]).
    """
    blocks = re.split(r"### Result \d+ of \d+", text)
    count = 0
    preview = ""
    for b in blocks[1:]:
        first_from = re.search(r"^From: [^\n]*", b, re.MULTILINE)
        if first_from and "[BOT]" in first_from.group(0):
            continue
        m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
        if m and float(m.group(1)) > since:
            count += 1
            if not preview:
                # Extract first Content: line as preview
                cm = re.search(r"Content:\s*(.+)", b)
                preview = cm.group(1).strip()[:100] if cm else ""
    return count, preview


def main():
    entry = json.loads(sys.argv[1])
    user_id = entry["user_id"]
    since = float(entry.get("last_checked_ts", "0"))

    since_dt = datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(days=1)
    since_date = since_dt.strftime("%Y-%m-%d")
    query = f"from:<@{user_id}> to:<@me> after:{since_date}"

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
            # (a "0" advances the watermark past unseen DMs — silent burial),
            # but never treat as `error` either — `error` marks the quest
            # dirty and burns a full Opus dispatch that finds nothing, and the
            # rate-limit was likely caused by the checker volume in the first
            # place. Distinct `ratelimited` outcome: triage skips the quest
            # this tick and holds the watermark. Retries next tick.
            print("ratelimited|slack ratelimited (transient); skipping tick, watermark held")
        else:
            print(f"error|non-json response: {body[:80]}")
        return

    text = d.get("results", "")
    count, preview = parse_search_results(text, since)
    print(f"{count}|{preview}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error|{e}")
