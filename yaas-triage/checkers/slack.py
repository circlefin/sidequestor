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
checkers/slack.py — the shared body of every Slack checker.

The four Slack checkers (channel, thread, dm, mention) differ only in which MCP tool
they call and how they name a message; they used to differ in ~400 lines of copied
implementation, including four copies of the exit-code taxonomy below. That taxonomy
carries the incident history (~1,380 rate limits misfiled as hard errors), so it is
the last thing that should exist in four places.

Slack splits into two coverage regimes and this module keeps them explicit:

  read_backed    channel, thread — the source can be paged, so drain() proves the
                 window was covered by reading down to the watermark. complete=false
                 when it saturates instead, so triage refuses to advance the cursor.
  search_backed  dm, mention — Slack search reads an eventually-consistent index, so
                 coverage cannot be proven by reading. search_advance_to() caps the
                 watermark short of now instead, and a full page means hold.

Env: MCP_CALL  path to mcp-call.sh (tick.py always sets it; the fallback is for
     running a checker by hand).
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack_utils
from slack_utils import PAGE_LIMIT, drain

CHECKERS_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_CALL = os.environ.get(
    "MCP_CALL", os.path.join(os.path.dirname(CHECKERS_DIR), "surfaces", "mcp-call.sh"))

SEARCH_LIMIT = 50   # was 20; a saturated page cannot prove nothing is older.
SEARCH_TOOL = "slack_search_public_and_private"


def _mcp(tool, args, timeout=30):
    return subprocess.run([MCP_CALL, tool, json.dumps(args)],
                          capture_output=True, text=True, timeout=timeout)


RATELIMITED = "TRANSIENT: slack ratelimited; watermark held"


def _transient_of(r, tool):
    """The one exit taxonomy, applied once.

    client.py gives every surface the same exit codes:
        0 ok  1 auth  2 error  3 bad args  4 transient
    Acting on them is the entire point of unifying them. Before this, a rate limit came
    back as a generic failure and had to be guessed at from the body text, which is how
    ~1,380 rate limits were misfiled as hard errors.

    Returns a reason string, or None if the call succeeded. A reason starting with
    "TRANSIENT:" means hold the watermark and retry; anything else is an error.
    """
    body = (r.stdout or "").strip()
    if r.returncode == 4:
        return f"TRANSIENT: {result.transient_cause(r.stderr, tool)}; watermark held"
    if r.returncode == 1:
        return f"slack auth failure on {tool}"
    if r.returncode != 0 or not body:
        # A non-zero exit is NOT automatically a hard error: mcp-call.sh exits 2 on any
        # JSON-RPC .error, and a rate limit arrives that way. Inspect the body before
        # classifying, or rate limits get misfiled as `error` and (before the backoff
        # landed) dispatched a paid worker.
        if "ratelimited" in body.lower():
            return RATELIMITED
        return f"mcp {tool} failed (exit {r.returncode}) {body[:60]}"
    return None


def _emit_transient(reason):
    """An explicit marker, not a keyword hunt through prose. Substring-matching the word
    "ratelimited" is how a transient failure got classified as permanent whenever the
    wording changed."""
    if reason.startswith("TRANSIENT:"):
        result.ratelimited(reason)
    else:
        result.error(reason)


def read_backed(entry, tool, identity_args, not_found_tokens):
    """The whole main() for a read-backed Slack checker.

    identity_args    the surface's identity, e.g. {"channel_id": "C..."}. Paging and
                     window bounds are added here.
    not_found_tokens body substrings meaning the surface is permanently gone. Those
                     arrive as plain text with exit 0 and must read as clean-and-
                     complete, else they wake the worker forever.
    """
    since = float(entry.get("last_checked_ts", "0"))

    def fetch_page(cursor, oldest=None, latest=None):
        """One page. Returns (text, next_cursor, transient_reason).

        `oldest`/`latest` bound the read. Bounding the bottom at the watermark is what
        makes paging terminate at the gap instead of walking the channel's history;
        bounding the top is what lets drain() take a coverable forward slice when the
        gap is too big to swallow whole."""
        args = dict(identity_args, limit=PAGE_LIMIT)
        if cursor:
            args["cursor"] = cursor
        if oldest is not None:
            args["oldest"] = f"{float(oldest):.6f}"
        if latest is not None:
            args["latest"] = f"{float(latest):.6f}"
        r = _mcp(tool, args)
        reason = _transient_of(r, tool)
        if reason:
            return "", None, reason
        body = (r.stdout or "").strip()
        try:
            d = json.loads(body)
        except Exception:
            # Permanent lookup failures arrive as plain text with exit 0. Those must
            # read as clean-and-complete, else they wake the worker forever.
            if any(tok in body for tok in not_found_tokens):
                return "", None, None
            if "ratelimited" in body.lower():
                return "", None, RATELIMITED
            return "", None, f"non-json response: {body[:80]}"
        return d.get("messages", ""), _next_cursor(d), None

    count, preview, advance_to, complete, transient = drain(
        fetch_page, since,
        entry.get("filter_user_ids") or None,
        entry.get("filter_keywords") or None,
    )
    if transient:
        _emit_transient(transient)
        return

    # advance_to is the newest message this check actually covered, not "now". If
    # nothing new arrived there is nothing to prove, so leave it unset and let
    # triage use its own clock.
    result.counted(count, preview, advance_to=advance_to, complete=complete)


