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

# sync-yaas-v2.sh — opt-in daily pull from the public yaas-v2 template repo.
#
# For people who just want to run the latest YAAS and not build/extend it
# themselves. Gated by settings.json -> sync.yaas_v2_auto_pull (default off —
# this never runs unless a user explicitly opts in). Also gated by
# state/last_yaas_v2_sync.ts — skips if run within the last 23 hours, same
# pattern as rotate-logs.sh.
#
# Requires a .git-yaas-v2 second git-dir (a separate GIT_DIR whose worktree
# is the repo root) tracking the canonical yaas-v2 template read-only. Set up
# by yaas-triage/setup/init-yaas-v2-tracking.sh, offered as an opt-in prompt
# during setup.sh. Pulls --ff-only so a diverged history never auto-merges. If any
# file tracked by that git-dir has local uncommitted changes (i.e. someone
# customized a shipped file), the pull is skipped for the day rather than
# clobbering it — customizers should sync manually when they're ready.
#
# Writes state/yaas-v2-sync-status.json so the outcome is checkable without
# reading logs.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTINGS="$REPO_ROOT/settings.json"
SENTINEL="$REPO_ROOT/state/last_yaas_v2_sync.ts"
STATUS_FILE="$REPO_ROOT/state/yaas-v2-sync-status.json"
V2_GIT_DIR="$REPO_ROOT/.git-yaas-v2"

log() { printf '%s  sync-yaas-v2: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

_write_status() {
  # $1=status  $2=detail
  python3 - "$STATUS_FILE" "$1" "$2" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
path, status, detail = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "status": status,
    "detail": detail,
}, open(path, "w"), indent=2)
PYEOF
}

# ── Opt-in gate (checked before the time sentinel, so flipping the setting
#    on takes effect on the next tick instead of waiting up to 23h) ──────────
if [ ! -f "$SETTINGS" ]; then
  exit 0
fi
ENABLED=$(python3 -c "
import json
try:
    print(bool(json.load(open('$SETTINGS')).get('sync', {}).get('yaas_v2_auto_pull', False)))
except Exception:
    print(False)
" 2>/dev/null)
[ "$ENABLED" = "True" ] || exit 0

if [ ! -d "$V2_GIT_DIR" ]; then
  log "sync.yaas_v2_auto_pull is on but $V2_GIT_DIR doesn't exist — nothing to pull from. Skipping."
  exit 0
fi

# ── Daily sentinel ────────────────────────────────────────────────────────────
NOW=$(date +%s)
LAST=$(cat "$SENTINEL" 2>/dev/null || echo 0)
ELAPSED=$(( NOW - LAST ))
[ "$ELAPSED" -lt 82800 ] && exit 0

log "starting"
GIT="git --git-dir=$V2_GIT_DIR --work-tree=$REPO_ROOT"

if ! $GIT fetch origin main --quiet 2>>"$REPO_ROOT/logs/triage.log"; then
  log "fetch failed — network or auth issue, will retry tomorrow"
  echo "$NOW" > "$SENTINEL"
  _write_status "failed" "git fetch failed"
  exit 0
fi

# Any tracked-file modification (not untracked personal files) means someone
# customized a shipped file — don't overwrite it.
if [ -n "$($GIT diff --name-only HEAD)" ]; then
  DIRTY=$($GIT diff --name-only HEAD | tr '\n' ',' | sed 's/,$//')
  log "local modifications to yaas-v2-tracked files ($DIRTY) — skipping pull, sync manually when ready"
  echo "$NOW" > "$SENTINEL"
  _write_status "skipped_dirty" "$DIRTY"
  exit 0
fi

FROM_SHA=$($GIT rev-parse HEAD)
TO_SHA=$($GIT rev-parse origin/main)

if [ "$FROM_SHA" = "$TO_SHA" ]; then
  log "already up to date ($FROM_SHA)"
  echo "$NOW" > "$SENTINEL"
  _write_status "up_to_date" "$FROM_SHA"
  exit 0
fi

if $GIT merge --ff-only origin/main --quiet 2>>"$REPO_ROOT/logs/triage.log"; then
  log "pulled $FROM_SHA -> $TO_SHA"
  echo "$NOW" > "$SENTINEL"
  _write_status "pulled" "$FROM_SHA..$TO_SHA"
else
  log "ff-only merge failed ($FROM_SHA -> $TO_SHA diverged) — leaving as-is, sync manually"
  echo "$NOW" > "$SENTINEL"
  _write_status "failed_diverged" "$FROM_SHA..$TO_SHA"
fi
log "done"
