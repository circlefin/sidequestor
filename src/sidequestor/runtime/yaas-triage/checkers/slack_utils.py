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
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone


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


# How far back a search-backed checker must keep its watermark. Slack search reads an
# INDEX, not the source, and that index is eventually consistent: a message posted at T is
# not necessarily findable at T. So "search returned nothing" never proves "nothing was
# posted" for the most recent seconds — only for the part of the window old enough to be
# indexed. Advancing to now would step over anything still in flight and bury it, since the
# watermark only ever moves forward.
SEARCH_INDEX_LAG = 120   # seconds; conservative, and the cost of being wrong is asymmetric
SEARCH_PAGE_LIMIT = 20   # slack_search_public_and_private clamps its result page here
SEARCH_MAX_SCAN_PAGES = 50


class SlackSearchTransient(RuntimeError):
    """A retryable Slack search failure whose cursor must remain unchanged."""


class SlackSearchAuth(RuntimeError):
    """A definite authorization failure reported by the shared surface client."""


def _search_cursor(payload):
    match = re.search(r"cursor `([^`]+)`", str(payload.get("pagination_info") or ""))
    return match.group(1) if match else None


def search_since_date(since, index_lag=SEARCH_INDEX_LAG):
    """Return a safe lower bound for Slack's exclusive, date-granular ``after`` filter."""
    lower_bound = max(0.0, float(since) - 86400 - max(0.0, float(index_lag)))
    return datetime.fromtimestamp(lower_bound, timezone.utc).strftime("%Y-%m-%d")


