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
checkers/reactions.py — scan Slack for new :claude-intensifies:, :writing_hand:, :floppy_disk:,
:incoming_envelope: reactions applied by the user since the 60-day cutoff. Diffs against
processed-set state files. Writes state/triage/pending_reactions.json if anything is new.

Unlike the per-entry checkers, this runs once globally (not per watch entry).
Called directly by triage.sh after the per-quest checker pass.

Usage:
  python3 checkers/reactions.py <mcp_call> <cutoff_date> <repo_root> <pending_path>

  mcp_call     — path to mcp-call.sh
  cutoff_date  — YYYY-MM-DD, 60 days ago (computed by triage.sh)
  repo_root    — absolute path to the repo root
  pending_path — path to write pending_reactions.json

Exit code: always 0. Prints one line to stdout for triage.sh to parse:
  "<SGT_timestamp>  REACTIONS_DIRTY=1"  or  "...REACTIONS_DIRTY=0"
Dirty emoji details go to stderr.
"""
import subprocess
import json
import re
import os
import sys
from datetime import datetime, timezone, timedelta


def sgtnow():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


MAX_PAGES = 30   # ~600 results per emoji over the 60-day window


def main():
    mcp_call, cutoff, repo_root, pending_path = sys.argv[1:5]
    state_dir = os.path.join(repo_root, "state")

    emoji_specs = [
        ("claude-intensifies", "claude_intensifies_replied.json", "replied_timestamps"),
        ("writing_hand",      "writing_hand_replied.json",      "replied_timestamps"),
        ("floppy_disk",       "floppy_disk_saved.json",         "saved_timestamps"),
        ("incoming_envelope", "incoming_envelope_adopted.json", "adopted_timestamps"),
    ]

    pending = {}
    truncated = []

    for emoji, fname, key in emoji_specs:
        state_file = os.path.join(state_dir, fname)
        processed, skipped = set(), set()
        if os.path.exists(state_file):
            try:
                s = json.load(open(state_file))
                processed = set(s.get(key, []))
                skipped = set((s.get("skipped_notes") or {}).keys())
            except Exception:
                pass
        known = processed | skipped

        cursor = ""
        new_ts = []
        pages = 0
        while True:
            args = {"query": f"hasmy::{emoji}: after:{cutoff}", "limit": 20,
                    "sort": "timestamp", "sort_dir": "desc"}
            if cursor:
                args["cursor"] = cursor
            r = subprocess.run(
                [mcp_call, "slack_search_public_and_private", json.dumps(args)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 or not r.stdout.strip():
                break
            try:
                d = json.loads(r.stdout)
            except Exception:
                break
            text = d.get("results", "")
            blocks = re.split(r"### Result \d+ of \d+", text)
            page_ts = []
            for b in blocks[1:]:
                m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
                if m:
                    page_ts.append(m.group(1))
            for ts in page_ts:
                if ts not in known:
                    new_ts.append(ts)
            pages += 1
            m = re.search(r"cursor `([^`]+)`", d.get("pagination_info", "") or "")
            if not m or not page_ts:
                break
            cursor = m.group(1)
            if pages >= MAX_PAGES:
                # Hit our own page cap with more results waiting. Every other checker
                # reports this as complete=false; this sweep has no watermark to
                # protect, but staying silent about it means a reacted message older
                # than the cap is simply never seen and nothing says so.
                truncated.append(emoji)
                break

        if new_ts:
            pending[emoji] = sorted(set(new_ts), key=float)
            print(f"DIRTY_REACTION: {emoji} → {len(pending[emoji])} new", file=sys.stderr)

    if truncated:
        print(f"REACTIONS_TRUNCATED={','.join(sorted(set(truncated)))}", file=sys.stderr)
        print(f"{sgtnow()}  REACTIONS_TRUNCATED=1")

    if pending:
        os.makedirs(os.path.dirname(pending_path), exist_ok=True)
        json.dump(pending, open(pending_path, "w"), indent=2)
        print(f"{sgtnow()}  REACTIONS_DIRTY=1")
    else:
        try:
            os.unlink(pending_path)
        except FileNotFoundError:
            pass
        print(f"{sgtnow()}  REACTIONS_DIRTY=0")


if __name__ == "__main__":
    main()
