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
checkers/slack_search.py — find new Slack messages matching a search query.

Input:  watch entry JSON as argv[1]
        {"type":"slack_search","query":"\"release notes\"","last_checked_ts":"1234.567"}

The query may use Slack text and conversation/sender modifiers. Date modifiers are
reserved for the checker, which adds a coarse lower bound from the precise watermark.
The shared search helper then filters to timestamps strictly newer than the watermark.
Search results are limited to conversations the connected Slack account can access.
"""
import json
import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CALL = os.environ.get(
    "MCP_CALL",
    os.path.join(os.path.dirname(SCRIPT_DIR), "surfaces", "mcp-call.sh"),
)

sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack_utils
import tick_state


def main():
    entry = json.loads(sys.argv[1])
    query = entry.get("query")
    problem = tick_state.slack_search_query_problem(query)
    if problem:
        result.misconfig(problem)
        return
    query = query.strip()
    try:
        raw_since = entry["last_checked_ts"]
        if isinstance(raw_since, bool):
            raise ValueError("boolean watermark")
        since = float(raw_since)
        if not math.isfinite(since) or since < 0:
            raise ValueError("non-finite or negative watermark")
    except (KeyError, TypeError, ValueError):
        result.misconfig(
            "last_checked_ts must be a non-negative Unix timestamp; "
            "use an explicit older value only for backfill"
        )
        return
    search_query = f"{query} after:{slack_utils.search_since_date(since)}"
    try:
        count, _preview, advance_to, complete = slack_utils.search_messages(
            MCP_CALL,
            search_query,
            since,
            content_label="Text",
        )
    except slack_utils.SlackSearchAuth as exc:
        result.misconfig(str(exc))
        return
    except slack_utils.SlackSearchTransient as exc:
        cause = result.transient_cause(str(exc), "slack_search")
        result.ratelimited(f"slack transient ({cause}); watermark held")
        return
    except Exception as exc:
        # Connector exceptions can include upstream response text. Do not persist it because
        # this search can cover private channels and DMs.
        result.error(f"{type(exc).__name__}: Slack search failed; inspect connector health")
        return
    # Search hits can come from private channels and DMs. Keep message bodies out of the
    # triage log; the worker re-reads only the dispatched watermark prefix before acting.
    result.counted(count, "matching Slack messages", advance_to=advance_to, complete=complete)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        result.error(f"{type(exc).__name__}: {exc}")
