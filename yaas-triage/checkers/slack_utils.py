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
import time


PAGE_LIMIT      = 50     # messages per request
MAX_PAGES       = 5      # cover a normal backlog (250 msgs) in one tick; past that,
                         # stop walking and take a coverable slice instead. Walking
                         # further only burns rate limit to learn what page 2 already
                         # told us: the gap is bigger than we can swallow.
MIN_SLICE       = 1      # a slice narrower than a second cannot meaningfully subdivide.
                         # This is the floor for HALVING only. Flooring the initial
                         # density estimate here instead meant a saturated 60s slice
                         # halved to 30s, tripped the floor, and gave up immediately.
SLICE_ATTEMPTS  = 12     # request budget for the slice phase, shared between paging
                         # a dense slice, halving a too-wide one, and walking forward


def drain(fetch_page, since: float, filter_user_ids=None, filter_keywords=None,
          now: float = None):
    """Read everything new on a Slack source, and only claim coverage we can prove.

    `fetch_page(cursor, oldest, latest)` must return `(text, next_cursor, transient)`.

    Returns `(count, preview, advance_to, complete, transient)`.

    Why this is not just "read the newest N"
    ────────────────────────────────────────
    Slack returns newest-first. If you read the newest N of a large backlog you hold a
    SUFFIX of the gap, and the unread part sits directly above the watermark — so the
    cursor can never move, and the next tick reads the same newest N and is stuck the
    same way. That is a livelock: it costs a dispatch every tick and never drains.

    The fix is to bound the read at BOTH ends. `oldest` alone already makes paging
    terminate at the watermark instead of walking a channel's whole history, which is
    the common case and costs one request. When the gap is bigger than one page we take
    a bounded forward SLICE — (watermark, watermark + slice] — which is a PREFIX of the
    gap. A prefix can be fully covered, so the cursor advances to the end of the slice
    and the backlog shrinks every tick until it is gone.
    """
    if now is None:
        now = time.time()

    def read(oldest, latest, cursor=None):
        text, next_cursor, transient = fetch_page(cursor, oldest, latest)
        if transient:
            return None, None, transient
        count, preview, newest, _saw_old, raw_seen = _parse_page(
            text, since, filter_user_ids, filter_keywords)
        return (count, preview, newest, raw_seen), next_cursor, None

    # ── Common case: bound the bottom at the watermark and page the gap out. ──
    total, preview, newest = 0, "", 0.0
    cursor, pages, raw_total, oldest_seen = None, 0, 0, None
    while pages < MAX_PAGES:
        got, cursor, transient = read(since, None, cursor)
        if transient:
            return 0, "", None, False, transient
        count, page_preview, page_newest, raw_seen = got
        total += count
        raw_total += raw_seen
        if not preview and page_preview:
            preview = page_preview
        newest = max(newest, page_newest)
        if raw_seen and page_newest:
            oldest_seen = page_newest if oldest_seen is None else min(oldest_seen, page_newest)
        pages += 1
        if not cursor or not raw_seen:
            # Paging ran out inside the gap, so the gap is fully covered.
            return total, preview, (newest or None), True, None

    # ── The gap is larger than MAX_PAGES * PAGE_LIMIT. Stop trying to swallow it
    #    whole and take a prefix instead, so this tick makes real progress. ──
    # Size the first slice from the ACTUAL gap rather than a fixed guess, then halve
    # until one fits in a page. A fixed 6h slice drains a sparse month-long backlog at
    # 6h per tick no matter how little is in it; starting from half the gap adapts to
    # whatever the real density turns out to be, at the same cost in requests.
    # Size the first slice from the density we just OBSERVED, not from a fixed guess
    # or a blind halving of the gap. The walk above saw raw_total messages spanning
    # (oldest_seen, now]; at that rate, this is roughly how long it takes to accumulate
    # half a page. A fixed guess either drains a sparse backlog far too slowly or burns
    # a request per halving on a dense one.
    slice_sec = max((now - since) / 2.0, MIN_SLICE)
    if oldest_seen and raw_total and now > oldest_seen:
        per_sec = raw_total / (now - oldest_seen)
        if per_sec > 0:
            # Trust the observed density. Do NOT floor it at MIN_SLICE: that is the
            # halving floor, and applying it here would inflate a correctly-small
            # estimate back up to a slice we already know is too dense.
            slice_sec = min(slice_sec, (PAGE_LIMIT / 2.0) / per_sec)
    slice_sec = max(slice_sec, MIN_SLICE)

    def cover(lo, hi, budget):
        """Page (lo, hi] to exhaustion. Returns (count, preview, covered, spent).

        A slice can defeat us in two independent ways: it can span too much TIME, or it
        can be too DENSE. Halving handles the first. Paging handles the second. Earlier
        this only halved, so a burst of 3000 messages inside 30 seconds could never be
        covered at any slice width and the watch stalled permanently.
        """
        c, prev, cur, spent = 0, "", None, 0
        while spent < budget:
            got, cur, transient = read(lo, hi, cur)
            spent += 1
            if transient:
                return c, prev, False, spent
            cnt, page_prev, _newest, raw = got
            c += cnt
            if not prev and page_prev:
                prev = page_prev
            if not cur or not raw:
                return c, prev, True, spent
        return c, prev, False, spent

    cursor_at = since          # how far we have proven coverage this call
    budget = SLICE_ATTEMPTS
    while budget > 0:
        upper = min(cursor_at + slice_sec, now)
        if upper <= cursor_at:
            break
        # Give one slice at most half the remaining budget, so a single dense stretch
        # cannot consume the whole call and leave nothing for the slices after it.
        c, page_prev, covered, spent = cover(cursor_at, upper, max(1, budget // 2))
        budget -= spent
        if not covered:
            slice_sec /= 2.0
            if slice_sec < MIN_SLICE:
                break
            continue
        # (cursor_at, upper] is fully covered. Bank it and keep walking forward with
        # whatever budget is left: one proven slice per call is correct but drains a
        # long backlog far too slowly, and the requests are already paid for.
        total += c
        if not preview and page_prev:
            preview = page_prev
        cursor_at = upper
        if cursor_at >= now:
            break

    if cursor_at > since:
        return total, preview, cursor_at, True, None

    # Even a one-minute slice is saturated. Genuinely pathological; report it honestly
    # and let the no-progress counter escalate it to a human.
    return total, preview, None, False, None


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

    Returns (count, preview, newest_ts, saw_at_or_below_watermark, raw_seen).

    `count` is filtered; `raw_seen` is every message the page returned. Coverage must
    be judged on raw_seen: a slice holding 50 messages that all fail the filter has
    still only shown us 50 of however many are in that slice, and treating it as
    "covered" because the filtered count was 0 would advance the cursor straight past
    the rest.
    `saw_at_or_below_watermark` is computed BEFORE the user/keyword filters, because
    it is a statement about the time window we covered, not about which messages we
    care about. Filtering it would make a page of filtered-out old messages look
    like an undrained window and hold the cursor forever.
    """
    lines = text.split("\n")
    count, newest, preview = 0, 0.0, ""
    saw_old, raw_seen = False, 0
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
            raw_seen += 1
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
    return count, preview, newest, saw_old, raw_seen
