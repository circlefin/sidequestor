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
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(os.path.dirname(SCRIPT_DIR), "surfaces", "mcp-call.sh"))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack_utils

def parse_search_results(text, since):
    """Parse Slack MCP search result text (### Result N of M / Message_ts: format).
    Returns (count, preview, newest_ts). newest_ts is the newest Message_ts seen in ANY
    block, including already-seen ones: a message at or below the watermark still proves the
    search index had reached that point, which is what search_advance_to() needs.
    Skips bot messages (From: line tagged [BOT]).
    """
    blocks = re.split(r"### Result \d+ of \d+", text)
    count = 0
    preview = ""
    newest = 0.0
    for b in blocks[1:]:
        first_from = re.search(r"^From: [^\n]*", b, re.MULTILINE)
        if first_from and "[BOT]" in first_from.group(0):
            continue
        m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
        if not m:
            continue
        ts = float(m.group(1))
        newest = max(newest, ts)
        if ts > since:
            count += 1
            if not preview:
                # Extract first Content: line as preview
                cm = re.search(r"Content:\s*(.+)", b)
                preview = cm.group(1).strip()[:100] if cm else ""
    return count, preview, newest


SEARCH_LIMIT = 50   # was 20; a saturated page cannot prove nothing is older.


def main():
    entry = json.loads(sys.argv[1])
    user_id = entry["user_id"]
    since = float(entry.get("last_checked_ts", "0"))

    since_dt = datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(days=1)
    since_date = since_dt.strftime("%Y-%m-%d")
    query = f"from:<@{user_id}> to:<@me> after:{since_date}"

    r = subprocess.run(
        [MCP_CALL, "slack_search_public_and_private",
         json.dumps({"query": query, "limit": SEARCH_LIMIT,
                     "sort": "timestamp", "sort_dir": "desc"})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        # Inspect the body before classifying: mcp-call.sh exits 2 on any JSON-RPC
        # .error, and a rate limit arrives that way.
        # One exit taxonomy from client.py: 0 ok, 1 auth, 2 error, 3 args, 4 transient.
        if r.returncode == 4:
            result.ratelimited(f"slack transient ({result.transient_cause(r.stderr, 'slack_dm')}); watermark held")
            return
        _b = (r.stdout or "").lower()
        if "ratelimited" in _b:
            result.ratelimited("slack ratelimited (transient); watermark held")
        else:
            result.error(f"mcp slack_search_public_and_private failed (exit {r.returncode})")
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
            result.ratelimited("slack ratelimited (transient); watermark held")
        else:
            result.error(f"non-json response: {body[:80]}")
        return

    text = d.get("results", "")
    count, preview, newest = parse_search_results(text, since)
    # Saturation: Slack search returns newest-first, so a full page means older
    # matching hits may be unseen. Hold the cursor rather than skip them.
    hits = text.count("Message_ts:") + text.count("Message TS:")
    # Emit advance_to EXPLICITLY. Leaving it out makes tick.py fall back to
    # `now - lag_map[type]`, which depends on an optional checkers/<type>.lag file — and the
    # slack_dm one did not exist, so the watermark advanced to exactly now and any DM not yet
    # in the search index was buried. search_advance_to() encodes the safe rule once, in
    # code, for both search-backed checkers.
    result.counted(count, preview,
                   # Pass the FLOAT. Formatting it here with `:.6f` would round-half-even, and
                   # result.emit truncates — it cannot undo a round that already happened.
                   advance_to=slack_utils.search_advance_to(newest),
                   complete=hits < SEARCH_LIMIT)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
