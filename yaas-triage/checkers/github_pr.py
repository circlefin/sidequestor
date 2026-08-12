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
                         than burning a dispatch; see the 2026-07-24 storm note
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

# A repo that does not exist, or that this token cannot see. Retrying cannot fix either, so
# these are misconfig (hold, page a human) rather than error (exponential backoff, then
# promote to misconfig anyway after burning a day of retries).
NOT_FOUND_MARKERS = (
    "could not resolve to a repository",
    "cannot be searched either because the resources do not exist",
    "http 404", "not found",
)


class Transient(Exception):
    """Retryable upstream condition — skip the tick, don't dispatch."""


class Misconfig(Exception):
    """Permanent condition needing a human — never dispatch, never back off."""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result


def gh_env(account):
    """Environment for the gh call, optionally pinned to a non-active account.

    GH_TOKEN wins over the active account's keyring entry, so this switches
    identity for one subprocess without mutating global gh state (which
    `gh auth switch` would, breaking every other repo in the session).
    """
    env = dict(os.environ)
    if not account:
        return env
    r = subprocess.run([GH, "auth", "token", "-u", account],
                       capture_output=True, text=True, timeout=15)
    token = (r.stdout or "").strip()
    if r.returncode != 0 or not token:
        err = (r.stderr or "").strip() or f"gh auth token exit {r.returncode}"
        raise Misconfig(f"no gh token for account {account!r}: {err}")
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def _search_tokens(extra):
    """Split a watch's `search` string into argv tokens, refusing gh FLAGS.

    The tokens are spliced in ahead of the flags gh parses, so anything starting with `-`
    lands as a real flag rather than a search qualifier — `--owner`, `--json`, and `--limit`
    all silently change what the result means, and on the sibling github_issue checker
    `--include-prs` would make two watches report the same PR twice. Qualifiers never need a
    leading dash, so refusing them costs nothing. Negated qualifiers (`-label:bug`) need a
    `--` separator gh cannot receive through this field; write the positive form instead.
    """
    tokens = extra.split()
    flags = [t for t in tokens if t.startswith("-")]
    if flags:
        raise Misconfig(
            f"\"search\" may only contain search qualifiers, not gh flags: {' '.join(flags)}")
    return tokens


def gh_search(repo, extra, limit, env=None, timeout=30, since_iso=None, order="desc"):
    cmd = [GH, "search", "prs", "--repo", repo,
           "--sort", "updated", "--order", order,
           "--limit", str(limit),
           "--json", "number,title,updatedAt,state,url"]
    if since_iso:
        # Bound the LOW end. Without this the query returns the newest N overall, which on
        # a repo busier than `limit` is a SUFFIX of the gap — and a watermark can never
        # cross a suffix, because the unread part sits directly above it. Bounding the low
        # end and sorting ASCENDING makes the result a PREFIX instead, which the watermark
        # can cross, so the backlog shrinks every tick instead of livelocking.
        cmd.append(f"updated:>={since_iso}")
    if extra:
        # Positional search terms/qualifiers go before the flags gh parses.
        cmd = cmd[:3] + _search_tokens(extra) + cmd[3:]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        err = (r.stderr or "").strip()
        low = err.lower()
        if any(m in low for m in TRANSIENT_MARKERS):
            raise Transient(err.splitlines()[-1] if err else "gh transient failure")
        if any(m in low for m in NOT_FOUND_MARKERS):
            raise Misconfig(err.splitlines()[-1] if err else "repo not found or not visible")
        raise RuntimeError(err.splitlines()[-1] if err else f"gh exit {r.returncode}")
    return json.loads(r.stdout)


def _drained(rows, limit):
    """Did this page reach the end of the gap? (Purely informational now.)

    With a bounded, ascending query a short page means the gap is exhausted. A FULL page
    still means we hold a contiguous prefix of the gap, which is safe to commit — see the
    note on `complete` in main().
    """
    return len(rows) < limit


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
    env = gh_env(entry.get("gh_account"))

    # Ask for the OLDEST unseen changes first, bounded below by the watermark. One second
    # is subtracted because GitHub's `updated:>=` is inclusive and coarse; the post-filter
    # below re-applies the exact boundary.
    since_iso = None
    if since_ts > 0:
        since_iso = datetime.fromtimestamp(since_ts - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = gh_search(repo, extra, limit, env, since_iso=since_iso, order="asc")
    changed = [pr for pr in rows if updated_epoch(pr) > since_ts]
    drained = _drained(rows, limit)

    # ── Tie safety. Ascending order alone does NOT make a capped page a safe prefix ──────
    # If the page fills up partway through timestamp T, advancing to T means the next run
    # filters `> T` and permanently skips the rows at T we never saw. Ordering by timestamp
    # gives no tiebreak, so on a capped page the only provable boundary is the last timestamp
    # we hold IN FULL: anything strictly below the final row's timestamp.
    if not drained and rows:
        if not changed:
            # Every row is at or below the watermark, yet the page filled. The boundary
            # timestamp therefore holds more rows than one page. Reporting clean+complete here
            # would let triage advance this watch to now-lag and skip everything past the
            # page, so hold instead.
            result.emit("hold", count=0, preview="", complete=False,
                        reason=(f"a full page of {limit} rows produced nothing past the "
                                f"watermark; the boundary timestamp spans more than one page "
                                f"— raise \"limit\" above {limit} for {repo}"))
            return
        boundary = updated_epoch(rows[-1])
        safe = [pr for pr in changed if updated_epoch(pr) < boundary]
        if not safe:
            # Every NEW row sits at the boundary timestamp, so no advance is provable.
            result.emit("hold", count=len(changed), preview="", complete=False,
                        reason=(f"all {len(changed)} new rows share updatedAt "
                                f"{rows[-1].get('updatedAt')} on a full page; raise \"limit\" "
                                f"above {limit} for {repo} or the watermark cannot advance"))
            return
        changed = safe

    if not changed:
        result.counted(0, "", complete=True)
        return

    # Ascending, so the LAST row is the newest we can prove we hold in full.
    newest = changed[-1]
    title = (newest.get("title") or "")[:50]

    # `complete` means "everything up to advance_to has been seen", NOT "the whole gap is
    # done" — advance_to bounds the claim, the same convention slack_utils.drain() uses for a
    # covered forward slice. With the tie trimming above, what remains IS a contiguous prefix,
    # so committing to `newest` is safe and the backlog shrinks every tick.
    complete = True

    more = "" if drained else f" (+backlog; oldest {len(changed)} first)"
    # count MUST stay a bare integer — triage compares it with `-gt`, so any non-numeric
    # decoration makes the test fail and the quest read clean, silently swallowing the
    # dispatch. Truncation goes in the preview.
    result.counted(len(changed),
                   f"#{newest.get('number','?')} [{newest.get('state','?')}] {title}{more}",
                   advance_to=updated_epoch(newest), complete=complete)


if __name__ == "__main__":
    try:
        main()
    except Transient as e:
        # Not dirty: skip the tick. Watermark is held, so nothing is lost.
        result.ratelimited(str(e))
    except Misconfig as e:
        result.misconfig(str(e))
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
