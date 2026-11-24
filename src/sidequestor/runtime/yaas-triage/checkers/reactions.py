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
checkers/reactions.py — scan Slack for the configured process, draft, save, adopt, and expand reactions
applied by the user since the 60-day cutoff. Diffs against processed-set state files. Writes
state/triage/pending_reactions.json if anything is new.

Unlike the per-entry checkers, this runs once globally (not per watch entry).
Called directly by the original shell orchestrator after the per-quest checker pass.

Usage:
  python3 checkers/reactions.py <mcp_call> <cutoff_date> <repo_root> <pending_path>

  mcp_call     — path to mcp-call.sh
  cutoff_date  — YYYY-MM-DD, 60 days ago (computed by the original shell orchestrator)
  repo_root    — absolute path to the repo root
  pending_path — path to write pending_reactions.json

Exit code follows the shared surface taxonomy: 0 ok, 1 auth, 2 error, 3 bad args,
4 transient. Prints one line to stdout for the original shell orchestrator to parse:
  "<SGT_timestamp>  REACTIONS_DIRTY=1"  or  "...REACTIONS_DIRTY=0" on success.
Dirty emoji details go to stderr.
"""
import subprocess
import json
import re
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reaction_config import load_reaction_emojis


def sgtnow():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


MAX_PAGES = 30   # ~600 results per emoji over the 60-day window


def normalized_timestamps(values):
    """Return valid Slack timestamps in deterministic order."""
    timestamps = []
    for value in values if isinstance(values, list) else []:
        try:
            timestamp = str(value)
            float(timestamp)
        except (TypeError, ValueError):
            continue
        timestamps.append(timestamp)
    return sorted(set(timestamps), key=float)


def load_pending(path):
    """Read the worker's blocked/unacked queue without making it a hard dependency."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    pending = {}
    for emoji, timestamps in data.items():
        normalized = normalized_timestamps(timestamps)
        if normalized:
            pending[str(emoji)] = normalized
    return pending


