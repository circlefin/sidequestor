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

# init-yaas-v2-tracking.sh — wire up the .git-yaas-v2 second git-dir.
#
# Lets triage.sh (via sync-yaas-v2.sh) pull daily updates from the canonical
# yaas-v2 template without touching the repo's main .git history. This is a
# second GIT_DIR pointed at the SAME worktree (the repo root) — it never runs
# `checkout` or `reset --hard`, so it never overwrites your working tree.
#
# Ref plumbing alone (symbolic-ref/update-ref) is NOT enough: it leaves the
# index empty, and `git diff`/`status` compare against the INDEX first, so
# every tracked file would show as "deleted" even if it's untouched on disk —
# `sync-yaas-v2.sh` would then see permanent fake drift and never pull for
# anyone. `read-tree` populates the index from the tree's blob SHAs (still no
# worktree writes), so the subsequent diff is a real content comparison.
#
# Idempotent: safe to re-run (e.g. if setup.sh is re-run for token rotation).
#
# Usage:
#   ./init-yaas-v2-tracking.sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
V2_GIT_DIR="$REPO_ROOT/.git-yaas-v2"
V2_URL="https://github.com/guangmian-kung_crcl/yourself-as-a-service-v2.git"

GIT() { git --git-dir="$V2_GIT_DIR" --work-tree="$REPO_ROOT" "$@"; }

if [ ! -d "$V2_GIT_DIR" ]; then
  GIT init --initial-branch=main --quiet
fi

if ! GIT remote get-url origin >/dev/null 2>&1; then
  GIT remote add origin "$V2_URL"
fi

echo "Fetching yaas-v2 template ($V2_URL)..."
GIT fetch origin main --quiet

# Point refs/heads/main at origin/main and populate the index to match —
# neither step touches the worktree (read-tree without -u/--reset never
# writes files, only the index).
GIT symbolic-ref HEAD refs/heads/main
GIT update-ref refs/heads/main refs/remotes/origin/main
GIT read-tree HEAD
GIT branch --set-upstream-to=origin/main main --quiet 2>/dev/null || true

echo "✓ .git-yaas-v2 tracking origin/main ($(GIT rev-parse --short HEAD))"

MODIFIED=$(GIT diff --name-only HEAD || true)
if [ -n "$MODIFIED" ]; then
  echo "  Note: these yaas-v2-shipped files already differ from the template on your machine:"
  echo "$MODIFIED" | sed 's/^/    /'
  echo "  sync-yaas-v2.sh will skip the daily pull as long as any of these stay modified."
fi
