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
checkers/jira.py — check a JQL result set for issues changed since the watermark.

Headless analog of the interactive Atlassian MCP: reads through the REST bridge
(../jira-call.sh, Basic-auth API token in Keychain), so the pre-dispatch checker
runs with no LLM cost and dispatches Opus ONLY when a watched issue actually
moved (status change, new comment, any field edit — all bump Jira's `updated`).

Input:  watch entry JSON as argv[1]
        {"type":"jira","jql":"labels=my-label",
         "last_checked_ts":"1234.567","reason":"..."}

Output: count|preview   (preview = "KEY [status] — summary" of newest-updated
                         changed issue; count = number of changed issues)
        0|              (nothing changed since the watermark)
        ratelimited|r   (transient Jira 429/5xx/timeout — triage SKIPS the tick
                         instead of dispatching; see the 2026-07-24 storm note
                         in the original shell orchestrator check_quest)
        error|reason    (hard failure — triage treats this as dirty/retry)

Env:    JIRA_CALL   path to the REST bridge (falls back to ../jira-call.sh)

Watermark lag: 15 seconds (see jira.lag). Only needed because the JQL *search
index* can trail a write by a few seconds (an issue changed at T may not appear
in results until T+n). Kept deliberately small: every second of lag widens the
re-report window, and a change caught inside that window dispatches a second
time on the following tick. 15s covers realistic index lag without routinely
double-dispatching. (Contrast email.lag=120 — Gmail's index is far slower.)

Precision: JQL `updated` filtering is only minute-granular and timezone-
sensitive, so we DON'T filter in JQL. We pull `updated` per issue and compare
its epoch to last_checked_ts in Python — same post-filter pattern as email.py.

Pagination: /rest/api/3/search/jql is CURSOR-paginated (isLast + nextPageToken)
and returns NO `total`. Reading only the first page would silently miss a changed
issue once the set outgrows one page, so we (a) append `ORDER BY updated DESC`
when the caller's JQL has no ORDER BY, which floats every changed issue onto the
first page and lets us stop at the first issue at/below the watermark, and
(b) follow nextPageToken when a full page is still all-changed. If a caller
supplies its own ORDER BY we cannot assume sort order, so the early stop is
disabled and we page up to MAX_PAGES. Hitting that cap is surfaced in the
preview rather than silently truncating.

Do NOT put an ORDER BY in a watch entry's `jql` unless you need it: it disables
the early stop, so every tick pages to MAX_PAGES. Measured against a ~1000-issue
project: 12.9s and 10 API calls per tick with a caller ORDER BY, vs 1.0s and one
call without. Leave the ordering to this checker.
"""
import sys
import os
import json
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JIRA_CALL = os.environ.get("JIRA_CALL", os.path.join(SCRIPT_DIR, "..", "surfaces", "jira-call.sh"))

# Bridge exit 4 = transient (429 / 5xx / timeout). Surfaced as `ratelimited`.
EXIT_TRANSIENT = 4
MAX_PAGES = 10


class Transient(Exception):
    """Retryable upstream condition — skip the tick, don't dispatch."""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result

def jira_get(path, timeout=30):
    r = subprocess.run([JIRA_CALL, "GET", path], capture_output=True, text=True, timeout=timeout)
    detail = (r.stderr or "").strip().splitlines()
    msg = detail[-1] if detail else f"jira-call.sh exit {r.returncode}"
    if r.returncode == EXIT_TRANSIENT:
        raise Transient(msg)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(msg)
    return json.loads(r.stdout)


def updated_epoch(issue):
    """Epoch seconds of an issue's `updated` field.

    Raises rather than returning a sentinel: a 0.0 fallback would sort to the
    bottom under `ORDER BY updated DESC` and trip the early stop, silently
    truncating the scan. A loud error (-> dirty) is the safe failure here.
    """
    s = (issue.get("fields") or {}).get("updated")
    if not s:
        raise ValueError(f"{issue.get('key', '?')}: no `updated` field in response")
    try:
        # Handles all Jira variants: ±0400, ±00:00, and trailing Z (py3.11+).
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # Fallback for older interpreters, which reject `Z` and `+00:00`.
        return datetime.strptime(s.replace("Z", "+0000"),
                                 "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def main():
    entry = json.loads(sys.argv[1])
    jql = entry["jql"]
    since_ts = float(entry.get("last_checked_ts") or 0)

    # Bound the LOW end and sort ASCENDING, so the pages we hold are a contiguous PREFIX of
    # the gap. Newest-first was a suffix: on a set busier than the page cap the cursor could
    # never advance past it, which is the livelock github_pr hit for 14 hours on 2026-08-05.
    # JQL wants "yyyy/MM/dd HH:mm" and is minute-granular, so back off a minute and re-apply
    # the exact boundary in the post-filter below.
    caller_ordered = "order by" in jql.lower()
    bound = ""
    if since_ts > 0:
        since_str = datetime.fromtimestamp(since_ts - 60, timezone.utc).strftime("%Y/%m/%d %H:%M")
        bound = f' AND updated >= "{since_str}"'

    if caller_ordered:
        # The caller owns the sort, so we cannot assume a prefix. Insert the bound before
        # their ORDER BY and keep the conservative page-cap semantics.
        m = re.search(r"(?i)\s+order\s+by\s+", jql)
        head, tail = jql[:m.start()], jql[m.start():]
        effective_jql = f"({head}){bound}{tail}"
    else:
        effective_jql = f"({jql}){bound} ORDER BY updated ASC"

    base = ("/rest/api/3/search/jql?jql=" + quote(effective_jql)
            + "&fields=status,summary,updated&maxResults=100")

    changed, token, pages, capped = [], None, 0, False
    while True:
        pages += 1
        path = base + (f"&nextPageToken={quote(token)}" if token else "")
        d = jira_get(path)
        issues = d.get("issues") or []

        for issue in issues:
            if updated_epoch(issue) > since_ts:
                changed.append(issue)

        if d.get("isLast") or not d.get("nextPageToken"):
            break
        if pages >= MAX_PAGES:
            capped = True
            break
        token = d["nextPageToken"]

    changed.sort(key=updated_epoch)

    # ── Tie safety, same hazard as github_pr ────────────────────────────────────────────
    # Ascending order alone does not make a capped page a safe prefix: if paging stops partway
    # through timestamp T, advancing to T makes the next run filter `> T` and permanently skip
    # the rows at T we never saw. On a capped page the only provable boundary is strictly below
    # the final row's timestamp. Caller-ordered queries are exempt because we do not own the
    # sort there and already keep the conservative page-cap rule.
    if capped and not caller_ordered and changed:
        boundary = updated_epoch(changed[-1])
        safe = [i for i in changed if updated_epoch(i) < boundary]
        if not safe:
            result.emit("hold", count=len(changed), preview="", complete=False,
                        reason=(f"all {len(changed)} changed issues share the same updated "
                                f"timestamp on a capped page; the watermark cannot advance "
                                f"safely"))
            return
        changed = safe

    if not changed:
        if capped and not caller_ordered:
            # The page filled yet nothing is past the watermark, so the boundary timestamp
            # spans more than the page cap. Reporting clean+complete would let triage advance
            # this watch to now-lag and skip everything beyond it.
            result.emit("hold", count=0, preview="", complete=False,
                        reason="a capped page produced nothing past the watermark; the "
                               "boundary timestamp spans more than one page of results")
            return
        # An empty gap is covered by definition; a caller-ordered query that hit the page cap
        # genuinely might have missed something.
        result.counted(0, "", complete=(not capped) or not caller_ordered)
        return

    newest = changed[-1]
    key = newest.get("key", "?")
    status = newest.get("fields", {}).get("status", {}).get("name", "?")
    summary = (newest.get("fields", {}).get("summary") or "")[:55]

    if caller_ordered:
        complete = not capped
        more = f" (+more, page cap {MAX_PAGES} hit)" if capped else ""
    else:
        complete = True
        more = f" (+backlog; oldest {len(changed)} first)" if capped else ""

    # count MUST stay a bare integer — triage compares it with `-gt`, so any non-numeric
    # decoration ("18+") fails that test and the quest reads clean, silently swallowing the
    # dispatch. Page-cap warning goes in the preview.
    result.counted(len(changed), f"{key} [{status}] — {summary}{more}",
                   advance_to=updated_epoch(newest), complete=complete)


if __name__ == "__main__":
    try:
        main()
    except Transient as e:
        # Not dirty: skip the tick. Watermark is held, so the change is not lost.
        result.ratelimited(str(e))
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
