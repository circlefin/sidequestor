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
        # A missing/logged-out account is permanent until a human logs in again.
        raise Misconfig(f"no gh token for account {account!r}: {err}")
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


class Misconfig(Exception):
    """Permanent condition needing a human — never dispatch, never back off."""


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
    tokens = extra.split()
    flags = [t for t in tokens if t.startswith("-")]
    if flags:
        raise Misconfig(
            f"\"search\" may only contain search qualifiers, not gh flags: {' '.join(flags)}. "
            f"A flag here can change what is searched (e.g. --include-prs puts PRs back into "
            f"an issues query, double-reporting with a github_pr watch on the same repo).")
    return tokens


def gh_search(repo, extra, limit, env, timeout=30, since_iso=None, order="desc"):
    cmd = [GH, "search", "issues", "--repo", repo,
           "--sort", "updated", "--order", order,
           "--limit", str(limit),
           "--json", "number,title,updatedAt,state,url,author,labels"]
    if since_iso:
        # Bound the LOW end so the page is a PREFIX of the gap, not a suffix — a
        # watermark can never cross a suffix. Same reasoning as github_pr.py.
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
    """Did this page reach the end of the gap? (Purely informational.)"""
    return len(rows) < limit


def updated_epoch(issue):
    """Epoch seconds of an issue's updatedAt.

    Raises rather than returning a sentinel: a 0.0 fallback would sort to the
    bottom and silently truncate the scan.
    """
    s = issue.get("updatedAt")
    if not s:
        raise ValueError(f"#{issue.get('number', '?')}: no updatedAt in gh response")
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

    # Oldest unseen first, bounded below by the watermark. One second is subtracted
    # because GitHub's `updated:>=` is inclusive and coarse; the post-filter below
    # re-applies the exact boundary.
    since_iso = None
    if since_ts > 0:
        since_iso = datetime.fromtimestamp(since_ts - 1, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    rows = gh_search(repo, extra, limit, env, since_iso=since_iso, order="asc")
    changed = [i for i in rows if updated_epoch(i) > since_ts]
    drained = _drained(rows, limit)

    # ── Tie safety ──────────────────────────────────────────────────────────────
    # A full page may stop partway through timestamp T; advancing to T would skip
    # the rows at T we never saw. The only provable boundary on a capped page is
    # strictly below the final row's timestamp.
    if not drained and rows:
        if not changed:
            result.emit("hold", count=0, preview="", complete=False,
                        reason=(f"a full page of {limit} rows produced nothing past the "
                                f"watermark; the boundary timestamp spans more than one "
                                f"page — raise \"limit\" above {limit} for {repo}"))
            return
        boundary = updated_epoch(rows[-1])
        safe = [i for i in changed if updated_epoch(i) < boundary]
        if not safe:
            result.emit("hold", count=len(changed), preview="", complete=False,
                        reason=(f"all {len(changed)} new rows share updatedAt "
                                f"{rows[-1].get('updatedAt')} on a full page; raise "
                                f"\"limit\" above {limit} for {repo} or the watermark "
                                f"cannot advance"))
            return
        changed = safe

    if not changed:
        result.counted(0, "", complete=True)
        return

    # Ascending, so the LAST row is the newest we can prove we hold in full.
    newest = changed[-1]
    title = (newest.get("title") or "")[:50]
    author = ((newest.get("author") or {}).get("login")) or "?"

    more = "" if drained else f" (+backlog; oldest {len(changed)} first)"
    # count MUST stay a bare integer — triage compares it numerically.
    result.counted(len(changed),
                   f"#{newest.get('number','?')} [{newest.get('state','?')}] "
                   f"{title} (@{author}){more}",
                   advance_to=updated_epoch(newest), complete=True)


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
