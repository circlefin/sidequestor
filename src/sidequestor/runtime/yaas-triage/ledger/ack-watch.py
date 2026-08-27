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
ack-watch.py — per-dispatch acknowledgment ledger.

Why this exists
───────────────
`claude -p` exits 0 whenever the model completes its output normally, even if it
handled 3 of 5 dirty watches and quietly skipped the rest. Before this ledger,
triage advanced the watermark of every watch it had named in the dispatch on the
strength of that exit code alone, so unhandled activity was buried with no trace.

The ledger makes the commit evidence-based instead of exit-code-based: triage
opens a manifest listing exactly what was dispatched, the worker closes each item
by name, and triage advances only the items that were actually closed. Anything
unacked keeps its old watermark and re-surfaces on the next tick.

Manifest lives at state/triage/dispatch-<run_id>.json:

    {
      "run_id":      "run-20260805T091402Z-4711",
      "target":      "quest-foo-2026-04-28",
      "kind":        "quest",             # quest | reactions
      "created_utc": "2026-08-05T09:14:02Z",
      "items": [
        {"item_id": "watch-a1b2c3d4e5f6a7b8", "type": "slack_thread",
         "status": "pending", "note": "", "acked_utc": null}
      ]
    }

`item_id` is a watch_id for quest dispatches and "<emoji>:<msg_ts>" for the
reactions dispatch, so one mechanism covers both.

Sub-commands
────────────
open <run_id> <target> <kind> <items_json>
    Create the manifest. <items_json> is a JSON array of objects carrying at
    least `item_id`; `type` is optional. Prints the manifest path.

ack <run_id> <item_id> <handled|nothing_to_do|blocked> [note]
    Close one item. This is the call the WORKER makes. Exits non-zero (and
    changes nothing) if the run or item is unknown, so a typo is loud rather
    than silently accepted as work done.

    handled       — you did the thing (replied, drafted, queued, adopted, logged)
    nothing_to_do — you read the new activity and it correctly needs no action
    blocked       — you could not complete it; watermark must be held

acked <run_id>
    Print the item_ids whose status is handled or nothing_to_do, one per line.
    This is what triage advances. Empty output means nothing was acked.

summary <run_id>
    Print one JSON object of counts, for logging.

prune [days]
    Delete manifests older than `days` (default 7).
"""

import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
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
    override = (os.environ.get("SIDEQUESTOR_WORKSPACE")
                or os.environ.get("YAAS_WORKSPACE"))
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT    = _repo_root(__file__)
MANIFEST_DIR = REPO_ROOT / "state" / "triage"

OPEN_STATUS     = "pending"
COMMIT_STATUSES = ("handled", "nothing_to_do")
ACK_STATUSES    = COMMIT_STATUSES + ("blocked",)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock_path(run_id: str) -> Path:
    """Sidecar lockfile. We must NOT lock the manifest inode itself: `ack` replaces
    that inode via os.replace for crash-atomicity, and a lock held on the old inode
    would not serialise writers against the new one."""
    return MANIFEST_DIR / f"dispatch-{_safe(run_id)}.lock"


def _safe(run_id: str) -> str:
    # run_id lands in a filename, so refuse anything that could escape the dir.
    if not run_id or "/" in run_id or ".." in run_id:
        print(f"error:bad_run_id:{run_id}", file=sys.stderr)
        sys.exit(2)
    return run_id


@contextlib.contextmanager
def _locked(run_id: str, exclusive: bool):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    lp = _lock_path(run_id)
    with open(lp, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _write_atomic(path: Path, data: dict):
    """Write via temp file + fsync + os.replace, then fsync the directory. A crash
    (or the 30-minute worker watchdog) mid-ack therefore leaves either the old
    manifest or the new one, never a truncated half-written file — which would read
    as "nothing acked" and silently re-dispatch work the worker already finished."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _path(run_id: str) -> Path:
    return MANIFEST_DIR / f"dispatch-{_safe(run_id)}.json"


