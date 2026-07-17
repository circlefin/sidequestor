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
        error|reason    (on gws failure — triage treats this as dirty/retry)

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


def gws_run(*args, timeout=20):
    r = subprocess.run([GWS] + list(args), capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"gws {' '.join(args[:3])} failed (exit {r.returncode})")
    return json.loads(r.stdout)


def main():
    entry = json.loads(sys.argv[1])
    query = entry["query"]
    since_ts = float(entry.get("last_checked_ts", "0"))
    since_ms = since_ts * 1000

    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc) - timedelta(days=1)
    since_date = since_dt.strftime("%Y/%m/%d")
    full_query = f"{query} after:{since_date}"

    try:
        d = gws_run("gmail", "users", "messages", "list",
                    "--params", json.dumps({"userId": "me", "q": full_query, "maxResults": 10}))
    except RuntimeError as e:
        print(f"error|{e}")
        return

    msgs = d.get("messages", [])
    if not msgs:
        print("0|")
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
                })
        except Exception:
            pass

    if not new_msgs:
        print("0|")
        return

    preview = f"{new_msgs[0]['from']} — {new_msgs[0]['subject']}"
    print(f"{len(new_msgs)}|{preview}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error|{e}")
