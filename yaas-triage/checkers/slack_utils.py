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
checkers/slack_utils.py — shared utilities for Slack MCP checker scripts.
"""
import re


def parse_slack_messages(
    text: str,
    since: float,
    filter_user_ids: list = None,
    filter_keywords: list = None,
) -> tuple[int, str]:
    """Parse Slack MCP message text (Message TS: / body lines format).

    Returns (count, preview) where preview is the body of the newest new message.
    count is the number of messages with ts > since that pass the optional filters.

    filter_user_ids: if set, only count messages from these Slack user IDs.
    filter_keywords: if set, only count messages whose body contains at least one keyword.

    The MCP header format is:
        === Message from NAME <email> (USER_ID) at DATETIME ===
        Message TS: NUMERIC_TS
        body lines...
    """
    lines = text.split("\n")
    count, newest_ts, preview = 0, 0.0, ""
    current_user_id = None
    i = 0
    while i < len(lines):
        header_m = re.match(r"=== Message from .+\(([A-Z0-9]+)\) at .+ ===", lines[i])
        if header_m:
            current_user_id = header_m.group(1)
            i += 1
            continue

        ts_m = re.match(r"Message TS:\s*([0-9]+\.[0-9]+)", lines[i])
        if ts_m:
            ts = float(ts_m.group(1))
            if ts > since:
                body, j = [], i + 1
                while j < len(lines):
                    ln = lines[j]
                    if (ln.startswith("===") or ln.startswith("---")
                            or ln.startswith("Thread: ")
                            or re.match(r"Message TS:", ln)):
                        break
                    body.append(ln)
                    j += 1
                body_text = " ".join(" ".join(body).split())

                if filter_user_ids and current_user_id not in filter_user_ids:
                    i += 1
                    continue
                if filter_keywords:
                    body_lower = body_text.lower()
                    if not any(kw.lower() in body_lower for kw in filter_keywords):
                        i += 1
                        continue

                count += 1
                if ts > newest_ts:
                    newest_ts = ts
                    preview = body_text[:100]
        i += 1
    return count, preview
