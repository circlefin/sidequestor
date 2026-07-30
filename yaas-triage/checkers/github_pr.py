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
checkers/github_pr.py — check a repo's PRs for activity since the watermark.

Companion to checkers/jira.py. A reviewer comment left on a GitHub PR does NOT
bump the linked Jira issue's `updated`, so the `jira` watch cannot see it; this
checker covers that half. Runs on `gh` (bash, no LLM) and dispatches Opus only
when a PR actually moves.

Input:  watch entry JSON as argv[1]
        {"type":"github_pr","repo":"owner/repo",
         "last_checked_ts":"1234.567","reason":"..."}
        Optional:
          "search" — extra GitHub search qualifiers/terms, e.g. "author:dave"
                     or "label:docs". Narrow the set when a repo is noisy.
                     WARNING, two ways to silently break this watch:
                     (1) Repeated qualifiers AND, they do NOT OR. Two authors
                         ("author:a author:b") match nothing, so the checker
                         reports 0 forever and the watch looks permanently
                         clean. Verified against a live repo.
                     (2) Full-text terms match only what a PR happens to say.
                         Filtering on a ticket-key prefix drops PRs whose body
                         cites the ticket another way (an alternate id, or just
                         a prose reference), silently losing PRs you care about.
                     Prefer no `search` unless you have verified recall against
                     a known set of PRs that MUST match.
          "limit"  — max PRs to pull per tick (default 100).

Output: count|preview   (preview = "#<num> [state] title" of most-recently
                         updated changed PR; count = number of changed PRs)
        0|              (nothing changed since the watermark)
        ratelimited|r   (transient gh/API failure — triage SKIPS the tick rather
                         than burning a dispatch; see the 2026-07-24 storm note
                         in triage.sh check_quest)
        error|reason    (hard failure — triage treats this as dirty/retry)

Env:    GH_BIN   path to the gh CLI (default: resolved from PATH, then
                 /opt/homebrew/bin/gh)

Watermark lag: 30 seconds (see github_pr.lag). `gh search prs` reads GitHub's
search index, which is eventually consistent and can trail a write by seconds.
The lag covers that; it is kept small because every second of lag widens the
re-report window (a change caught inside it dispatches once more next tick).

State is deliberately NOT filtered to open PRs. A PR that merges bumps its
updatedAt at merge time — but if the query said `is:open`, the merge would drop
it out of the result set and the single most important signal (the fix landed)
would be silently missed. We take all states and let the worker interpret.

Sorting: we always pass --sort updated --order desc, so the newest-changed PRs
are first and we can stop at the first PR at/below the watermark. `gh` paginates
internally up to --limit; if the whole window is changed we may be truncating,
which is surfaced in the preview rather than hidden.
"""
import sys
import os
import json
import shutil
import subprocess
from datetime import datetime, timezone

GH = os.environ.get("GH_BIN") or shutil.which("gh") or "/opt/homebrew/bin/gh"

# gh exits non-zero for everything; classify from stderr instead. These markers
# mean "try again later", not "this is broken".
TRANSIENT_MARKERS = (
    "rate limit", "secondary rate", "abuse detection",
    "http 502", "http 503", "http 504", "bad gateway", "service unavailable",
    "timeout", "timed out", "deadline exceeded", "connection reset",
    "tls handshake", "temporary failure", "no such host", "eof",
)


class Transient(Exception):
    """Retryable upstream condition — skip the tick, don't dispatch."""


def gh_search(repo, extra, limit, timeout=30):
    cmd = [GH, "search", "prs", "--repo", repo,
           "--sort", "updated", "--order", "desc",
           "--limit", str(limit),
           "--json", "number,title,updatedAt,state,url"]
    if extra:
        # Positional search terms/qualifiers go before the flags gh parses.
        cmd = cmd[:3] + extra.split() + cmd[3:]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        err = (r.stderr or "").strip()
        low = err.lower()
        if any(m in low for m in TRANSIENT_MARKERS):
            raise Transient(err.splitlines()[-1] if err else "gh transient failure")
        raise RuntimeError(err.splitlines()[-1] if err else f"gh exit {r.returncode}")
    return json.loads(r.stdout)


def updated_epoch(pr):
    """Epoch seconds of a PR's updatedAt.

    Raises rather than returning a sentinel: a 0.0 fallback sorts to the bottom
    of a descending list and would trip the early stop, silently truncating the
    scan. A loud error (-> dirty) is the safe failure here.
    """
    s = pr.get("updatedAt")
    if not s:
        raise ValueError(f"#{pr.get('number', '?')}: no updatedAt in gh response")
    # gh returns RFC3339 Zulu, e.g. 2026-07-29T16:40:32Z (py3.11+ parses Z).
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()


def main():
    entry = json.loads(sys.argv[1])
    repo = entry["repo"]
    extra = entry.get("search") or ""
    limit = int(entry.get("limit") or 100)
    since_ts = float(entry.get("last_checked_ts") or 0)

    prs = gh_search(repo, extra, limit)

    changed = []
    for pr in prs:
        if updated_epoch(pr) > since_ts:
            changed.append(pr)
        else:
            # Descending order: this and everything after predates the watermark.
            break

    if not changed:
        print("0|")
        return

    top = changed[0]
    title = (top.get("title") or "")[:50]
    # count MUST stay a bare integer — triage compares it with `-gt`, so any
    # non-numeric decoration makes the test fail and the quest read clean,
    # silently swallowing the dispatch. Truncation warning goes in the preview.
    more = f" (+more, limit {limit} hit)" if len(changed) >= limit else ""
    print(f"{len(changed)}|#{top.get('number','?')} [{top.get('state','?')}] {title}{more}")


if __name__ == "__main__":
    try:
        main()
    except Transient as e:
        # Not dirty: skip the tick. Watermark is held, so nothing is lost.
        print(f"ratelimited|{e}")
    except Exception as e:
        print(f"error|{e}")
