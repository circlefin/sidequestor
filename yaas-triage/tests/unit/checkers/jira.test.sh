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

# jira.test.sh — the same suffix-vs-prefix problem github_pr had, in a second checker.
#
# jira paginated newest-first and stopped at a page cap. When the cap was hit it reported
# `complete: false`, which holds the watermark — so on an issue set busier than the cap the
# cursor could never advance past it. Exactly the livelock github_pr had, waiting to happen in
# a different file.
#
# The fix is the query: bound the low end with `updated >= "..."` and sort ASCENDING, so the
# pages held form a contiguous PREFIX of the gap, which is committable up to its newest row.
#
# One case stays conservative on purpose. If the WATCH supplies its own `ORDER BY`, we no
# longer own the sort and cannot claim a prefix, so the old page-cap rule still applies
# there. That asymmetry is the interesting part of this file.

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

# A fake jira-call.sh: records the path it was asked for (the QUERY is the fix, so it gets
# asserted) and replays a canned response.
mk_jira() {  # mk_jira <json_response>
  cat > "$TMP/jira-call.sh" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/jira.args"
cat <<'RESP'
$1
RESP
EOF
  chmod +x "$TMP/jira-call.sh"
  : > "$TMP/jira.args"
}
issue() { printf '{"key":"D-%s","fields":{"status":{"name":"Open"},"summary":"s%s","updated":"%s"}}' "$1" "$1" "$2"; }

run() {  # run <jql> <last_checked_ts>
  JIRA_CALL="$TMP/jira-call.sh" timeout 30 python3 "$SCRIPT_DIR/checkers/jira.py" \
    "$(python3 -c 'import json,sys; print(json.dumps({"type":"jira","jql":sys.argv[1],"last_checked_ts":sys.argv[2]}))' "$1" "$2")" 2>&1
}
field() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get(sys.argv[1],''))" "$2"; }

WM=1785931200   # 2026-08-05T12:00:00Z

echo "── the query: bounded low, ascending ──────────────────────────────────────"
mk_jira "{\"issues\":[$(issue 1 2026-08-05T13:00:00.000+0000)],\"isLast\":true}"
run "project=X" "$WM" >/dev/null
python3 -c "import urllib.parse,sys;sys.exit(0 if 'ORDER BY updated ASC' in urllib.parse.unquote(open(sys.argv[1]).read()) else 1)" "$TMP/jira.args" \
  && ok "sorts ASCENDING, so the pages are a prefix" \
  || bad "not ascending — the pages would be a suffix"
python3 -c "import urllib.parse,sys;sys.exit(0 if 'updated >=' in urllib.parse.unquote(open(sys.argv[1]).read()) else 1)" "$TMP/jira.args" \
  && ok "bounds the low end at the watermark" || bad "no updated >= bound"
python3 -c "import urllib.parse,sys;sys.exit(0 if '2026/08/05 11:59' in urllib.parse.unquote(open(sys.argv[1]).read()) else 1)" "$TMP/jira.args" \
  && ok "...backed off a minute (JQL is minute-granular)" || bad "boundary not backed off"

echo
echo "── a capped page set is still COMMITTABLE ─────────────────────────────────"
# isLast=false with a nextPageToken: more remains. Ascending means what we hold is a prefix,
# so it must commit up to its newest row. Reporting complete=false here is the livelock.
mk_jira "{\"issues\":[$(issue 1 2026-08-05T13:00:00.000+0000),$(issue 2 2026-08-05T14:00:00.000+0000),$(issue 3 2026-08-05T15:00:00.000+0000)],\"isLast\":false,\"nextPageToken\":\"t\"}"
OUT=$(run "project=X" "$WM")
eq "complete=true despite more pages remaining" "$(field "$OUT" complete)" "True"
# TIE SAFETY: on a capped page the final timestamp may be only partially held, so the advance
# stops strictly below it. Advancing onto it would skip unseen rows sharing that timestamp.
eq "...advancing only BELOW the final timestamp" "$(field "$OUT" advance_to)" "1785938400.000000"

# All changed issues sharing one timestamp on a capped page: no provable boundary, so hold.
mk_jira "{\"issues\":[$(issue 1 2026-08-05T14:00:00.000+0000),$(issue 2 2026-08-05T14:00:00.000+0000)],\"isLast\":false,\"nextPageToken\":\"t\"}"
OUT=$(run "project=X" "$WM")
eq "a capped page of identical timestamps holds" "$(field "$OUT" outcome)" "hold"
eq "...and is incomplete" "$(field "$OUT" complete)" "False"

echo
echo "── a caller-supplied ORDER BY stays conservative ──────────────────────────"
# We no longer own the sort, so a prefix cannot be claimed and the page cap must still block
# the advance. This asymmetry is deliberate.
mk_jira "{\"issues\":[$(issue 1 2026-08-05T13:00:00.000+0000)],\"isLast\":false,\"nextPageToken\":\"t\"}"
OUT=$(run "project=X ORDER BY key DESC" "$WM")
python3 -c "import urllib.parse,sys;sys.exit(0 if 'ORDER BY key' in urllib.parse.unquote(open(sys.argv[1]).read()) else 1)" "$TMP/jira.args" \
  && ok "the caller's ORDER BY is preserved" || bad "the caller's ORDER BY was overwritten"
python3 -c "import urllib.parse,sys;sys.exit(0 if 'updated >=' in urllib.parse.unquote(open(sys.argv[1]).read()) else 1)" "$TMP/jira.args" \
  && ok "...and the low bound is still inserted before it" || bad "no bound on the caller-ordered path"

echo
echo "── nothing new ────────────────────────────────────────────────────────────"
mk_jira '{"issues":[],"isLast":true}'
OUT=$(run "project=X" "$WM")
eq "an empty set is clean" "$(field "$OUT" outcome)" "clean"
eq "...and complete" "$(field "$OUT" complete)" "True"
mk_jira "{\"issues\":[$(issue 9 2026-08-05T11:00:00.000+0000)],\"isLast\":true}"
eq "an issue below the watermark is filtered out" \
   "$(field "$(run "project=X" "$WM")" outcome)" "clean"

echo
echo "── monotonic advance (no livelock) ────────────────────────────────────────"
mk_jira "{\"issues\":[$(issue 1 2026-08-05T13:00:00.000+0000)],\"isLast\":true}"
A=$(field "$(run "project=X" "$WM")" advance_to)
mk_jira "{\"issues\":[$(issue 2 2026-08-05T15:00:00.000+0000)],\"isLast\":true}"
B=$(field "$(run "project=X" "$A")" advance_to)
python3 -c "import sys;sys.exit(0 if float('$B') > float('$A') else 1)" \
  && ok "tick 2 advances strictly beyond tick 1" || bad "watermark did not advance"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "jira: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
