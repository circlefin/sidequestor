"""Shared bounded-query logic for X API checkers."""

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import result


SURFACE = os.environ.get(
    "X_CALL", str(Path(__file__).resolve().parents[1] / "surfaces" / "x-call.py"),
)
PAGE_SIZE = 100
MAX_PAGES = 5
SLICE_ATTEMPTS = 20
WALL_BUDGET_SECONDS = 20


class Transient(Exception):
    pass


class Misconfig(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def _iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _call(path, params, auth_spec, timeout=20):
    try:
        proc = subprocess.run(
            [sys.executable, SURFACE, "GET", path,
             json.dumps(params, separators=(",", ":")), auth_spec],
            capture_output=True, text=True, timeout=max(0.1, timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise BudgetExceeded("X query exceeded the checker wall-time budget") from exc
    detail = (proc.stderr or "").strip()[:250]
    if proc.returncode == 4:
        raise Transient(detail or "X API transient failure")
    if proc.returncode in (1, 3):
        raise Misconfig(detail or "X credential/query is invalid")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(detail or f"x-call exit {proc.returncode}")
    value = json.loads(proc.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("X API returned an invalid response")
    if value.get("errors"):
        raise RuntimeError(f"X API returned partial errors: {str(value['errors'])[:180]}")
    return value


def _fetch_window(path, fixed, auth_spec, since, upper, deadline):
    rows = []
    token = None
    for _page in range(MAX_PAGES):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            return rows, False
        params = dict(fixed)
        params.update({"start_time": _iso(max(0, math.floor(since))),
                       # X end_time is exclusive. Asking through upper+1 covers
                       # every post in the whole second committed as `upper`.
                       "end_time": _iso(upper + 1), "max_results": PAGE_SIZE})
        if token:
            params["pagination_token"] = token
        try:
            value = _call(path, params, auth_spec, timeout=min(20, remaining))
        except BudgetExceeded:
            return rows, False
        rows.extend(value.get("data") or [])
        token = (value.get("meta") or {}).get("next_token")
        if not token:
            return rows, True
    return rows, False


def _matches(row, entry):
    excluded = {str(value) for value in entry.get("exclude_user_ids", [])}
    if str(row.get("author_id", "")) in excluded:
        return False
    keywords = [str(value).lower() for value in entry.get("filter_keywords", []) if str(value)]
    text = str(row.get("text", ""))
    return not keywords or any(keyword in text.lower() for keyword in keywords)


def _string_list(entry, key):
    value = entry.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, (str, int)) for item in value):
        raise Misconfig(f"{key} must be a list of strings or integers")
    return [str(item) for item in value]


def lag_for(watch_type):
    try:
        value = int(Path(__file__).with_name(f"{watch_type}.lag").read_text().strip())
    except (OSError, ValueError) as exc:
        raise Misconfig(f"{watch_type}.lag is missing or invalid") from exc
    return value


def _gap_reason(gap_seconds, max_age):
    """Name a clamped-away interval so the loss is visible in the row, not silent."""
    if not gap_seconds:
        return ""
    return (f"skipped {gap_seconds}s of unrecoverable history: X recent search retains only "
            f"{int(max_age)} seconds, so the watermark was clamped forward to that floor")


def run(entry, path, fixed=None, lag=0, now=None, max_age=None):
    try:
        since = float(entry.get("last_checked_ts") or 0)
    except (TypeError, ValueError) as exc:
        raise Misconfig("last_checked_ts must be an epoch number") from exc
    if since <= 0:
        raise Misconfig("last_checked_ts must be greater than zero")
    _string_list(entry, "exclude_user_ids")
    _string_list(entry, "filter_keywords")
    current = float(now if now is not None else time.time())
    retention_margin = max(60, lag)
    gap_seconds = 0
    if max_age:
        # Recent search cannot serve anything older than max_age, so a watermark that has
        # fallen behind that floor names posts which no longer exist to be fetched. Clamp
        # forward and say so: the interval is unrecoverable either way, and clamping lets
        # the watch resume. Raising Misconfig here instead stranded it PERMANENTLY, because
        # nothing ever advances a misconfigured watch's cursor (tick_check: "permanently
        # stuck; hold and page a human") — so an operator who left `x` out of
        # YAAS_CHECKER_CONNECTORS for eight days, or slept the machine for a week, came
        # back to a watch that could only be repaired by hand.
        floor_ts = current - (max_age - retention_margin)
        if since < floor_ts:
            gap_seconds = int(floor_ts - since)
            since = floor_ts
    ceiling = math.floor(current - max(0, lag))
    if ceiling <= since:
        result.emit(result.CLEAN, advance_to=since, complete=True,
                    reason=_gap_reason(gap_seconds, max_age))
        return
    upper = ceiling
    credential_id = str(entry.get("credential_id") or "default")
    auth_spec = f"app:{credential_id}"
    deadline = time.monotonic() + WALL_BUDGET_SECONDS

    for _attempt in range(SLICE_ATTEMPTS):
        if time.monotonic() >= deadline:
            result.emit("hold", complete=False, reason="X slice budget exhausted; cursor held")
            return
        rows, complete = _fetch_window(path, fixed or {}, auth_spec, since, upper, deadline)
        if complete:
            unique = {str(row.get("id")): row for row in rows if row.get("id") is not None}
            changed = [row for row in unique.values()
                       if since < _epoch(row.get("created_at")) <= upper and _matches(row, entry)]
            changed.sort(key=lambda row: (_epoch(row["created_at"]), int(row["id"])))
            newest = changed[-1] if changed else None
            preview = ""
            if newest:
                preview = f"@{newest.get('author_id', '?')}: {' '.join(str(newest.get('text', '')).split())[:140]}"
            result.emit(result.DIRTY if changed else result.CLEAN, count=len(changed),
                        preview=preview, advance_to=upper, complete=True,
                        reason=_gap_reason(gap_seconds, max_age))
            return
        narrowed = math.floor(since + ((upper - since) / 2.0))
        if narrowed <= since or upper - since <= 1:
            raise Misconfig("X result density exceeds the smallest coverable time slice")
        upper = narrowed
    result.emit("hold", complete=False, reason="X backlog could not be sliced safely")


def cli(entry, **kwargs):
    try:
        run(entry, **kwargs)
    except Transient as exc:
        result.ratelimited(str(exc))
    except Misconfig as exc:
        result.misconfig(str(exc))
    except Exception as exc:
        result.error(f"{type(exc).__name__}: {exc}")
