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
add-watch.py — the only way to add a watch. Append-only by construction.

Why this exists
───────────────
`watch.json` has one invariant: triage owns every existing `last_checked_ts`, and the
worker may only APPEND. That invariant lived solely as prose in CLAUDE.md, and prose
does not stop an Edit tool call. It has been violated in production (the `threads[]`
versus `watches[]` dead drop, where appends went to a key nothing read).

So the raw Edit/Write path is now blocked by a PreToolUse hook, and this is the
supported path. It cannot corrupt an existing entry because it never rewrites one.

Usage
─────
  add-watch.py <quest_id> '<entry_json>'

  entry_json needs `type` plus that type's required fields, and `reason`.
  `last_checked_ts` should be the response_ts of the message you just sent (see
  CLAUDE.md § 3a) — pass it explicitly. If omitted, a slack_thread watch falls back
  to its own thread_ts, which is the documented behaviour for a draft with no send
  timestamp; every other type defaults to now.

Prints the assigned watch_id, or `skip:duplicate` if an equivalent watch already
exists (idempotent, so a retry is safe). Exits non-zero on a validation failure, so a
malformed watch is loud rather than silently appended and never checked.
"""

import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path

def _repo_root(start):
    """The repo root is the nearest ancestor directory that contains yaas-triage/.

    NOT counted as `parent.parent`: that is correct only while every script sits directly
    in yaas-triage/, and silently resolves to yaas-triage/ itself once a script moves into
    a subdirectory, producing a parallel state/ tree nothing reads. NOT keyed on CLAUDE.md
    (a fresh clone has only CLAUDE.example.md) and NOT on .git (two git dirs here, none in
    fixtures). Ambient $REPO_ROOT is deliberately ignored: a stale value pointing at another
    checkout would pass any marker check and silently redirect writes. Test fixtures copy
    the whole tree, so the walk-up finds the fixture on its own.

    Kept byte-identical across every file that needs it; tests/behaviour/repo-root.test.sh
    asserts that, because a shared module would need sys.path handling whose own path is
    depth-dependent, which is the bug being fixed.
    """
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT  = _repo_root(__file__)
QUESTS_DIR = REPO_ROOT / "state" / "quests"


def _load_watch_manifests():
    sys.path.insert(0, str(REPO_ROOT / "yaas-triage"))
    from tick_state import load_watch_manifests
    return load_watch_manifests(REPO_ROOT / "yaas-triage")


def _watch_shapes():
    manifests = _load_watch_manifests()
    required = {wtype: tuple(tuple(alt) for alt in manifest["required"])
                for wtype, manifest in manifests.items()}
    identity = {wtype: tuple(manifest["identity"])
                for wtype, manifest in manifests.items()}
    return required, identity


def die(msg):
    print(f"error:{msg}", file=sys.stderr)
    sys.exit(2)


def find_watch(quest_id):
    if not quest_id or "/" in quest_id or ".." in quest_id:
        die(f"bad_quest_id:{quest_id}")
    for bucket in ("active", "completed", "archived"):
        p = QUESTS_DIR / bucket / quest_id / "watch.json"
        if p.exists():
            return p
    die(f"no_watch_json_for_quest:{quest_id}")


def make_watch_id(quest_id, index, watch):
    """Same scheme as ensure-watch-ids.py, so an appended watch is indistinguishable
    from a migrated one."""
    identity = {k: v for k, v in watch.items() if k not in ("last_checked_ts", "watch_id")}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(f"{quest_id}\0{index}\0{canonical}".encode()).hexdigest()[:16]
    return f"watch-{digest}"


def validate(entry):
    required, _ = _watch_shapes()
    wtype = entry.get("type")
    if wtype not in required:
        die(f"unknown_type:{wtype}:known are {', '.join(sorted(required))}")
    if not any(all(entry.get(field) for field in alt) for alt in required[wtype]):
        if wtype == "schedule":
            die("schedule_needs_cron_and_tz_or_next_fire_ts")
        die(f"missing_fields_for_{wtype}:{','.join(required[wtype][0])}")
    if not entry.get("reason"):
        # A watch with no reason is unmaintainable: nobody can tell later whether it
        # is still wanted.
        die("missing_reason")
    mode = entry.get("watch_mode")
    if mode is not None and mode != "read_only":
        die(f"bad_watch_mode:{mode}:only read_only is meaningful")
    eph = entry.get("ephemeral")
    if eph is not None and not isinstance(eph, bool):
        # Strict: housekeep retires on `is True`, so a string "false" would be truthy to a
        # careless reader while doing nothing here, and "true" would silently NOT expire.
        # Both directions are silent, so reject anything that is not a real JSON boolean.
        die(f"bad_ephemeral:{eph!r}:must be JSON true or false, not a string")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    quest_id = sys.argv[1]
    try:
        entry = json.loads(sys.argv[2])
    except Exception as exc:
        die(f"bad_entry_json:{exc}")
    if not isinstance(entry, dict):
        die("entry_must_be_object")

    validate(entry)
    path = find_watch(quest_id)
    _, identity = _watch_shapes()

    # Default the watermark. Explicit is better: CLAUDE.md § 3a is specific that this
    # should be the response_ts of your own reply, not "now", or a reply arriving
    # between the send and this write is silently swallowed.
    if not entry.get("last_checked_ts"):
        if entry["type"] == "slack_thread" and entry.get("thread_ts"):
            entry["last_checked_ts"] = str(entry["thread_ts"])
        else:
            entry["last_checked_ts"] = f"{time.time():.6f}"
    entry["last_checked_ts"] = str(entry["last_checked_ts"])

    # Stamp WHEN this watch was created, which is not derivable from anything else on the
    # entry. last_checked_ts looks like an age but is a watermark: it advances every tick,
    # so a watch that should expire looks permanently fresh. Without this field a
    # slack_channel watch opened to catch one DM reply can never be aged out, and on
    # two such watches were still waking on every self-DM message 12 and 3 days after their
    # question was answered — one of them acted on an unrelated message and double-sent.
    # housekeep.retire_ephemeral() ages against this field.
    if not entry.get("created_ts"):
        entry["created_ts"] = f"{time.time():.6f}"
    entry["created_ts"] = str(entry["created_ts"])

    # Lock a SIDECAR, not the data file. We replace watch.json's inode below, and a
    # lock held on the old inode would not serialise pathname replacement: two writers
    # could each flock the old inode, then each replace the path from its own stale
    # snapshot, losing the first append. That is the exact failure this script exists
    # to prevent, so the lock has to outlive the inode.
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text())
            except Exception as exc:
                die(f"unreadable_watch_json:{exc}")
            watches = data.setdefault("watches", [])
            if not isinstance(watches, list):
                die("watches_is_not_a_list")

            ident = identity[entry["type"]]
            for w in watches:
                if not isinstance(w, dict) or w.get("type") != entry["type"]:
                    continue
                if all(w.get(k) == entry.get(k) for k in ident):
                    print(f"skip:duplicate:{w.get('watch_id', '?')}")
                    return 0

            entry["watch_id"] = make_watch_id(quest_id, len(watches), entry)
            existing_ids = {w.get("watch_id") for w in watches if isinstance(w, dict)}
            suffix = 0
            base = entry["watch_id"]
            while entry["watch_id"] in existing_ids:
                suffix += 1
                entry["watch_id"] = f"{base}-{suffix}"

            # APPEND ONLY. Every pre-existing entry is written back byte-identical,
            # which is the whole point of routing through this script.
            watches.append(entry)
            tmp = str(path) + ".tmp"
            with open(tmp, "w") as out:
                json.dump(data, out, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
            print(entry["watch_id"])
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
