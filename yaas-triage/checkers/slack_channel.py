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

Output: count|preview   (preview = first 100 chars of newest new message body)
        error|reason    (on MCP failure — triage treats this as dirty/retry)

Env:    MCP_CALL  path to mcp-call.sh (falls back to ../mcp-call.sh)
"""
import sys
import os
import json
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(SCRIPT_DIR, "mcp-call.sh"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slack_utils import parse_slack_messages


def main():
    entry = json.loads(sys.argv[1])
    channel_id = entry["channel_id"]
    since = float(entry.get("last_checked_ts", "0"))

    r = subprocess.run(
        [MCP_CALL, "slack_read_channel",
         json.dumps({"channel_id": channel_id, "limit": 30})],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"error|mcp slack_read_channel failed (exit {r.returncode})")
        return

    try:
        d = json.loads(r.stdout)
    except Exception:
        body = r.stdout.strip()
        # Permanent Slack lookup failures should not wake the worker forever.
        if "channel_not_found" in body:
            print("0|")
        elif "ratelimited" in body:
            # Transient: rate limits clear on their own. Never treat as clean
            # (a "0" advances the watermark past unseen messages — silent
            # burial), but never treat as `error` either — `error` marks the
            # quest dirty and burns a full Opus dispatch that finds nothing,
            # and the rate-limit was likely caused by the checker volume in the
            # first place. Distinct `ratelimited` outcome: triage skips the
            # quest this tick and holds the watermark. Retries next tick.
            print("ratelimited|slack ratelimited (transient); skipping tick, watermark held")
        else:
            print(f"error|non-json response: {body[:80]}")
        return

    text = d.get("messages", "")
    filter_user_ids = entry.get("filter_user_ids") or None
    filter_keywords = entry.get("filter_keywords") or None
    count, preview = parse_slack_messages(text, since, filter_user_ids, filter_keywords)
    print(f"{count}|{preview}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error|{e}")
