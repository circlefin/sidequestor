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
watch-guard.py — enforce append-only on watch.json by checking the OUTCOME.

Replaces .claude/hooks/deny-state-writes.sh, which tried to enforce the same rule by
regexing the agent's command text to guess whether it was about to write. That produced
five false positives in a single day, including on a command that merely COUNTED write
sites and on a sentence documenting the limitation. It could also never see a write it
did not have a pattern for.

Verifying the result instead of policing the method is simpler, has no false positives
by construction, and catches every write path including ones nobody thought to match.

  snapshot <quest_id>   record every existing entry's identity before dispatch
  verify <quest_id>     after dispatch: restore any pre-existing entry that changed
                        or vanished, keep anything appended, report what happened

The rule being enforced: triage owns `last_checked_ts` on every entry that already
existed; the worker may only APPEND. Appends are what § 3a asks for and are left alone.

Exit 0 when clean, 1 when a violation was found and repaired, 2 on usage error.
Prints one JSON object describing the outcome.
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTS_DIR = REPO_ROOT / "state" / "quests"
SNAP_DIR = REPO_ROOT / "state" / "triage" / "watch-snapshots"


def die(msg, code=2):
    print(json.dumps({"error": msg}))
    sys.exit(code)


def watch_path(quest_id):
    if not quest_id or "/" in quest_id or ".." in quest_id:
        die(f"bad_quest_id:{quest_id}")
    for bucket in ("active", "completed", "archived"):
        p = QUESTS_DIR / bucket / quest_id / "watch.json"
        if p.exists():
            return p
    return None


def snap_path(quest_id):
    return SNAP_DIR / f"{quest_id}.json"


def load_watches(path):
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("watches"), list):
        return None
    return data


def write_atomic(path: Path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cmd_snapshot(quest_id):
    path = watch_path(quest_id)
    if path is None:
        print(json.dumps({"quest": quest_id, "snapshot": "no_watch_json"}))
        return 0
    data = load_watches(path)
    if data is None:
        print(json.dumps({"quest": quest_id, "snapshot": "unreadable"}))
        return 0
    # Store the whole entry, not just the watermark: a worker could also delete an
    # entry or edit its channel_id, and both are violations we want to undo.
    entries = {w.get("watch_id"): w for w in data["watches"]
               if isinstance(w, dict) and w.get("watch_id")}
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    write_atomic(snap_path(quest_id), entries)
    print(json.dumps({"quest": quest_id, "snapshot": "ok", "entries": len(entries)}))
    return 0


def cmd_verify(quest_id):
    path = watch_path(quest_id)
    snap = snap_path(quest_id)
    if path is None or not snap.exists():
        print(json.dumps({"quest": quest_id, "verify": "skipped"}))
        return 0
    try:
        before = json.loads(snap.read_text())
    except Exception:
        print(json.dumps({"quest": quest_id, "verify": "snapshot_unreadable"}))
        return 0

    data = load_watches(path)
    if data is None:
        # The file is now unreadable, which is the worst outcome. Rebuild it from the
        # snapshot: losing the worker's appends is far better than losing every cursor.
        write_atomic(path, {"watches": list(before.values())})
        print(json.dumps({"quest": quest_id, "verify": "restored_unreadable",
                          "restored": len(before)}))
        return 1

    current = {w.get("watch_id"): w for w in data["watches"]
               if isinstance(w, dict) and w.get("watch_id")}
    modified = [wid for wid, w in before.items()
                if wid in current and current[wid] != w]
    removed = [wid for wid in before if wid not in current]

    if not modified and not removed:
        appended = len(current) - len(before)
        out = {"quest": quest_id, "verify": "clean"}
        if appended > 0:
            out["appended"] = appended
        print(json.dumps(out))
        return 0

    # Repair: pre-existing entries go back to their snapshot form, anything appended
    # is kept in place. Order is preserved for the entries we still have.
    repaired, seen = [], set()
    for w in data["watches"]:
        if not isinstance(w, dict):
            continue
        wid = w.get("watch_id")
        if wid in before:
            repaired.append(before[wid])
        else:
            repaired.append(w)
        seen.add(wid)
    for wid in removed:
        repaired.append(before[wid])
    write_atomic(path, {**data, "watches": repaired})

    print(json.dumps({"quest": quest_id, "verify": "violation_repaired",
                      "modified": modified, "removed": removed}))
    return 1


def cmd_clear(quest_id):
    try:
        snap_path(quest_id).unlink()
    except OSError:
        pass
    return 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, quest_id = sys.argv[1], sys.argv[2]
    if cmd == "snapshot":
        return cmd_snapshot(quest_id)
    if cmd == "verify":
        return cmd_verify(quest_id)
    if cmd == "clear":
        return cmd_clear(quest_id)
    die(f"unknown_command:{cmd}")


if __name__ == "__main__":
    sys.exit(main())
