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
                         in triage.sh check_quest)
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
import subprocess
from datetime import datetime
from urllib.parse import quote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JIRA_CALL = os.environ.get("JIRA_CALL", os.path.join(SCRIPT_DIR, "..", "jira-call.sh"))

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

    # Sort newest-changed first so changed issues can never hide on a later page.
    # Only safe to rely on (and to early-stop against) when we own the ordering.
    caller_ordered = "order by" in jql.lower()
    effective_jql = jql if caller_ordered else f"{jql} ORDER BY updated DESC"

    base = ("/rest/api/3/search/jql?jql=" + quote(effective_jql)
            + "&fields=status,summary,updated&maxResults=100")

    changed, token, pages, capped = [], None, 0, False
    while True:
        pages += 1
        path = base + (f"&nextPageToken={quote(token)}" if token else "")
        d = jira_get(path)
        issues = d.get("issues") or []

        reached_old = False
        for issue in issues:
            if updated_epoch(issue) > since_ts:
                changed.append(issue)
            elif not caller_ordered:
                # Descending sort: this and everything after it predates the
                # watermark, so nothing further can be new.
                reached_old = True
                break

        if reached_old or d.get("isLast") or not d.get("nextPageToken"):
            break
        if pages >= MAX_PAGES:
            capped = True
            break
        token = d["nextPageToken"]

    # `capped` means the paging loop stopped at its page cap, so older changed
    # issues may be unseen. It used to only warn inside the human preview string,
    # where nothing read it; now it blocks the cursor advance.
    if not changed:
        result.counted(0, "", complete=not capped)
        return

    changed.sort(key=updated_epoch, reverse=True)
    top = changed[0]
    key = top.get("key", "?")
    status = top.get("fields", {}).get("status", {}).get("name", "?")
    summary = (top.get("fields", {}).get("summary") or "")[:55]
    # count MUST stay a bare integer — triage compares it with `-gt`, so any
    # non-numeric decoration ("18+") fails that test and the quest reads clean,
    # silently swallowing the dispatch. Page-cap warning goes in the preview.
    more = f" (+more, page cap {MAX_PAGES} hit)" if capped else ""
    result.counted(len(changed), f"{key} [{status}] — {summary}{more}",
                   advance_to=updated_epoch(top), complete=not capped)


if __name__ == "__main__":
    try:
        main()
    except Transient as e:
        # Not dirty: skip the tick. Watermark is held, so the change is not lost.
        result.ratelimited(str(e))
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
