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

# github_pr.test.sh — the checker that stalled a live watch for 14 hours.
#
# INCIDENT, 2026-08-05 15:00Z to 2026-08-06 05:19Z, 424 `gate_watch_misconfigured` events.
#
# The query was UNBOUNDED and DESCENDING: "the N most recently updated PRs". On a repo
# busier than N that is a SUFFIX of the gap, and a watermark can never cross a suffix,
# because the unread part sits directly above it. Coverage was tested as
# `len(prs) < limit`, which on such a repo is permanently false — so `complete: false`
# every tick, watermark frozen, watch parked as misconfigured. Three dispatches in seven
# minutes, each correctly acked `nothing_to_do`, each held.
#
# THE FIX IS THE QUERY, not the predicate. Bound the low end at the watermark and sort
# ASCENDING: the page becomes a contiguous PREFIX of the gap, which is safe to commit up to
# its newest row, so the backlog shrinks by up to `limit` rows every tick.
#
# The subtle part, and the reason this test exists: `complete` means "everything up to
# advance_to has been seen", NOT "the whole gap is done" — advance_to bounds the claim, the
# same convention slack_utils.drain() uses for a covered forward slice. Reporting
# complete=false on a prefix would hold the watermark and recreate the stall with entirely
# plausible-looking code. That mistake was made while writing this fix and caught here.

set -u
# yaas-triage/, found by walking up rather than by counting "..": these suites live at
# varying depths under tests/, and counting is the bug A1 removed from the scripts.
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

# A fake `gh` that records the query it was given and replays canned rows. The query shape
# IS the fix, so it is asserted rather than assumed.
mk_gh() {  # mk_gh <json_rows>
  cat > "$TMP/gh" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/gh.args"
cat <<'ROWS'
$1
ROWS
EOF
  chmod +x "$TMP/gh"
  : > "$TMP/gh.args"
}

run() {  # run <last_checked_ts> <limit>
  GH_BIN="$TMP/gh" timeout 30 python3 "$SCRIPT_DIR/checkers/github_pr.py" \
    "{\"type\":\"github_pr\",\"repo\":\"o/r\",\"last_checked_ts\":\"$1\",\"limit\":$2}" 2>&1
}
field() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get(sys.argv[1],''))" "$2"; }

WM=1785931200   # 2026-08-05T12:00:00Z

echo "── the query itself: bounded low, ascending ───────────────────────────────"
mk_gh '[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
run "$WM" 10 >/dev/null
grep -q -- "--order asc" "$TMP/gh.args" \
  && ok "sorts ASCENDING, so the page is a prefix and not a suffix" \
  || bad "still sorting descending — the page would be a suffix"
grep -q "updated:>=" "$TMP/gh.args" \
  && ok "bounds the low end at the watermark" \
  || bad "no updated:>= bound — the query returns the newest N overall"
grep -q "updated:>=2026-08-05T11:59:59Z" "$TMP/gh.args" \
  && ok "...one second below it, since the qualifier is inclusive and coarse" \
  || bad "boundary not backed off by a second"

echo
echo "── a prefix is COMMITTABLE, which is the whole fix ────────────────────────"
# THREE distinct timestamps so the page can end on a boundary we hold in full.
ROWS='[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"},
       {"number":2,"title":"b","updatedAt":"2026-08-05T14:00:00Z","state":"open"},
       {"number":3,"title":"c","updatedAt":"2026-08-05T15:00:00Z","state":"open"}]'
mk_gh "$ROWS"
OUT=$(run "$WM" 3)
eq "a FULL page still reports complete=true" "$(field "$OUT" complete)" "True"
# TIE SAFETY: the final row's timestamp may be only PARTIALLY held on a capped page, so the
# advance stops strictly below it. Advancing to the last row (15:00) would make the next run
# filter `> 15:00` and permanently skip any unseen rows also at 15:00.
eq "...advancing only BELOW the final timestamp, never to it" \
   "$(field "$OUT" advance_to)" "1785938400.000000"
