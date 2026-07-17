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
cron-due.py — decide whether a cron schedule is due to fire.

Usage: cron-due.py CRON_EXPR TZ LAST_TS

    CRON_EXPR  Standard 5-field cron (minute hour dom month dow).
               Supports *, */N, N, N-M, N,M. Vixie OR semantics when both
               dom and dow are set.
    TZ         IANA timezone (e.g., "Asia/Singapore"). Empty = UTC.
    LAST_TS    Previous last_checked_ts: unix epoch (Slack-style "123.456"
               or plain int) or ISO 8601. Empty = "never run" → not-due.

Output: "due" or "not-due". Exit 0 on success, 2 on bad input.

Semantics: due iff ANY cron-matching datetime exists in (LAST_TS, NOW].
Missed fires are collapsed into a single "due" — caller advances
last_checked_ts to NOW on success, so we fire at most once per tick even
after long downtime.
"""
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


def parse_field(field, lo, hi):
    vals = set()
    for part in field.split(','):
        step = 1
        base = part
        if '/' in part:
            base, step_s = part.split('/', 1)
            step = int(step_s)
        if base == '*':
            start, end = lo, hi
        elif '-' in base:
            a, b = base.split('-', 1)
            start, end = int(a), int(b)
        else:
            v = int(base)
            start = end = v
        vals.update(range(start, end + 1, step))
    vals = {v for v in vals if lo <= v <= hi}
    if not vals:
        raise ValueError(f"no valid values in {field!r}")
    return vals


def parse_last(s):
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)


def day_matches(dt, dom_set, dow_set, dom_raw, dow_raw):
    # cron dow: Sun=0 (or 7), Mon=1, ..., Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_wild = dom_raw.strip() == '*'
    dow_wild = dow_raw.strip() == '*'
    if dom_wild and dow_wild:
        return True
    if dom_wild:
        return cron_dow in dow_set
    if dow_wild:
        return dt.day in dom_set
    return dt.day in dom_set or cron_dow in dow_set


def main():
    if len(sys.argv) != 4:
        print("usage: cron-due.py CRON_EXPR TZ LAST_TS", file=sys.stderr)
        sys.exit(2)
    expr, tz_name, last_arg = sys.argv[1:4]
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc

    fields = expr.split()
    if len(fields) != 5:
        print(f"error: need 5 cron fields, got {len(fields)}", file=sys.stderr)
        sys.exit(2)
    try:
        minute = parse_field(fields[0], 0, 59)
        hour = parse_field(fields[1], 0, 23)
        dom = parse_field(fields[2], 1, 31)
        month = parse_field(fields[3], 1, 12)
        dow = parse_field(fields[4].replace('7', '0'), 0, 6)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    last_utc = parse_last(last_arg)
    now_utc = datetime.now(timezone.utc)
    if last_utc is None or last_utc >= now_utc:
        print("not-due")
        return

    last_local = last_utc.astimezone(tz)
    now_local = now_utc.astimezone(tz)

    day = last_local.date()
    end_day = now_local.date()
    safety = 400  # ~13 months max scan
    while day <= end_day and safety > 0:
        safety -= 1
        probe = datetime(day.year, day.month, day.day, tzinfo=tz)
        if probe.month in month and day_matches(probe, dom, dow, fields[2], fields[4]):
            for h in sorted(hour):
                for m in sorted(minute):
                    fire = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                    if last_local < fire <= now_local:
                        print("due")
                        return
        day += timedelta(days=1)
    print("not-due")


if __name__ == "__main__":
    main()
