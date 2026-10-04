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

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get("MCP_CALL", os.path.join(os.path.dirname(SCRIPT_DIR), "surfaces", "mcp-call.sh"))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack_utils

def main():
    entry = json.loads(sys.argv[1])
    user_id = entry["user_id"]
    since = float(entry.get("last_checked_ts", "0"))

    since_date = slack_utils.search_since_date(since)
    query = f"<@{user_id}> after:{since_date}"

    try:
        count, preview, advance_to, complete = slack_utils.search_messages(
            MCP_CALL,
            query,
            since,
            content_label="Text",
            self_user_id=user_id,
        )
    except slack_utils.SlackSearchAuth as exc:
        result.misconfig(str(exc))
        return
    except slack_utils.SlackSearchTransient as exc:
        cause = result.transient_cause(str(exc), "slack_mention")
        result.ratelimited(f"slack transient ({cause}); watermark held")
        return
    result.counted(count, preview, advance_to=advance_to, complete=complete)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