def fetch_search_page(mcp_call, query, cursor=None, limit=SEARCH_PAGE_LIMIT, sort_dir="asc"):
    """Fetch one Slack search page.

    Returns ``(results_text, next_cursor)``. Retryable failures raise
    ``SlackSearchTransient``; malformed or permanent failures raise ``RuntimeError``.
    """
    args = {"query": query, "limit": limit, "sort": "timestamp", "sort_dir": sort_dir}
    if cursor:
        args["cursor"] = cursor
    try:
        completed = subprocess.run(
            [mcp_call, "slack_search_public_and_private", json.dumps(args)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise SlackSearchTransient("Slack search timeout") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        detail_lower = detail.lower()
        if completed.returncode == 1:
            raise SlackSearchAuth("Slack search authorization failed")
        if (completed.returncode == 4 or "ratelimited" in detail_lower
                or "rate_limited" in detail_lower):
            raise SlackSearchTransient(detail or "Slack search transient")
        raise RuntimeError(
            f"mcp slack_search_public_and_private failed (exit {completed.returncode})"
        )
    if not completed.stdout.strip():
        raise RuntimeError("mcp slack_search_public_and_private returned no payload")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        body_lower = completed.stdout.lower()
        if "ratelimited" in body_lower or "rate_limited" in body_lower:
            raise SlackSearchTransient(completed.stdout.strip()) from exc
        raise RuntimeError(f"non-json response: {completed.stdout.strip()[:80]}") from exc
    if "results" not in payload:
        raise RuntimeError("Slack search response omitted results")
    return payload["results"], _search_cursor(payload)


_SEARCH_RESULT_START = re.compile(r"^### Result \d+ of \d+\s*$", re.MULTILINE)
_SEARCH_TS = re.compile(r"^Message(?:_ts| TS):\s*([0-9]+\.[0-9]+)\s*$", re.MULTILINE)


def parse_search_records(text, content_label, self_user_id=None):
    """Return ``(timestamp, preview-or-None)`` records from a Slack search page.

    ``None`` previews are ignored bot/self hits. Their timestamps remain in the result
    so coverage is based on everything Slack returned, not only dispatchable messages.
    """
    starts = list(_SEARCH_RESULT_START.finditer(text))
    if not starts:
        if re.search(r"(?:^|\n)No results found\.\s*$", text):
            return [], 0
        if text.strip():
            raise ValueError("unrecognized Slack search response shape")
        return [], 0
    records = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.end():end]
        content_match = re.search(rf"^{re.escape(content_label)}:\s*", block, re.MULTILINE)
        metadata = block[:content_match.start()] if content_match else block
        timestamp_match = _SEARCH_TS.search(metadata)
        if not timestamp_match:
            raise ValueError("incomplete Slack search result record")
        from_match = re.search(r"^From: [^\n]*", block, re.MULTILINE)
        ignored = bool(from_match and "[BOT]" in from_match.group(0))
        if from_match and self_user_id:
            ignored = ignored or f"(ID: {self_user_id})" in from_match.group(0)
        content = block[content_match.end():] if content_match else ""
        content = content.split("\n---", 1)[0]
        preview = None if ignored else " ".join(content.split())[:100]
        records.append((float(timestamp_match.group(1)), preview))
    return records, len(starts)


def _strictly_before(timestamp):
    return math.floor(timestamp * 1_000_000 - 1) / 1_000_000


def _search_result(records, since, advance_to, complete=True):
    dispatchable = [
        (timestamp, preview)
        for timestamp, preview in records
        if since < timestamp <= advance_to and preview is not None
    ]
    preview = next((body for _timestamp, body in reversed(dispatchable) if body), "")
    return len(dispatchable), preview, advance_to, complete


def _bank_search_prefix(records, since, ceiling):
    if not records:
        return None
    advance_to = min(_strictly_before(records[-1][0]), ceiling)
    if advance_to <= since:
        return None
    return _search_result(records, since, advance_to)


def drain_search(fetch_page, since, content_label, self_user_id=None, now=None,
                 max_active_pages=MAX_PAGES,
                 max_scan_pages=SEARCH_MAX_SCAN_PAGES):
    """Drain an oldest-first Slack search without parking a saturated watermark.

    Pages containing only records at or below ``since`` do not consume the active-page
    budget. When the budget is reached, one lookahead page proves whether the boundary
    timestamp is complete. The returned prefix is therefore safe to commit and the next
    tick can continue forward instead of rereading the same newest-page suffix forever.
    """
    now = time.time() if now is None else now
    ceiling = now - SEARCH_INDEX_LAG
    cursor = None
    records = []
    active_pages = 0
    scanned_pages = 0

    while scanned_pages < max_scan_pages:
        try:
            text, next_cursor = fetch_page(cursor)
        except SlackSearchTransient:
            banked = _bank_search_prefix(records, since, ceiling)
            if banked:
                return banked
            raise
        page, block_count = parse_search_records(text, content_label, self_user_id)
        scanned_pages += 1
        if page:
            timestamps = [timestamp for timestamp, _preview in page]
            if timestamps != sorted(timestamps):
                raise ValueError("Slack search results were not sorted ascending")
            if records and timestamps[0] < records[-1][0]:
                raise ValueError("Slack search pagination moved backwards")
            records.extend(page)
            if any(timestamp > since for timestamp in timestamps):
                active_pages += 1

        if not next_cursor:
            if block_count >= SEARCH_PAGE_LIMIT:
                advance_to = min(_strictly_before(page[-1][0]), ceiling)
                if advance_to <= since:
                    return 0, "", None, False
            else:
                advance_to = max(since, ceiling)
            break
        if not page:
            raise ValueError("Slack search returned an empty page with a cursor")
        if active_pages >= max_active_pages:
            try:
                lookahead_text, _lookahead_cursor = fetch_page(next_cursor)
            except SlackSearchTransient:
                banked = _bank_search_prefix(records, since, ceiling)
                if banked:
                    return banked
                raise
            lookahead, _lookahead_count = parse_search_records(
                lookahead_text, content_label, self_user_id
            )
            boundary = records[-1][0]
            if lookahead and lookahead[0][0] < boundary:
                raise ValueError("Slack search pagination moved backwards")
            advance_to = boundary if lookahead and lookahead[0][0] > boundary else _strictly_before(boundary)
            advance_to = min(advance_to, ceiling)
            if advance_to <= since:
                return 0, "", None, False
            break
        cursor = next_cursor
    else:
        return _bank_search_prefix(records, since, ceiling) or (0, "", None, False)

    return _search_result(records, since, advance_to)


def search_messages(mcp_call, query, since, content_label, self_user_id=None, now=None):
    """Check Slack search efficiently, falling back to prefix draining only for a real backlog."""
    now = time.time() if now is None else now
    ceiling = now - SEARCH_INDEX_LAG
    text, cursor = fetch_search_page(mcp_call, query, sort_dir="desc")
    records, block_count = parse_search_records(text, content_label, self_user_id)
    timestamps = [timestamp for timestamp, _preview in records]
    if timestamps != sorted(timestamps, reverse=True):
        raise ValueError("Slack search results were not sorted descending")

    reached_watermark = bool(records and records[-1][0] <= since)
    exhausted = not cursor and block_count < SEARCH_PAGE_LIMIT
    if reached_watermark or exhausted:
        return _search_result(list(reversed(records)), since, max(since, ceiling))

    return drain_search(
        lambda page_cursor: fetch_search_page(
            mcp_call, query, page_cursor, sort_dir="asc"
        ),
        since,
        content_label,
        self_user_id=self_user_id,
        now=now,
    )


def drain(fetch_page, since: float, filter_user_ids=None, filter_keywords=None,
          now: float = None, page_parser=None):
    """Read everything new on a Slack source, and only claim coverage we can prove.

    `fetch_page(cursor, oldest, latest)` must return `(text, next_cursor, transient)`.
    `page_parser` may override the channel-response parser for sources such as
    Slack threads whose MCP text format is different.

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
    page_parser = page_parser or _parse_page

    def read(oldest, latest, cursor=None):
        text, next_cursor, transient = fetch_page(cursor, oldest, latest)
        if transient:
            return None, None, transient
        count, preview, newest, _saw_old, raw_seen = page_parser(
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


_MESSAGE_HEADER = re.compile(
    r"^=== Message from .+\(([A-Z0-9]+)(?:,\s*external:\s*[^\r\n]+)?\) at .+ ==="
)


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
        === Message from NAME <email> (USER_ID, external: ORGANIZATION) at DATETIME ===
        Message TS: NUMERIC_TS
        body lines...
    """
    lines = text.split("\n")
    count, newest_ts, preview = 0, 0.0, ""
    current_user_id = None
    i = 0
    while i < len(lines):
        header_m = _MESSAGE_HEADER.match(lines[i])
        if header_m:
            current_user_id = header_m.group(1)
            i += 1
            continue

        ts_m = re.match(r"Message TS:\s*([0-9]+\.[0-9]+)", lines[i])
        # A real message is always `=== header ===` then `Message TS:` on ADJACENT
        # lines. Require that adjacency so a `Message TS:` line an author typed into
        # a message body is read as body text, not counted as a new message.
        # ponytail: this enforces the format contract and kills the orphan/lone-TS
        # mis-count, but cannot stop a body that forges BOTH an adjacent
        # `=== Message from ... ===` and its `Message TS:` line — that is inherent to
        # the Slack MCP returning unescaped delimited text and is only fully closed
        # by structured (per-message) MCP output.
        if ts_m and i > 0 and _MESSAGE_HEADER.match(lines[i - 1]):
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
        header_m = _MESSAGE_HEADER.match(lines[i])
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
            # Same adjacency contract as parse_slack_messages: a real message is
            # `=== header ===` then `Message TS:` on adjacent lines. A forged/orphan
            # TS still counts toward raw_seen (coverage stays conservative → cursor
            # held) but is never attributed or counted as a dispatchable message.
            # ponytail: an adjacent forged header+TS pair typed into a body remains
            # indistinguishable in this text format; only structured MCP output
            # closes that fully.
            header_adjacent = i > 0 and _MESSAGE_HEADER.match(lines[i - 1])
            if ts <= since:
                saw_old = True
                i += 1
                continue
            if not header_adjacent:
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


_THREAD_RECORD_START = re.compile(
    r"^(?:=== THREAD PARENT MESSAGE ===|--- Reply \d+ of \d+ ---)$",
    re.MULTILINE,
)
# Slack Connect appends organization metadata inside the author parentheses.
# Capture only the stable user ID so author filters behave identically for both forms.
_THREAD_FROM = re.compile(
    r"^From: .*\(([A-Z0-9]+)(?:,\s*external:\s*[^\r\n]+)?\)$",
    re.MULTILINE,
)
_THREAD_TS = re.compile(r"^Message TS:\s*([0-9]+\.[0-9]+)$", re.MULTILINE)


def _parse_thread_page(text, since, filter_user_ids=None, filter_keywords=None,
                       include_parent=False, until=None):
    """Parse the distinct text shape returned by ``slack_read_thread``.

    Thread responses put ``From:`` and ``Message TS:`` inside parent/reply
    sections, unlike channel responses where the author header immediately
    precedes the timestamp. Keep this parser separate so channel hardening does
    not make thread watches silently clean.
    """
    count, newest, preview = 0, 0.0, ""
    saw_old, raw_seen = False, 0
    starts = list(_THREAD_RECORD_START.finditer(text))
    timestamps = list(_THREAD_TS.finditer(text))
    if text.strip() and not starts:
        raise ValueError("unrecognized slack thread response shape")
    # This also catches timestamp-shaped data before the first record delimiter.
    if len(timestamps) != len(starts):
        raise ValueError("incomplete slack thread response records")

    for index, start in enumerate(starts):
        segment_end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        segment = text[start.end():segment_end]
        from_matches = list(_THREAD_FROM.finditer(segment))
        ts_matches = list(_THREAD_TS.finditer(segment))
        if len(from_matches) != 1 or len(ts_matches) != 1:
            raise ValueError("incomplete slack thread response record")
        from_match, ts_match = from_matches[0], ts_matches[0]
        if from_match.start() > ts_match.start():
            raise ValueError("invalid slack thread response record order")

        user_id = from_match.group(1)
        ts = float(ts_match.group(1))
        raw_seen += 1
        if until is not None and ts > until:
            continue
        if ts <= since:
            saw_old = True

        is_parent = start.group(0) == "=== THREAD PARENT MESSAGE ==="
        if is_parent and not include_parent:
            # slack_read_thread repeats the parent on every page. It identifies
            # the thread but is not new thread activity and must never dispatch.
            # It still counts as raw coverage so a page with a live cursor cannot
            # terminate drain() merely because that page has no replies.
            continue

        if ts <= since:
            continue

        body = segment[ts_match.end():]
        # As with channel parsing, fully forged delimiter/author/timestamp lines
        # in a message body are indistinguishable in the MCP's unescaped text.
        body_text = " ".join(body.split())
        if filter_user_ids and user_id not in filter_user_ids:
            continue
        if filter_keywords:
            body_lower = body_text.lower()
            if not any(kw.lower() in body_lower for kw in filter_keywords):
                continue

        count += 1
        if ts > newest:
            newest = ts
            preview = body_text[:100]
    return count, preview, newest, saw_old, raw_seen


def thread_page_has_ts(text, target_ts):
    """Whether a Slack thread page contains an exact Message TS line."""
    target = float(target_ts)
    return any(float(match.group(1)) == target for match in _THREAD_TS.finditer(text))