def _next_cursor(d):
    """Slack MCP reports pagination in a human string; pull the cursor out of it."""
    m = re.search(r"cursor `([^`]+)`", str(d.get("pagination_info") or ""))
    return m.group(1) if m else None


def parse_search_results(text, since, preview_field, skip_author_id=None):
    """Parse Slack MCP search result text (### Result N of M / Message_ts: format).

    Returns (count, preview, newest_ts). newest_ts is the newest Message_ts seen in ANY
    block, including already-seen ones: a message at or below the watermark still proves
    the search index had reached that point, which is what search_advance_to() needs.

    Skips bot messages (From: line tagged [BOT]). skip_author_id additionally skips
    messages authored by that user, so the bot posting *as* the watched user can never
    re-trigger the watch.
    """
    blocks = re.split(r"### Result \d+ of \d+", text)
    count = 0
    preview = ""
    newest = 0.0
    for b in blocks[1:]:
        first_from = re.search(r"^From: [^\n]*", b, re.MULTILINE)
        if first_from:
            from_line = first_from.group(0)
            if "[BOT]" in from_line:
                continue
            if skip_author_id and f"(ID: {skip_author_id})" in from_line:
                continue
        m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
        if not m:
            continue
        ts = float(m.group(1))
        newest = max(newest, ts)
        if ts > since:
            count += 1
            if not preview:
                cm = re.search(rf"{preview_field}:\s*(.+)", b)
                preview = cm.group(1).strip()[:100] if cm else ""
    return count, preview, newest


def search_backed(entry, label, query, preview_field, skip_author_id=None):
    """The whole main() for a search-backed Slack checker.

    query           the Slack search query for this surface.
    preview_field   the block field holding the message body (`Content` or `Text`).
    skip_author_id  see parse_search_results.
    """
    since = float(entry.get("last_checked_ts", "0"))
    r = _mcp(SEARCH_TOOL, {"query": query, "limit": SEARCH_LIMIT,
                           "sort": "timestamp", "sort_dir": "desc"})

    reason = _transient_of(r, label)
    if reason:
        # Rate limits clear on their own. Never treat as clean (a "0" advances the
        # watermark past unseen messages — silent burial), but never as `error` either:
        # `error` marks the quest dirty and burns a full Opus dispatch that finds
        # nothing, and the rate limit was likely caused by the checker volume in the
        # first place. `ratelimited` skips the quest this tick and holds the watermark.
        _emit_transient(reason)
        return

    try:
        d = json.loads(r.stdout)
    except Exception:
        body = (r.stdout or "").strip()
        if "ratelimited" in body.lower():
            _emit_transient(RATELIMITED)
        else:
            result.error(f"non-json response: {body[:80]}")
        return

    text = d.get("results", "")
    count, preview, newest = parse_search_results(text, since, preview_field, skip_author_id)
    # Saturation: Slack search returns newest-first, so a full page means older matching
    # hits may be unseen. Hold the cursor rather than skip them.
    hits = text.count("Message_ts:") + text.count("Message TS:")
    # Emit advance_to EXPLICITLY. Leaving it out makes tick.py fall back to
    # `now - lag_map[type]`, which depends on an optional checkers/<type>.lag file — and
    # the slack_dm one did not exist, so the watermark advanced to exactly now and any DM
    # not yet in the search index was buried. search_advance_to() encodes the safe rule
    # once, in code, for both search-backed checkers.
    result.counted(count, preview,
                   advance_to=f"{slack_utils.search_advance_to(newest):.6f}",
                   complete=hits < SEARCH_LIMIT)


def since_date(entry, back_days=1):
    """The `after:` date for a search query: the watermark, backdated a day.

    Slack's `after:` is date-granular and exclusive, so the extra day is what keeps a
    message that arrived earlier the same day from falling outside the query.
    """
    from datetime import datetime, timezone, timedelta
    since = float(entry.get("last_checked_ts", "0"))
    dt = datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(days=back_days)
    return dt.strftime("%Y-%m-%d")
