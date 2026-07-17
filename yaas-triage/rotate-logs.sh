#!/bin/bash
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

# rotate-logs.sh — daily log rotation for YAAS state files.
#
# Gated by state/last_rotation.ts — skips if run within the last 23 hours.
#
# Rotates:
#   state/run-log.ndjson            keep last 7 days → run-log-archive-YYYY-MM.ndjson
#   state/quests/*/timeline.ndjson  keep last 100 entries → timeline.archive.ndjson
#   state/pending-approvals.json    prune executed/cancelled items older than 30 days

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SENTINEL="$REPO_ROOT/state/last_rotation.ts"

NOW=$(date +%s)
LAST=$(cat "$SENTINEL" 2>/dev/null || echo 0)
ELAPSED=$(( NOW - LAST ))

# Skip if run within last 23 hours
if [ "$ELAPSED" -lt 82800 ]; then
    exit 0
fi

log() { printf '%s  rotate-logs: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
log "starting"

# ── run-log.ndjson: keep last 7 days ─────────────────────────────────────────
RUN_LOG="$REPO_ROOT/state/run-log.ndjson"
if [ -f "$RUN_LOG" ]; then
    ARCHIVE="$REPO_ROOT/state/run-log-archive-$(date -u +%Y-%m).ndjson"
    python3 - "$RUN_LOG" "$ARCHIVE" <<'PYEOF'
import sys, json, os
from datetime import datetime, timezone, timedelta

log_path, archive_path = sys.argv[1], sys.argv[2]
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT")

keep, archive = [], []
for raw in open(log_path):
    line = raw.rstrip()
    if not line:
        continue
    try:
        ts = json.loads(line).get("ts", "")
        (archive if ts < cutoff else keep).append(line)
    except Exception:
        keep.append(line)

if archive:
    with open(archive_path, "a") as f:
        f.write("\n".join(archive) + "\n")

tmp = log_path + ".tmp"
with open(tmp, "w") as f:
    content = "\n".join(keep)
    f.write(content + ("\n" if content else ""))
os.replace(tmp, log_path)

print(f"run-log: kept {len(keep)}, archived {len(archive)} to {archive_path}")
PYEOF
fi

# ── per-quest timeline.ndjson: keep last 100 entries ─────────────────────────
shopt -s nullglob
for timeline in \
    "$REPO_ROOT/state/quests/active"/*/timeline.ndjson \
    "$REPO_ROOT/state/quests/completed"/*/timeline.ndjson; do

    LINE_COUNT=$(wc -l < "$timeline" | tr -d ' ')
    [ "${LINE_COUNT:-0}" -gt 100 ] || continue

    ARCHIVE="$(dirname "$timeline")/timeline.archive.ndjson"
    python3 - "$timeline" "$ARCHIVE" <<'PYEOF'
import sys, os
path, archive_path = sys.argv[1], sys.argv[2]
lines = [l for l in open(path).read().splitlines() if l.strip()]
overflow, keep = lines[:-100], lines[-100:]
if overflow:
    with open(archive_path, "a") as f:
        f.write("\n".join(overflow) + "\n")
tmp = path + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(keep) + "\n")
os.replace(tmp, path)
print(f"{path}: kept {len(keep)}, archived {len(overflow)}")
PYEOF

done
shopt -u nullglob

# ── pending-approvals.json: prune old executed/cancelled items ────────────────
APPROVALS="$REPO_ROOT/state/pending-approvals.json"
if [ -f "$APPROVALS" ]; then
    python3 - "$APPROVALS" <<'PYEOF'
import sys, json
from datetime import datetime, timezone, timedelta

path = sys.argv[1]
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

import os as _os
data = json.load(open(path))
before = len(data.get("items", []))
data["items"] = [
    item for item in data.get("items", [])
    if not (
        item.get("status") in ("executed", "cancelled")
        and item.get("created_at", "9999") < cutoff
    )
]
after = len(data["items"])
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
_os.replace(tmp, path)
print(f"pending-approvals: kept {after}, pruned {before - after}")
PYEOF
fi

echo "$NOW" > "$SENTINEL"
log "done"