def cmd_open(run_id: str, target: str, kind: str, items_json: str):
    try:
        items = json.loads(items_json)
    except Exception as e:
        print(f"error:bad_items_json:{e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(items, list):
        print("error:items_must_be_array", file=sys.stderr)
        sys.exit(2)

    seen = set()
    norm = []
    for it in items:
        if not isinstance(it, dict) or not it.get("item_id"):
            print(f"error:item_missing_item_id:{it!r}", file=sys.stderr)
            sys.exit(2)
        iid = str(it["item_id"])
        # A duplicate item_id would make "acked" ambiguous; collapse it here.
        if iid in seen:
            continue
        seen.add(iid)
        norm.append({
            "item_id":   iid,
            "type":      it.get("type", ""),
            "status":    OPEN_STATUS,
            "note":      "",
            "acked_utc": None,
        })

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(run_id)
    with _locked(run_id, exclusive=True):
        _write_atomic(path, {
            "run_id":      run_id,
            "target":      target,
            "kind":        kind,
            "created_utc": _now(),
            "items":       norm,
        })
    print(path)


def cmd_ack(run_id: str, item_id: str, status: str, note: str = ""):
    if status not in ACK_STATUSES:
        print(f"error:bad_status:{status}:expected one of {'|'.join(ACK_STATUSES)}",
              file=sys.stderr)
        sys.exit(2)

    path = _path(run_id)
    if not path.exists():
        print(f"error:unknown_run:{run_id}", file=sys.stderr)
        sys.exit(3)

    with _locked(run_id, exclusive=True):
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"error:unreadable_manifest:{run_id}:{e}", file=sys.stderr)
            sys.exit(4)
        item = next((i for i in data.get("items", [])
                     if i.get("item_id") == item_id), None)
        if item is None:
            # Loud on purpose: silently accepting an unknown id would let a
            # hallucinated ack read as coverage for an item still pending.
            known = ", ".join(i.get("item_id", "") for i in data.get("items", []))
            print(f"error:unknown_item:{item_id}:dispatched items are [{known}]",
                  file=sys.stderr)
            sys.exit(3)
        item["status"]    = status
        item["note"]      = note
        item["acked_utc"] = _now()
        _write_atomic(path, data)
    print("ok")


def _load(run_id: str, strict: bool = False) -> dict:
    """Read the manifest under a shared sidecar lock.

    A manifest we cannot read must never be mistaken for "everything acked", so the
    lenient path returns {} (no committable ids). Callers that need to distinguish
    "valid but nothing acked" from "missing or corrupt" pass strict=True and get a
    non-zero exit instead, so the caller can bound the retry rather than loop."""
    path = _path(run_id)
    if not path.exists():
        if strict:
            print(f"error:unknown_run:{run_id}", file=sys.stderr)
            sys.exit(3)
        return {}
    try:
        with _locked(run_id, exclusive=False):
            return json.loads(path.read_text())
    except SystemExit:
        raise
    except Exception as e:
        if strict:
            print(f"error:unreadable_manifest:{run_id}:{e}", file=sys.stderr)
            sys.exit(4)
        return {}


def cmd_acked(run_id: str):
    # strict: a missing or corrupt manifest exits non-zero rather than printing an
    # empty list that reads identically to "the worker acked nothing".
    for i in _load(run_id, strict=True).get("items", []):
        if i.get("status") in COMMIT_STATUSES:
            print(i.get("item_id", ""))


def cmd_summary(run_id: str):
    items = _load(run_id).get("items", [])
    counts = {"total": len(items), "handled": 0, "nothing_to_do": 0,
              "blocked": 0, "pending": 0}
    for i in items:
        st = i.get("status", OPEN_STATUS)
        if st in counts:
            counts[st] += 1
    counts["committable"] = counts["handled"] + counts["nothing_to_do"]
    print(json.dumps(counts))


def cmd_prune(days: str = "7"):
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    except ValueError:
        print(f"error:bad_days:{days}", file=sys.stderr)
        sys.exit(2)
    if not MANIFEST_DIR.exists():
        return
    n = 0
    for p in list(MANIFEST_DIR.glob("dispatch-*.json")) + list(MANIFEST_DIR.glob("dispatch-*.lock")):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    if n:
        print(f"pruned {n} dispatch manifest(s)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    try:
        if cmd == "open":
            cmd_open(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
        elif cmd == "ack":
            cmd_ack(sys.argv[2], sys.argv[3], sys.argv[4],
                    sys.argv[5] if len(sys.argv) > 5 else "")
        elif cmd == "acked":
            cmd_acked(sys.argv[2])
        elif cmd == "summary":
            cmd_summary(sys.argv[2])
        elif cmd == "prune":
            cmd_prune(sys.argv[2] if len(sys.argv) > 2 else "7")
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            sys.exit(1)
    except IndexError:
        print(f"error:missing_arguments_for:{cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