def initialization_epoch(repo_root):
    """Return the workspace's first-scan boundary, if this is a package workspace."""
    candidates = (
        Path(repo_root) / "state" / "triage" / "reaction-watermark.json",
        Path(repo_root) / ".yaas" / "instance.json",
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            value = data.get("initialized_at") or data.get("created_at")
            if value:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _main():
    mcp_call, cutoff, repo_root, pending_path = sys.argv[1:5]
    state_dir = os.path.join(repo_root, "state")
    init_epoch = initialization_epoch(repo_root)

    try:
        emojis = load_reaction_emojis()
    except ValueError as exc:
        print(f"REACTIONS_CONFIG_ERROR: {exc}", file=sys.stderr)
        return 2

    emoji_specs = [
        (emojis["process"], "claude_intensifies_replied.json", "replied_timestamps"),
        (emojis["draft"],   "writing_hand_replied.json",      "replied_timestamps"),
        (emojis["save"],    "floppy_disk_saved.json",         "saved_timestamps"),
        (emojis["adopt"],   "incoming_envelope_adopted.json", "adopted_timestamps"),
        (emojis["expand"],  "magic_wand_expanded.json", "expanded_timestamps"),
    ]

    # A blocked worker item may no longer carry its original trigger reaction. Keep
    # the durable queue across a fresh Slack search, then prune only items proven
    # complete by their state file below.
    existing_pending = load_pending(pending_path)
    pending = dict(existing_pending)
    truncated = []
    sources_path = os.path.join(state_dir, "reaction-sources.json")
    receipt_path = Path(state_dir) / 'triage/reaction-lifecycle.json'
    receipt_data = json.loads(receipt_path.read_text()) if receipt_path.exists() else {'items': {}}
    terminal = receipt_data.get('items') if isinstance(receipt_data, dict) else None
    if not isinstance(terminal, dict) or any(not isinstance(r, dict) for r in terminal.values()):
        raise ValueError('invalid reaction lifecycle receipts; dispatch must remain held')
    try:
        with open(sources_path) as handle:
            sources = json.load(handle)
    except (OSError, ValueError):
        sources = {}
    if not isinstance(sources, dict):
        sources = {}

    baseline_path = Path(state_dir) / "triage/reaction-expand-baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    if not isinstance(baseline, dict) or not isinstance(baseline.get("seen_timestamps", []), list):
        raise ValueError("invalid expand baseline; dispatch must remain held")

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
        initializing_expand = emoji == emojis["expand"] and baseline.get("emoji") != emoji
        if emoji == emojis["expand"] and not initializing_expand:
            known.update(baseline.get("seen_timestamps", []))
        known.update(iid.partition(':')[2] for iid, record in terminal.items()
                     if iid.partition(':')[0] == emoji and (record.get('final') or record.get('review_pending')))

        cursor = ""
        new_ts = []
        pages = 0
        while True:
            args = {"query": f"hasmy::{emoji}: after:{cutoff}", "limit": 20,
                    "sort": "timestamp", "sort_dir": "desc"}
            if cursor:
                args["cursor"] = cursor
            try:
                r = subprocess.run(
                    [mcp_call, "slack_search_public_and_private", json.dumps(args)],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                # The adapter's own HTTP timeout is also 30s, so this deadline can win
                # the race before the adapter reports exit 4. Same transient class;
                # nothing has been written yet, so the next sweep retries cleanly.
                print("REACTIONS_TRANSIENT_ERROR: Slack adapter timed out", file=sys.stderr)
                print(f"{sgtnow()}  REACTIONS_TRANSIENT_ERROR=1")
                return 4
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "Slack adapter failed").strip().splitlines()[0]
                kind = {1: "AUTH", 3: "BAD_ARGS", 4: "TRANSIENT"}.get(r.returncode, "ERROR")
                print(f"REACTIONS_{kind}_ERROR: {detail}", file=sys.stderr)
                print(f"{sgtnow()}  REACTIONS_{kind}_ERROR=1")
                return r.returncode
            if not r.stdout.strip():
                print("REACTIONS_ERROR: Slack adapter returned no payload", file=sys.stderr)
                print(f"{sgtnow()}  REACTIONS_ERROR=1")
                return 2
            try:
                d = json.loads(r.stdout)
            except Exception:
                print("REACTIONS_ERROR: Slack adapter returned malformed JSON", file=sys.stderr)
                print(f"{sgtnow()}  REACTIONS_ERROR=1")
                return 2
            text = d.get("results", "")
            blocks = re.split(r"### Result \d+ of \d+", text)
            page_ts = []
            for b in blocks[1:]:
                m = re.search(r"Message_ts: ?([0-9]+\.[0-9]+)", b)
                if m:
                    ts = m.group(1)
                    page_ts.append(ts)
                    url = re.search(r"https://[A-Za-z0-9.-]+\.slack\.com/archives/([A-Z0-9]+)/p\d+[^\s<>`\"')]*", b)
                    channel = re.search(r"Channel(?:_id)?:\s*([CDG][A-Z0-9]+)\b", b)
                    source = {"message_ts": ts}
                    if url:
                        source.update(permalink=url.group(0), channel_id=url.group(1))
                    elif channel:
                        source["channel_id"] = channel.group(1)
                    sources[f"{emoji}:{ts}"] = source
            for ts in page_ts:
                # Slack search is date-granular. Filter again by exact timestamp so a
                # same-day reaction from before `yaas init` cannot enter the first run.
                if init_epoch is not None and float(ts) <= init_epoch:
                    continue
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

        if initializing_expand:
            # Enabling a new action must not turn existing decorative reactions
            # into edit/execute requests. Only subsequent new matches are queued.
            if emoji not in truncated:
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = baseline_path.with_suffix(".tmp")
                with temporary.open("w") as stream:
                    json.dump({"emoji": emoji, "seen_timestamps": normalized_timestamps(new_ts + existing_pending.get(emoji, []))}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, baseline_path)
            new_ts = []

        retained = [] if initializing_expand else [ts for ts in existing_pending.get(emoji, []) if ts not in known]
        merged = normalized_timestamps(retained + new_ts)
        if merged:
            pending[emoji] = merged
            print(f"DIRTY_REACTION: {emoji} → {len(pending[emoji])} new", file=sys.stderr)
        else:
            pending.pop(emoji, None)

    if truncated:
        print(f"REACTIONS_TRUNCATED={','.join(sorted(set(truncated)))}", file=sys.stderr)
        print(f"{sgtnow()}  REACTIONS_TRUNCATED=1")

    # Preserve source coordinates so save/skip acknowledgments have useful receipts.
    os.makedirs(state_dir, exist_ok=True)
    source_tmp = sources_path + ".tmp"
    with open(source_tmp, "w") as handle:
        json.dump({f"{emoji}:{ts}": sources.get(f"{emoji}:{ts}", {"message_ts": ts})
                   for emoji, timestamps in pending.items() for ts in timestamps}, handle)
    os.replace(source_tmp, sources_path)

    if pending:
        pending_parent = os.path.dirname(pending_path)
        os.makedirs(pending_parent, exist_ok=True)
        pending_tmp = pending_path + ".tmp"
        with open(pending_tmp, "w") as handle:
            json.dump(pending, handle, indent=2)
        os.replace(pending_tmp, pending_path)
        print(f"{sgtnow()}  REACTIONS_DIRTY=1")
    else:
        try:
            os.unlink(pending_path)
        except FileNotFoundError:
            pass
        print(f"{sgtnow()}  REACTIONS_DIRTY=0")
    return 0


def main():
    try:
        return _main()
    except (ValueError, TypeError, OSError) as exc:
        print(f"REACTIONS_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