eq "...counting only the rows inside that safe prefix" "$(field "$OUT" count)" "2"
printf '%s' "$OUT" | grep -q "backlog" \
  && ok "...and says in the preview that a backlog remains" || bad "backlog not surfaced"

echo
echo "── a short page means the gap is drained ──────────────────────────────────"
mk_gh "$ROWS"
OUT=$(run "$WM" 50)
eq "complete=true" "$(field "$OUT" complete)" "True"
printf '%s' "$OUT" | grep -q "backlog" \
  && bad "claimed a backlog on a drained gap" || ok "no backlog note when drained"

echo
echo "── the watermark advances MONOTONICALLY (no livelock) ────────────────────"
# The property the incident violated: each tick must start strictly above the last.
mk_gh "$ROWS"
A=$(field "$(run "$WM" 2)" advance_to)
mk_gh '[{"number":3,"title":"c","updatedAt":"2026-08-05T15:00:00Z","state":"open"}]'
B=$(field "$(run "$A" 2)" advance_to)
python3 -c "import sys; sys.exit(0 if float('$B') > float('$A') else 1)" \
  && ok "tick 2 advances strictly beyond tick 1 ($A -> $B)" \
  || bad "watermark did not advance — this is the livelock"

echo
echo "── nothing new ────────────────────────────────────────────────────────────"
mk_gh '[]'
OUT=$(run "$WM" 10)
eq "an empty result is clean" "$(field "$OUT" outcome)" "clean"
eq "...and complete" "$(field "$OUT" complete)" "True"
# The qualifier is coarse, so rows at or below the watermark can come back. The exact
# boundary is re-applied in the checker; this is that post-filter doing its job.
mk_gh '[{"number":9,"title":"old","updatedAt":"2026-08-05T11:59:59Z","state":"open"}]'
eq "a row at/below the watermark is filtered out" \
   "$(field "$(run "$WM" 10)" outcome)" "clean"

echo
echo "── tie safety: the cases where no advance is provable ─────────────────────"
# A FULL page whose new rows all share one timestamp. There is no boundary below them, so
# nothing can be advanced. `hold` rather than `error`: it is not a misconfiguration, and hold
# feeds the no-progress counter so it cannot sit silent forever.
mk_gh '[{"number":1,"title":"x","updatedAt":"2026-08-05T13:00:00Z","state":"open"},
        {"number":2,"title":"y","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
OUT=$(run "$WM" 2)
eq "a full page of identical timestamps holds" "$(field "$OUT" outcome)" "hold"
eq "...and is explicitly incomplete" "$(field "$OUT" complete)" "False"
printf '%s' "$OUT" | grep -q "limit" \
  && ok "...and the reason names the knob to raise" || bad "reason does not say what to do"

# A FULL page with NOTHING past the watermark means the boundary timestamp holds more rows
# than one page. Reporting clean+complete here is the dangerous case: triage would advance the
# watch to now-lag and skip everything beyond the page.
mk_gh '[{"number":1,"title":"x","updatedAt":"2026-08-05T11:59:59Z","state":"open"},
        {"number":2,"title":"y","updatedAt":"2026-08-05T12:00:00Z","state":"open"}]'
OUT=$(run "$WM" 2)
eq "a full page with nothing new HOLDS rather than reading clean" \
   "$(field "$OUT" outcome)" "hold"
eq "...and never reports complete" "$(field "$OUT" complete)" "False"

# When the page is NOT capped, a tie at the end is safe: there is provably nothing more.
mk_gh '[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"},
        {"number":2,"title":"b","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
OUT=$(run "$WM" 50)
eq "an UNcapped page may advance onto a tie" "$(field "$OUT" advance_to)" "1785934800.000000"

echo
echo "── transient upstream failures skip rather than dispatch ──────────────────"
cat > "$TMP/gh" <<'SH'
#!/bin/bash
echo "API rate limit exceeded" >&2
exit 1
SH
chmod +x "$TMP/gh"
eq "a rate limit is ratelimited, not error" \
   "$(field "$(run "$WM" 10)" outcome)" "ratelimited"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "github_pr: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
