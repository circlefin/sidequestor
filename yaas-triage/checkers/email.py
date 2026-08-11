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
checkers/email.py — check Gmail for messages matching a query since watermark.

Input:  watch entry JSON as argv[1]
        {"type":"email","query":"from:...","last_checked_ts":"1234.567","reason":"..."}

Output: count|preview   (preview = "From — Subject" of newest new message)
        ratelimited|r   (transient Gmail 429/5xx/quota/timeout — triage SKIPS the
                        tick rather than dispatching; watermark is held)
        error|reason    (hard failure — triage treats this as dirty/retry)

Env:    GWS_BIN   path to gws CLI (falls back to /opt/homebrew/bin/gws)

Watermark lag: 120 seconds (see email.lag). Triage reads the lag file and
subtracts it when advancing the watermark, giving Gmail's search index time
to catch up before a clean tick claims "nothing new".

Precision: fetches metadata for each list result and post-filters by
internalDate/1000 > last_checked_ts — no false positives from the day-boundary
overlap in the after:YYYY/MM/DD query filter.
"""
import sys
import os
import json
import subprocess
from datetime import datetime, timezone, timedelta

GWS = os.environ.get("GWS_BIN", "/opt/homebrew/bin/gws")

# gws prints the Google API error envelope to STDOUT as
# {"error":{"code":429,"message":...,"reason":...}} and exits 1, so classify on
# the HTTP code rather than string-matching stderr. These mean "try again
# later", not "this is broken".
TRANSIENT_CODES = {429, 500, 502, 503, 504}
# Gmail returns 403 for quota exhaustion, which IS retryable — unlike a 403 for
# insufficient scope, which is not. Split on `reason`.
TRANSIENT_REASONS = {
    "ratelimitexceeded", "userratelimitexceeded", "quotaexceeded",
    "backenderror", "internalerror", "serviceunavailable",
}
# Network-layer failures never produce an error envelope; match on the text.
TRANSIENT_MARKERS = (
    "timeout", "timed out", "deadline exceeded", "connection reset",
    "tls handshake", "temporary failure", "no such host", "eof",
    "connection refused", "network is unreachable",
)


class Transient(Exception):
    """Retryable upstream condition — skip the tick, don't dispatch."""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result

def gws_run(*args, timeout=20):
    label = f"gws {' '.join(args[:3])}"
    try:
        r = subprocess.run([GWS] + list(args), capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Transient(f"{label} timed out after {timeout}s")

    out = r.stdout.strip()
    if r.returncode == 0 and out:
        return json.loads(out)

    # Prefer the structured error envelope on stdout; fall back to stderr text.
    err = None
    if out:
        try:
            err = (json.loads(out) or {}).get("error")
        except json.JSONDecodeError:
            pass

    if isinstance(err, dict):
        code = err.get("code")
        reason = str(err.get("reason", "")).lower()
        detail = f"{label}: {code} {err.get('message', '')}".strip()
        if code in TRANSIENT_CODES or reason in TRANSIENT_REASONS:
            raise Transient(detail)
        raise RuntimeError(detail)

    blob = f"{out} {r.stderr or ''}".lower()
    detail = f"{label} failed (exit {r.returncode})"
    if any(m in blob for m in TRANSIENT_MARKERS):
        raise Transient(detail)
    raise RuntimeError(detail)


PAGE_LIMIT = 50   # was 10. See the complete= note below.


def main():
    entry = json.loads(sys.argv[1])
    query = entry["query"]
    since_ts = float(entry.get("last_checked_ts", "0"))
    since_ms = since_ts * 1000

    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc) - timedelta(days=1)
    since_date = since_dt.strftime("%Y/%m/%d")
    full_query = f"{query} after:{since_date}"

    d = gws_run("gmail", "users", "messages", "list",
                "--params", json.dumps({"userId": "me", "q": full_query, "maxResults": PAGE_LIMIT}))

    msgs = d.get("messages", [])
    if not msgs:
        result.counted(0, "")
        return

    new_msgs = []
    for m in msgs:
        try:
            md = gws_run("gmail", "users", "messages", "get",
                         "--params", json.dumps({"userId": "me", "id": m["id"],
                                                 "format": "metadata"}),
                         timeout=10)
            if int(md.get("internalDate", "0")) > since_ms:
                hdrs = {h["name"]: h["value"]
                        for h in md.get("payload", {}).get("headers", [])}
                new_msgs.append({
                    "from": hdrs.get("From", "")[:40],
                    "subject": hdrs.get("Subject", "")[:50],
                    "ts": int(md.get("internalDate", "0")) / 1000.0,
                })
        except Transient:
            # Must NOT be swallowed. Skipping a message here undercounts, and an
            # undercount to zero prints "0|" -> clean tick -> triage advances the
            # watermark past a message nobody ever read. Losing the email is
            # worse than losing the tick, so surface it and let triage hold.
            raise
        except Exception:
            pass

    # A full page does not prove there is nothing older: Gmail returns newest-first,
    # so a saturated window means older matching messages may be unseen. Report
    # complete=false and triage holds the cursor instead of skipping them.
    complete = len(msgs) < PAGE_LIMIT
    newest = max((m["ts"] for m in new_msgs), default=None)
    if not new_msgs:
        result.counted(0, "", complete=complete)
        return

    preview = f"{new_msgs[0]['from']} — {new_msgs[0]['subject']}"
    result.counted(len(new_msgs), preview, advance_to=newest, complete=complete)


if __name__ == "__main__":
    try:
        main()
    except Transient as e:
        # Not dirty: skip the tick. Watermark is held, so nothing is lost.
        result.ratelimited(str(e))
    except Exception as e:
        result.error(f"{type(e).__name__}: {e}")
