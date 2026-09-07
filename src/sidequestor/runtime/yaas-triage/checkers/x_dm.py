#!/usr/bin/env python3
"""Watch incoming direct-message events visible to the authorized X user."""

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x

MAX_PAGES = 5
PAGE_SIZE = 100
MAX_AGE = 30 * 24 * 60 * 60


def run(entry, now=None, lag=30):
    try:
        since = float(entry.get("last_checked_ts") or 0)
    except (TypeError, ValueError) as exc:
        raise x.Misconfig("last_checked_ts must be an epoch number") from exc
    if since <= 0:
        raise x.Misconfig("last_checked_ts must be greater than zero")
    current = float(time.time() if now is None else now)
    floor = current - MAX_AGE + max(60, lag)
    reason = ""
    if since < floor:
        since = floor
        reason = "skipped direct-message history older than X's 30-day API window"
    upper = math.floor(current - max(0, lag))
    if upper <= since:
        x.result.emit(x.result.CLEAN, advance_to=since, complete=True, reason=reason)
        return
    credential_id = str(entry.get("credential_id") or "default")
    auth = f"user:{credential_id}"
    owner_id = x.current_user_id(entry)
    conversation_id = str(entry.get("conversation_id") or "")
    participant_id = str(entry.get("participant_id") or "")
    keywords = x._string_list(entry, "filter_keywords")
    rows = []
    token = None
    complete = False
    for _page in range(MAX_PAGES):
        params = {
            "max_results": PAGE_SIZE,
            "dm_event.fields": "id,event_type,text,sender_id,created_at,dm_conversation_id,participant_ids",
        }
        if token:
            params["pagination_token"] = token
        value = x._call("/2/dm_events", params, auth)
        page = value.get("data") or []
        rows.extend(page)
        timestamps = [x._epoch(row["created_at"]) for row in page if row.get("created_at")]
        token = (value.get("meta") or {}).get("next_token")
        if not token or (timestamps and min(timestamps) <= since):
            complete = True
            break
    if not complete:
        x.result.emit("hold", complete=False, reason="X DM page budget exhausted; cursor held")
        return
    changed = []
    for row in rows:
        created = x._epoch(row.get("created_at"))
        if not since < created <= upper or str(row.get("sender_id") or "") == owner_id:
            continue
        if conversation_id and str(row.get("dm_conversation_id") or "") != conversation_id:
            continue
        participants = {str(value) for value in row.get("participant_ids") or []}
        if participant_id and participant_id not in participants and str(row.get("sender_id") or "") != participant_id:
            continue
        text = str(row.get("text") or "")
        if keywords and not any(keyword.lower() in text.lower() for keyword in keywords):
            continue
        changed.append(row)
    unique = {str(row.get("id")): row for row in changed if row.get("id") is not None}
    newest = max(unique.values(), key=lambda row: x._epoch(row["created_at"])) if unique else None
    preview = ""
    if newest:
        preview = f"DM from {newest.get('sender_id', '?')}: {' '.join(str(newest.get('text', '')).split())[:140]}"
    x.result.emit(x.result.DIRTY if unique else x.result.CLEAN, count=len(unique), preview=preview,
                  advance_to=upper, complete=True, reason=reason)


def main():
    run(json.loads(sys.argv[1]), lag=x.lag_for("x_dm"))


if __name__ == "__main__":
    try:
        main()
    except x.Transient as exc:
        x.result.ratelimited(str(exc))
    except (x.Misconfig, KeyError, ValueError, TypeError) as exc:
        x.result.misconfig(str(exc))
    except Exception as exc:
        x.result.error(f"{type(exc).__name__}: {exc}")
