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
checkers/github_issue.py — check a repo's ISSUES for activity since the watermark.

Sibling of checkers/github_pr.py; same watermark/tie/drain doctrine, different
search surface. `gh search issues` excludes pull requests unless --include-prs is
passed, so the two watches do not double-report the same item.

Input:  watch entry JSON as argv[1]
        {"type":"github_issue","repo":"owner/repo",
         "last_checked_ts":"1234.567","reason":"..."}
        Optional:
          "search"      — extra GitHub search qualifiers, e.g. "label:bug" or
                          "is:open". Read the two traps in github_pr.py first:
                          repeated qualifiers AND rather than OR (so
                          "author:a author:b" matches nothing and the watch
                          reports clean forever), and full-text terms only match
                          what an issue happens to say. `is:open` also hides the
                          close event, which is often the signal you want.
                          QUALIFIERS ONLY: a token starting with `-` is refused as
                          misconfig, because this string is spliced into argv ahead
                          of gh's own flags and would land as a real one. See
                          _search_tokens() for why --include-prs in particular is
                          worth refusing.
          "limit"       — max issues to pull per tick (default 100).
          "gh_account"  — a `gh` account login whose token to use, e.g.
                          "octocat-work". Needed when the repo is private
                          to a second GitHub account and the ACTIVE gh account
                          cannot see it: without this the search 404s, which gh
                          reports as a hard error every tick. Resolved per-run via
                          `gh auth token -u <login>` and passed as GH_TOKEN; the
                          token is never written to disk or to any state file.

Output: one line of checkers/result.py JSON — see that module.

Env:    GH_BIN   path to the gh CLI (default: resolved from PATH, then
                 /opt/homebrew/bin/gh)

Watermark lag: 30 seconds (github_issue.lag), same rationale as github_pr — the
GitHub search index is eventually consistent and can trail a write by seconds.

State is deliberately NOT filtered to open issues: an issue that closes bumps its
updatedAt at close time, and `is:open` would drop it out of the result set right
when the most important signal fires. Take all states, let the worker interpret.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import github


def _search_tokens(extra):
    """Split a watch's `search` string into argv tokens, refusing gh FLAGS.

    The tokens are spliced into the gh command line ahead of the flags gh parses, so a
    `search` value is not merely a set of search qualifiers — anything starting with `-`
    lands as a real flag. `search: "--include-prs"` would put pull requests back into an
    `issues` query, and every PR would then be reported twice: once here and once by the
    sibling github_pr watch on the same repo, paying for two dispatches on one event.
    Other flags are worse (`--owner`, `--json`, `--limit` all silently change the meaning of
    the result). Qualifiers never need a leading dash, so refusing them costs nothing.

    Negated qualifiers (`-label:bug`) are refused too. gh needs a `--` separator for those,
    which would itself have to be smuggled through this field; write the positive qualifier
    instead, or filter in the worker.
    """
    return github.search_tokens(
        extra,
        lambda flags: (
            f"\"search\" may only contain search qualifiers, not gh flags: {' '.join(flags)}. "
            f"A flag here can change what is searched (e.g. --include-prs puts PRs back into "
            f"an issues query, double-reporting with a github_pr watch on the same repo)."
        ),
    )


def _preview(issue, drained, changed_count):
    title = (issue.get("title") or "")[:50]
    author = ((issue.get("author") or {}).get("login")) or "?"
    more = "" if drained else f" (+backlog; oldest {changed_count} first)"
    return f"#{issue.get('number','?')} [{issue.get('state','?')}] {title} (@{author}){more}"


def main():
    github.run_watch(
        json.loads(sys.argv[1]),
        kind="issues",
        json_fields="number,title,updatedAt,state,url,author,labels",
        tokenise=_search_tokens,
        preview_fn=_preview,
    )


if __name__ == "__main__":
    github.cli(main)
