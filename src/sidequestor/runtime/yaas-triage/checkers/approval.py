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
checkers/approval.py — check if a manual review item has been marked reviewed.

Input:  watch entry JSON as argv[1]
        {"type":"approval","approval_id":"appr-...","last_checked_ts":"..."}

Output: 1|message   if status == "reviewed" (approved → execute) or
                     status == "needs_reply" (reviewer asked a question → answer)
        0|message   if pending_review / executing / executed / cancelled / not found
        error|msg   on read failure (triage treats error as dirty/retry)
"""
import sys
import os
import json
from datetime import datetime, timezone
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


CHECKERS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = _repo_root(__file__)
APPROVALS = os.path.join(REPO_ROOT, "state", "pending-approvals.json")

# Resolve runtime imports from the installed package, not from the workspace.
RUNTIME_ROOT = Path(os.environ.get("YAAS_RUNTIME_ROOT", Path(__file__).resolve().parents[1]))
if (RUNTIME_ROOT / "yaas-triage").is_dir():
    RUNTIME_ROOT = RUNTIME_ROOT / "yaas-triage"
sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approval_state
import approval_store
import result
import tick_state

approval_state.configure(tick_state.load_environment(REPO_ROOT))

def main():
    if len(sys.argv) < 2:
        print("error|missing watch entry argument", file=sys.stderr)
        result.error("missing watch entry argument")
        return

    try:
        entry = json.loads(sys.argv[1])
    except Exception as e:
        print(f"error|bad watch entry JSON: {e}", file=sys.stderr)
        result.error("bad watch entry JSON")
        return

    approval_id = entry.get("approval_id")
    if not approval_id:
        print("error|approval_id missing from watch entry", file=sys.stderr)
        result.misconfig("approval_id missing from watch entry")
        return

    if not os.path.exists(APPROVALS):
        # File not yet created — not dirty
        result.counted(0, "pending-approvals.json not found")
        return

    try:
        data = approval_store.read_queue()
    except Exception as e:
        print(f"error|could not read pending-approvals.json: {e}", file=sys.stderr)
        result.error("could not read pending-approvals.json")
        return

    item = next((i for i in data.get("items", []) if i.get("id") == approval_id), None)

    if item is None:
        # Item pruned by rotate-logs — treat as clean (already handled)
        result.counted(0, f"{approval_id} not found (pruned or never written)")
        return

    status = item.get("status", "pending_review")

    if status == "reviewed":
        result.counted(1, f"manual review complete — {approval_id}")
    elif status == "needs_reply":
        result.counted(1, f"reviewer asked a question — {approval_id}")
    elif status == "executing":
        # A live claim: the worker is mid-execution, don't re-dispatch. An EXPIRED
        # claim means the worker died between `start` and `done`, so the send may or
        # may not have landed. Re-surface it — the worker's job then is to reconcile
        # (read the thread, look for the message) and NOT to blindly resend.
        if approval_state.is_stalled(item, datetime.now(timezone.utc)):
            result.counted(1, f"lease expired, outcome unknown — {approval_id}")
        else:
            result.counted(0, f"already executing — {approval_id}")
    else:
        result.counted(0, f"status={status}")


if __name__ == "__main__":
    main()
