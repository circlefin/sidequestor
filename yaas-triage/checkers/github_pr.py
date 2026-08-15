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
                     QUALIFIERS ONLY: a token starting with `-` is refused as
                     misconfig — the string is spliced into argv ahead of gh's own
                     flags, so a leading dash lands as a real flag and silently
                     changes what was searched. See _search_tokens().
          "limit"  — max PRs to pull per tick (default 100).
          "gh_account" — a `gh` account login whose token to use, e.g.
                     "octocat-work". Needed when the repo is private to a
                     second GitHub account and the ACTIVE gh account cannot see
                     it: without this the search 404s, which gh reports as a hard
                     error every tick. Resolved per-run via `gh auth token -u`
                     and passed as GH_TOKEN, so it does not mutate global gh
                     state; the token is never written to disk or state.

Output: count|preview   (preview = "#<num> [state] title" of most-recently
                         updated changed PR; count = number of changed PRs)
        0|              (nothing changed since the watermark)
        ratelimited|r   (transient gh/API failure — triage SKIPS the tick rather
                         than burning a dispatch; see the rate-limit note
                         in the original shell orchestrator check_quest)
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
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import github


def _search_tokens(extra):
    """Split a watch's `search` string into argv tokens, refusing gh FLAGS.

    The tokens are spliced in ahead of the flags gh parses, so anything starting with `-`
    lands as a real flag rather than a search qualifier — `--owner`, `--json`, and `--limit`
    all silently change what the result means, and on the sibling github_issue checker
    `--include-prs` would make two watches report the same PR twice. Qualifiers never need a
    leading dash, so refusing them costs nothing. Negated qualifiers (`-label:bug`) need a
    `--` separator gh cannot receive through this field; write the positive form instead.
    """
    return github.search_tokens(
        extra,
        lambda flags: f"\"search\" may only contain search qualifiers, not gh flags: {' '.join(flags)}",
    )


def _preview(pr, drained, changed_count):
    title = (pr.get("title") or "")[:50]
    more = "" if drained else f" (+backlog; oldest {changed_count} first)"
    return f"#{pr.get('number','?')} [{pr.get('state','?')}] {title}{more}"


def main():
    github.run_watch(
        json.loads(sys.argv[1]),
        kind="prs",
        json_fields="number,title,updatedAt,state,url",
        tokenise=_search_tokens,
        preview_fn=_preview,
    )


if __name__ == "__main__":
    github.cli(main)
