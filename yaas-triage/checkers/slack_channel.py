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
checkers/slack_channel.py — check a Slack channel for new top-level messages since watermark.

Input:  watch entry JSON as argv[1]
        {"type":"slack_channel","channel_id":"C...","last_checked_ts":"1234.567","reason":"..."}

Output: one line of JSON per checkers/result.py. Pages the source (50/page, up to
        5 pages) until a message at or below the watermark proves the gap is
        covered; if it saturates first, emits complete=false so triage refuses to
        advance the cursor past messages it never saw. Reports advance_to as the
        newest message actually covered rather than letting triage guess "now".

Env:    MCP_CALL  path to mcp-call.sh (falls back to ../mcp-call.sh)
"""
import sys
import os
import re
import json
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(os.path.dirname(SCRIPT_DIR), "surfaces", "mcp-call.sh"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
from slack_utils import PAGE_LIMIT, drain


def main():
    entry = json.loads(sys.argv[1])
    channel_id = entry["channel_id"]
    since = float(entry.get("last_checked_ts", "0"))

    def fetch_page(cursor, oldest=None, latest=None):
        """One page. Returns (text, next_cursor, transient_reason).

        `oldest`/`latest` bound the read. Bounding the bottom at the watermark is what
        makes paging terminate at the gap instead of walking the channel's history;
        bounding the top is what lets drain() take a coverable forward slice when the
        gap is too big to swallow whole."""
        args = {"channel_id": channel_id, "limit": PAGE_LIMIT}
        if cursor:
            args["cursor"] = cursor
        if oldest is not None:
            args["oldest"] = f"{float(oldest):.6f}"
        if latest is not None:
            args["latest"] = f"{float(latest):.6f}"
        r = subprocess.run(
            [MCP_CALL, "slack_read_channel", json.dumps(args)],
            capture_output=True, text=True, timeout=30,
        )
        body = (r.stdout or "").strip()
        # client.py gives every surface ONE exit taxonomy:
        #   0 ok  1 auth  2 error  3 bad args  4 transient
        # Acting on it is the entire point of unifying them. Before this, a rate limit
        # came back as a generic failure and had to be guessed at from the body text,
        # which is how ~1,380 rate limits were misfiled as hard errors.
        if r.returncode == 4:
            return "", None, "TRANSIENT: rate limit or network; watermark held"
        if r.returncode == 1:
            return "", None, f"slack auth failure on slack_read_channel"
        # A non-zero exit is NOT automatically a hard error: mcp-call.sh exits 2 on
        # any JSON-RPC .error, and a rate limit arrives that way. Inspect the body
        # before classifying, or rate limits get misfiled as `error` and (before the
        # backoff landed) dispatched a paid worker. ~1,380 lifetime occurrences of
        # exactly that were visible in triage.log.
        if "ratelimited" in body.lower():
            return "", None, "TRANSIENT: slack ratelimited; watermark held"
        if r.returncode != 0 or not body:
            return "", None, f"mcp slack_read_channel failed (exit {r.returncode}) {body[:60]}"
        try:
            d = json.loads(body)
        except Exception:
            # Permanent lookup failures arrive as plain text with exit 0. Those must
            # read as clean-and-complete, else they wake the worker forever.
            if "channel_not_found" in body:
                return "", None, None
            return "", None, f"non-json response: {body[:80]}"
        return d.get("messages", ""), _next_cursor(d), None

    count, preview, advance_to, complete, transient = drain(
        fetch_page, since,
        entry.get("filter_user_ids") or None,
        entry.get("filter_keywords") or None,
    )

    if transient:
        # An explicit marker, not a keyword hunt through prose. Substring-matching the
        # word "ratelimited" is how a transient failure got classified as permanent
        # whenever the wording changed.
        if transient.startswith("TRANSIENT:"):
            result.ratelimited(transient)
        else:
            result.error(transient)
        return

    # advance_to is the newest message this check actually covered, not "now". If
    # nothing new arrived there is nothing to prove, so leave it unset and let
    # triage use its own clock.
    result.counted(count, preview, advance_to=advance_to, complete=complete)


def _next_cursor(d):
    """Slack MCP reports pagination in a human string; pull the cursor out of it."""
    m = re.search(r"cursor `([^`]+)`", str(d.get("pagination_info") or ""))
    return m.group(1) if m else None


if __name__ == "__main__":
    result.guard(main)
