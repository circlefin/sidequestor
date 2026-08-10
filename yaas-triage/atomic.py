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
atomic.py — the one way this repo writes a JSON state file.

Every state file here is read by the next tick and by the dashboard, so a partial
write is not a cosmetic problem: a truncated watch.json loses watches, a truncated
pending-approvals.json loses drafts a human already reviewed.

The rule is temp + fsync + os.replace. os.replace is atomic within a filesystem, so
a reader either sees the whole old file or the whole new one, never a half-written
one. The fsync is what makes that survive a crash rather than just a process exit.

This existed in eleven hand-rolled copies at three different durability levels —
eight fsync'd the file, one also fsync'd the directory, and three did neither. The
inconsistency was invisible because every copy looked correct on its own.

Two things callers must know:

  * The lock cannot live on the file being replaced. os.replace swaps the inode, so
    a second writer blocked on a lock held against the old inode wakes up holding a
    deleted file, reads stale contents and writes them back. Lock a sidecar path
    (see ledger/approval-helper.py) or an enclosing directory instead.
  * write_json does not merge. Read, mutate, write is the caller's job, and it must
    happen inside whatever lock protects that file.
"""
import json
import os


def write_json(path, data, indent=2, trailing_newline=False):
    """Serialise `data` to `path` atomically and durably.

    trailing_newline matches the files that are read by shell/jq as well as Python,
    where a missing final newline shows up as a ragged diff.
    """
    path = str(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        if trailing_newline:
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path) or ".")


def _fsync_dir(directory):
    """Persist the rename itself.

    fsync on the file persists its contents; the directory entry that points at the
    new inode is a separate write. Without this a crash can leave the old file back.
    Best-effort: some filesystems refuse O_RDONLY fsync on a directory, and failing
    to harden the rename is not a reason to fail the write.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
