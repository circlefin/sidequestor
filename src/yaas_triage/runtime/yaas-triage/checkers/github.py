import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import result


GH = os.environ.get("GH_BIN") or shutil.which("gh") or "/opt/homebrew/bin/gh"

TRANSIENT_MARKERS = (
    "rate limit", "secondary rate", "abuse detection",
    "http 502", "http 503", "http 504", "bad gateway", "service unavailable",
    "timeout", "timed out", "deadline exceeded", "connection reset",
    "tls handshake", "temporary failure", "no such host", "eof",
)

NOT_FOUND_MARKERS = (
    "could not resolve to a repository",
    "cannot be searched either because the resources do not exist",
    "http 404", "not found",
)


class Transient(Exception):
    """Retryable upstream condition — skip the tick, don't dispatch."""


class Misconfig(Exception):
    """Permanent condition needing a human — never dispatch, never back off."""


def gh_env(account):
    """Environment for the gh call, optionally pinned to a non-active account."""
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


def search_tokens(extra, error_message):
    """Split a watch's search string into argv tokens, refusing gh flags."""
    tokens = extra.split()
    flags = [t for t in tokens if t.startswith("-")]
    if flags:
        raise Misconfig(error_message(flags))
    return tokens


def gh_search(kind, repo, extra, limit, json_fields, env, tokenise, timeout=30,
              since_iso=None, order="desc"):
    cmd = [GH, "search", kind, "--repo", repo,
           "--sort", "updated", "--order", order,
           "--limit", str(limit),
           "--json", json_fields]
    if since_iso:
        cmd.append(f"updated:>={since_iso}")
    if extra:
        cmd = cmd[:3] + tokenise(extra) + cmd[3:]
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


def drained(rows, limit):
    return len(rows) < limit


def updated_epoch(row):
    s = row.get("updatedAt")
    if not s:
        raise ValueError(f"#{row.get('number', '?')}: no updatedAt in gh response")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()


def since_iso(since_ts):
    if since_ts <= 0:
        return None
    return datetime.fromtimestamp(since_ts - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_watch(entry, kind, json_fields, tokenise, preview_fn):
    repo = entry["repo"]
    extra = entry.get("search") or ""
    limit = int(entry.get("limit") or 100)
    since_ts = float(entry.get("last_checked_ts") or 0)
    env = gh_env(entry.get("gh_account"))

    rows = gh_search(kind, repo, extra, limit, json_fields, env, tokenise,
                     since_iso=since_iso(since_ts), order="asc")
    changed = [row for row in rows if updated_epoch(row) > since_ts]
    is_drained = drained(rows, limit)

    if not is_drained and rows:
        if not changed:
            result.emit("hold", count=0, preview="", complete=False,
                        reason=(f"a full page of {limit} rows produced nothing past the "
                                f"watermark; the boundary timestamp spans more than one page "
                                f"— raise \"limit\" above {limit} for {repo}"))
            return
        boundary = updated_epoch(rows[-1])
        safe = [row for row in changed if updated_epoch(row) < boundary]
        if not safe:
            result.emit("hold", count=len(changed), preview="", complete=False,
                        reason=(f"all {len(changed)} new rows share updatedAt "
                                f"{rows[-1].get('updatedAt')} on a full page; raise \"limit\" "
                                f"above {limit} for {repo} or the watermark cannot advance"))
            return
        changed = safe

    if not changed:
        result.counted(0, "", complete=True)
        return

    newest = changed[-1]
    result.counted(len(changed), preview_fn(newest, is_drained, len(changed)),
                   advance_to=updated_epoch(newest), complete=True)


def cli(main_fn):
    try:
        main_fn()
    except Transient as e:
        result.ratelimited(str(e))
    except Misconfig as e:
        result.misconfig(str(e))
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
