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

"""Locked read/modify/write helpers for state/pending-approvals.json."""

import fcntl
import json
import os
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


REPO_ROOT = _repo_root(__file__)
STATE_DIR = REPO_ROOT / "state"
APPROVALS_FILE = STATE_DIR / "pending-approvals.json"
LOCK_FILE = STATE_DIR / "pending-approvals.json.lock"

NOT_FOUND = object()
NO_WRITE = object()


def _read_queue_unlocked() -> dict:
    if not APPROVALS_FILE.exists():
        return {"version": 1, "items": []}
    return _validate_queue(json.loads(APPROVALS_FILE.read_text()))


def _validate_queue(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("pending-approvals.json must contain an object")
    if not isinstance(data.get("items", []), list):
        raise ValueError("pending-approvals.json items must be a list")
    return data


def _write_queue_unlocked(data: dict):
    tmp = APPROVALS_FILE.with_name(APPROVALS_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, APPROVALS_FILE)


def read_queue() -> dict:
    if not APPROVALS_FILE.exists():
        return {"version": 1, "items": []}
    with open(APPROVALS_FILE) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return _validate_queue(json.load(f))
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def mutate_queue(callback):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            data = _read_queue_unlocked()
            result = callback(data)
            if result is NO_WRITE:
                return result
            _write_queue_unlocked(data)
            return result
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def mutate_item(approval_id: str, callback):
    def _mutate(data):
        item = next((i for i in data.get("items", []) if i.get("id") == approval_id), None)
        if item is None:
            return NOT_FOUND
        updates = callback(item)
        if updates is NO_WRITE:
            return NO_WRITE
        if updates is NOT_FOUND:
            return NOT_FOUND
        if isinstance(updates, dict):
            for key, value in updates.items():
                if value is None:
                    item.pop(key, None)
                else:
                    item[key] = value
            return item
        return updates

    return mutate_queue(_mutate)
