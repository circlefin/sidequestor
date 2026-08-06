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
new-quest.py — create a new quest folder with correctly-shaped files.

Usage (model calls this via Bash):
  python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '<spec_json>'

  Or pipe spec from stdin:
  echo '<spec_json>' | python3 yaas-triage/skills/yaas-quest-creation/new-quest.py -

Spec JSON fields:
  title       (required) — display name, also used to generate the quest ID slug
  watches     (required) — list of watch entries; do NOT include last_checked_ts
                           (this script sets it to now)
  priority    (optional) — "high" | "normal" | "low" — default: "normal"
  allow_send  (optional) — true | false — default: false
  context     (optional) — body text for context.md
  note        (optional) — short note for the created timeline event

Watch entry fields by type:
  slack_thread:  channel_id, thread_ts, reason
  slack_channel: channel_id, reason
  slack_dm:      user_id, reason
  slack_mention: user_id, reason   (fires on any message that @mentions user_id, anywhere)
  schedule:      cron (5-field), tz (IANA), reason  [optional: id]
  email:         query (Gmail search string), reason
  jira:          jql (JQL string, e.g. "labels=my-label"), reason
                 Fires when any issue in the set changes (status, comment, any edit).
                 Reads via yaas-triage/surfaces/jira-call.sh; needs the Keychain API token.
                 Do NOT put ORDER BY in the jql: it disables the checker's early
                 stop and makes every tick page to the cap.
  github_pr:     repo ("owner/name"), reason  [optional: search, limit]
                 Fires on any PR update in the repo (new PR, commit, review,
                 comment, merge). `search` adds GitHub qualifiers, but read the
                 warning in checkers/github_pr.py first: repeated qualifiers AND
                 rather than OR, so a bad one silently matches nothing forever.

Optional pre-dispatch filters (slack_channel + slack_thread only) — set these to avoid
waking the worker on irrelevant messages; evaluated inside the checker scripts
(checkers/slack_channel.py, checkers/slack_thread.py) before any dispatch decision:
  filter_user_ids:  [<user_id>, ...]  only these authors wake the worker
  filter_keywords:  [<str>, ...]      message must contain >=1 (case-insensitive substring)
  (both set → AND-ed. Passed through verbatim; spell exactly — unknown keys are silently ignored.)

