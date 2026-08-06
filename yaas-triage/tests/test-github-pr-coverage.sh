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

# test-github-pr-coverage.sh — the one-line predicate that stalled a live watch for 14h.
#
# INCIDENT, 2026-08-05 15:00Z to 2026-08-06 05:19Z. `_covered()` was
# `len(prs) < limit`. On a repo busier than `limit`, `gh search prs --sort updated`
# always returns exactly `limit` rows, so that test was permanently false, so the
# checker always reported `complete: false`, so the commit predicate always held the
# watermark even though the worker acked the item correctly.
#
# The visible sequence: three dispatches in seven minutes, each one correctly acked
# `nothing_to_do` about the same out-of-scope PR #762, each held by the saturation flag
# (three BACKLOG log lines). The no-progress counter then hit its threshold of 3 and
# promoted the watch to `misconfig`, which parked it and re-announced it every tick:
# 424 events over 14 hours. No data was lost, because holding the watermark is the safe
# direction, but a real reviewer question on a PR would have sat unread for 14
# hours.
#
# It had no unit test. The only coverage was a smoke test against a nonexistent repo,
# which exercises the error path and never reaches this function. This file is that
# missing test.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

check() {  # check <description> <expected true|false> <limit> <since> <updated...>
  local desc="$2" expect="$3" limit="$4" since="$5"; shift 5
  local got
  got=$(python3 - "$SCRIPT_DIR" "$limit" "$since" "$@" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "gpr", f"{sys.argv[1]}/checkers/github_pr.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
limit, since = int(sys.argv[2]), float(sys.argv[3])
prs = [{"updatedAt": t} for t in sys.argv[4:]]
print("true" if m._covered(prs, limit, since) else "false")
PY
)
  [ "$got" = "$expect" ] && ok "$desc" || bad "$desc (expected $expect, got $got)"
}

# Epochs used below, as ISO instants gh actually returns.
#   2026-08-05T10:00:00Z = 1785924000
#   2026-08-05T12:00:00Z = 1785931200
#   2026-08-05T14:00:00Z = 1785938400
WM=1785931200   # watermark: 2026-08-05T12:00:00Z

echo "── a short page proves coverage on its own ────────────────────────────────"
check _ "fewer rows than the limit means the repo had nothing more to give" \
  true 5 "$WM" 2026-08-05T14:00:00Z 2026-08-05T13:00:00Z
check _ "an empty result is covered, not saturated" true 5 "$WM"

echo
echo "── a FULL page is the case that broke ─────────────────────────────────────"
# THE REGRESSION. A full page whose oldest row predates the watermark has demonstrably
# reached back past it: nothing can hide in the gap. `len(prs) < limit` called this
# false forever.
check _ "full page whose oldest row is OLDER than the watermark is covered" \
  true 3 "$WM" 2026-08-05T14:00:00Z 2026-08-05T13:00:00Z 2026-08-05T10:00:00Z

# The genuine saturation case must still be detected, or the fix would trade a stall for
# silent data loss, which is far worse.
check _ "full page entirely NEWER than the watermark is NOT covered" \
  false 3 "$WM" 2026-08-05T14:00:00Z 2026-08-05T13:30:00Z 2026-08-05T13:00:00Z

# Boundary: oldest row exactly on the watermark. Inclusive, since the watermark itself
# has already been processed.
check _ "oldest row exactly at the watermark counts as covered" \
  true 2 "$WM" 2026-08-05T14:00:00Z 2026-08-05T12:00:00Z

echo
echo "── malformed input must not read as covered ───────────────────────────────"
# Failing closed matters: reading garbage as "covered" would advance the watermark past
# activity nobody looked at.
check _ "an unparseable timestamp in a full page is NOT covered" \
  false 2 "$WM" 2026-08-05T14:00:00Z not-a-date

echo
echo "── the exact live shape from the incident ─────────────────────────────────"
# a busy upstream docs repo, limit 20, 20 rows spanning 15:00-15:19Z.
LIVE=""; for i in $(seq 0 19); do LIVE="$LIVE 2026-08-05T15:$(printf '%02d' $i):00Z"; done

# Watermark AT the oldest row (15:00:00Z): the page reached back to it, so covered.
check _ "20-row page reaching back to the watermark is covered" \
  true 20 1785942000 $LIVE

# RESIDUAL GAP, and the reason github_pr stays on the open-items list.
#
# Watermark 10:00Z, five hours before the oldest row we got back. Reporting "not
# covered" is CORRECT: PRs updated between 10:00 and 15:00 could exist and we never saw
# them, so advancing would bury them. But github_pr does not page: it asks once with
# --limit and stops. So on a busy repo with a watermark older than one page-width, the
# watch reports complete:false on every tick and can never commit, which is the 14-hour
# stall all over again, arrived at honestly instead of through a bug.
#
# The Slack checkers already solve this with slack_utils.drain(), which walks a forward
# slice until it reaches the watermark. github_pr needs the same treatment. Until then
# this test documents the exposure rather than pretending it is closed.
check _ "full page NEWER than an old watermark is honestly not covered (no paging yet)" \
  false 20 1785924000 $LIVE

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "github_pr coverage: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
