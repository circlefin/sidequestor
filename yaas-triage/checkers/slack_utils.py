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


PAGE_LIMIT = 50   # was 30. Raised because the cost of a wider first page is one
                  # cheap API call, while the cost of missing messages is silent.
MAX_PAGES  = 5    # hard stop at 250 messages of backlog on a single watch.


def drain(fetch_page, since: float, filter_user_ids=None, filter_keywords=None):
    """Page a newest-first message source back until the watermark is passed.

    `fetch_page(cursor)` must return `(text, next_cursor, transient_error)`, where
    text is the MCP message blob and transient_error is a truthy reason string if
    the page could not be read.

    Returns `(count, preview, newest_ts, complete, transient)`.

    `complete` is the whole point. A bounded window that comes back FULL of
    post-watermark messages does not prove there are no older unseen ones, and
    advancing the cursor to "now" in that case skips them permanently. We keep
    paging until we see a message at or below the watermark — which proves the gap
    is covered — or until MAX_PAGES, which returns complete=False so triage refuses
    to advance the cursor at all.
    """
    total, preview, newest = 0, "", 0.0
    cursor, pages, complete = None, 0, False
    while pages < MAX_PAGES:
        text, cursor, transient = fetch_page(cursor)
        if transient:
            return total, preview, newest, False, transient
        count, page_preview, page_newest, saw_old, saw_any = _parse_page(
            text, since, filter_user_ids, filter_keywords)
        total += count
        if not preview and page_preview:
            preview = page_preview
        newest = max(newest, page_newest)
        pages += 1
        # saw_old: a message at or below the watermark, so everything newer than the
        # watermark is now accounted for. not saw_any / no cursor: the source is
        # exhausted. Either way the gap is fully covered.
        if saw_old or not saw_any or not cursor:
            complete = True
            break
    return total, preview, newest, complete, None


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
            # current_user_id is only valid for the message right after its
            # header; capture then clear so a TS block with no preceding header
            # can't inherit (and mis-attribute to) the previous message's author.
            msg_user_id = current_user_id
            current_user_id = None
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

                if filter_user_ids and msg_user_id not in filter_user_ids:
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


def _parse_page(text, since, filter_user_ids=None, filter_keywords=None):
    """Single-page parse used by drain().

    Returns (count, preview, newest_ts, saw_at_or_below_watermark, saw_any_message).
    `saw_at_or_below_watermark` is computed BEFORE the user/keyword filters, because
    it is a statement about the time window we covered, not about which messages we
    care about. Filtering it would make a page of filtered-out old messages look
    like an undrained window and hold the cursor forever.
    """
    lines = text.split("\n")
    count, newest, preview = 0, 0.0, ""
    saw_old, saw_any = False, False
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
            msg_user_id = current_user_id
            current_user_id = None
            ts = float(ts_m.group(1))
            saw_any = True
            if ts <= since:
                saw_old = True
                i += 1
                continue

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

            if filter_user_ids and msg_user_id not in filter_user_ids:
                i += 1
                continue
            if filter_keywords:
                low = body_text.lower()
                if not any(kw.lower() in low for kw in filter_keywords):
                    i += 1
                    continue

            count += 1
            if ts > newest:
                newest = ts
                preview = body_text[:100]
        i += 1
    return count, preview, newest, saw_old, saw_any