Optional watch behaviour:
  watch_mode:  "read_only"  — monitor only; worker must not reply in this thread.
               Use for internal escalation threads (#help-*, #cpn-se-questions, etc.)
               where you post a question and want outcome notifications without bot chatter.
               Any value other than "read_only" is rejected by this script.

Example spec:
  {
    "title": "Weekly Wednesday check-in DM",
    "priority": "normal",
    "allow_send": true,
    "context": "Send a weekly DM every Wednesday at 09:00 local time.",
    "watches": [
      {
        "type": "schedule",
        "cron": "0 9 * * 3",
        "tz": "Asia/Singapore",
        "reason": "Wednesday check-in DM at 09:00 SGT"
      }
    ]
  }
"""

import sys
import os
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUESTS_ACTIVE    = REPO_ROOT / "state" / "quests" / "active"
QUESTS_COMPLETED = REPO_ROOT / "state" / "quests" / "completed"
QUESTS_ARCHIVED  = REPO_ROOT / "state" / "quests" / "archived"

REQUIRED_FIELDS = {
    "slack_thread":  ["channel_id", "thread_ts"],
    "slack_channel": ["channel_id"],
    "slack_dm":      ["user_id"],
    "slack_mention": ["user_id"],
    "schedule":      ["cron", "tz"],
    "email":         ["query"],
    "jira":          ["jql"],
    "github_pr":     ["repo"],
    # Runtime-only: the worker appends these itself when it queues a manual-review
    # item (§3d). Registered so the type system matches checkers/, not because a
    # new quest should normally scaffold one (no approval exists at creation time).
    "approval":      ["approval_id"],
}

# Every type here must have an executable checkers/<type>.py, or triage silently
# skips the watch forever ("no checker for type X" is only a vlog line). Keep this
# set in sync with the checkers directory; validate_watches rejects anything else.
KNOWN_TYPES = set(REQUIRED_FIELDS)

# Canonical field order for each type (for readable output)
FIELD_ORDER = ["type", "channel_id", "thread_ts", "user_id", "cron", "tz", "id", "query",
               "jql", "repo", "search", "limit",
               "watch_mode", "last_checked_ts", "filter_user_ids", "filter_keywords", "reason"]


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def slugify(title, max_len=40):
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:max_len].rstrip("-")


def id_exists(quest_id):
    for base in [QUESTS_ACTIVE, QUESTS_COMPLETED, QUESTS_ARCHIVED]:
        if (base / quest_id).exists():
            return True
    return False


def unique_quest_id(title):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = f"quest-{slugify(title)}-{date}"
    if not id_exists(base):
        return base
    for n in range(2, 100):
        candidate = f"{base}-{n}"
        if not id_exists(candidate):
            return candidate
    die("Could not generate a unique quest ID after 99 attempts")


def ordered_entry(raw_entry, now_ts):
    """Return a watch entry dict with fields in canonical order and last_checked_ts injected."""
    entry = {}
    for field in FIELD_ORDER:
        if field == "last_checked_ts":
            entry["last_checked_ts"] = now_ts
        elif field in raw_entry:
            entry[field] = raw_entry[field]
    # Append any extra fields not in FIELD_ORDER
    for k, v in raw_entry.items():
        if k not in entry:
            entry[k] = v
    return entry


def validate_watches(watches):
    if not watches:
        die("watches must have at least one entry")
    for i, w in enumerate(watches):
        t = w.get("type")
        if not t:
            die(f"watches[{i}] missing 'type'")
        if t not in KNOWN_TYPES:
            # Without this guard an unknown/typo'd type scaffolds fine, then triage
            # finds no checkers/<type>.py and skips it silently on every tick, so the
            # quest looks healthy while that watch never fires once.
            die(f"watches[{i}] has unknown type {t!r}. "
                f"Known types: {', '.join(sorted(KNOWN_TYPES))}. "
                f"If this is a new type, add checkers/{t}.py first, then register it "
                f"in REQUIRED_FIELDS here.")
        if not w.get("reason"):
            die(f"watches[{i}] (type={t!r}) missing 'reason'")
        for field in REQUIRED_FIELDS.get(t, []):
            if not w.get(field):
                die(f"watches[{i}] (type={t!r}) missing required field '{field}'")
        if "watch_mode" in w and w["watch_mode"] not in ("read_only",):
            die(f"watches[{i}] watch_mode must be 'read_only' if set, got: {w['watch_mode']!r}")
        if "last_checked_ts" in w:
            die(f"watches[{i}] must not include 'last_checked_ts' — this script sets it")


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "-":
        raw = sys.stdin.read()
    else:
        raw = sys.argv[1]

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"Invalid JSON spec: {e}")

    title      = spec.get("title", "").strip()
    priority   = spec.get("priority", "normal")
    allow_send = spec.get("allow_send", False)
    context    = spec.get("context", "")
    watches_in = spec.get("watches", [])
    note_raw   = spec.get("note", context[:80] if context else "Quest created")
    retire_days = spec.get("retire_slack_threads_after_days", None)  # None → default (30 in triage.sh)

    if not title:
        die("'title' is required")
    if priority not in ("high", "normal", "low"):
        die(f"priority must be high/normal/low, got: {priority!r}")
    if retire_days is not None and retire_days is not False:
        if not isinstance(retire_days, int) or retire_days < 0:
            die(f"retire_slack_threads_after_days must be a non-negative int or false, got: {retire_days!r}")

    validate_watches(watches_in)

    quest_id = unique_quest_id(title)
    now_ts   = f"{time.time():.6f}"
    now_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    note     = (note_raw or "Quest created")[:80]

    watches = [ordered_entry(w, now_ts) for w in watches_in]

    # ── Create folder ────────────────────────────────────────────────────────
    quest_dir = QUESTS_ACTIVE / quest_id
    try:
        quest_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        die(f"Quest folder already exists: {quest_dir}")

    # ── meta.json ────────────────────────────────────────────────────────────
    meta = {
        "id":         quest_id,   # always matches folder name — no more drift
        "title":      title,
        "status":     "active",
        "created":    now_utc,
        "priority":   priority,
        "allow_send": allow_send,
    }
    if retire_days is not None:
        meta["retire_slack_threads_after_days"] = retire_days
    (quest_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    # ── watch.json ───────────────────────────────────────────────────────────
    watch_doc = {
        "watches": watches,
    }
    (quest_dir / "watch.json").write_text(json.dumps(watch_doc, indent=2) + "\n")

    # ── timeline.ndjson ──────────────────────────────────────────────────────
    created_event = {"ts": now_utc, "event": "created", "by": "human", "note": note}
    (quest_dir / "timeline.ndjson").write_text(json.dumps(created_event) + "\n")

    # ── context.md ───────────────────────────────────────────────────────────
    watch_lines = []
    for w in watches:
        t = w["type"]
        if t == "slack_thread":
            watch_lines.append(f"- `slack_thread` channel `{w['channel_id']}` ts `{w['thread_ts']}`  \n  _{w['reason']}_")
        elif t == "slack_channel":
            watch_lines.append(f"- `slack_channel` `{w['channel_id']}`  \n  _{w['reason']}_")
        elif t == "slack_dm":
            watch_lines.append(f"- `slack_dm` from `{w['user_id']}`  \n  _{w['reason']}_")
        elif t == "schedule":
            watch_lines.append(f"- `schedule` `{w['cron']}` ({w['tz']})  \n  _{w['reason']}_")
        elif t == "email":
            watch_lines.append(f"- `email` query: `{w['query']}`  \n  _{w['reason']}_")
        else:
            watch_lines.append(f"- `{t}`  \n  _{w['reason']}_")

    context_md = f"""# {title}

## Why this quest exists

{context if context else "_Fill in why this quest exists._"}

## What we're watching

{chr(10).join(watch_lines)}

## Current state

_Quest just created._

## Links

_Add Slack permalinks, Coda docs, Jira tickets as they emerge._
"""
    (quest_dir / "context.md").write_text(context_md)

    # ── Confirmation ─────────────────────────────────────────────────────────
    type_counts: dict[str, int] = {}
    for w in watches:
        type_counts[w["type"]] = type_counts.get(w["type"], 0) + 1
    watch_summary = ", ".join(f"{n} {t}" for t, n in type_counts.items())

    print(f"✓ Created {quest_id}")
    print(f"  Path:       state/quests/active/{quest_id}/")
    print(f"  Watching:   {watch_summary}")
    print(f"  Priority:   {priority}  allow_send: {allow_send}")
    print()
    print("Triage will pick this up on its next tick.")
    print(f"Dry-run check: DRY_RUN=1 bash yaas-triage/triage.sh")


if __name__ == "__main__":
    main()
