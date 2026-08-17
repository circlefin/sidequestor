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

# github_issue.test.sh — the issues half of the GitHub pair.
#
# github_issue.py now adapts through shared github.py doctrine, but this suite still asserts
# the stall fix on the issues surface directly. The dangerous regression is the same as on the
# PR surface (unbounded DESCENDING query -> suffix of the gap -> the watermark can never cross
# it -> the watch parks as misconfigured), and count assertions alone still do not catch it.
# So the query shape is asserted here too, in its own suite.
#
# Two things are genuinely new versus github_pr and get the bulk of this file:
#
#   * `gh_account` -> GH_TOKEN. The repo this was built for is private to a SECOND GitHub
#     account, so the checker resolves a token per subprocess instead of mutating global gh
#     state. Two ways that goes wrong and both are silent: the token reaching a subprocess
#     that should not have it, and a logged-out account being reported as a retryable error
#     (which would back off forever) instead of `misconfig` (which pages a human).
#
#   * The searched surface is `issues`, not `prs`. If it ever includes pull requests, the
#     sibling github_pr watch on the same repo double-reports every PR and every dispatch is
#     paid for twice.

set -u
# yaas-triage/, found by walking up rather than by counting "..".
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

# A fake `gh` that records both the argv it was given and whether GH_TOKEN was in its
# environment, then replays canned rows. Both halves are asserted below.
mk_gh() {  # mk_gh <json_rows>
  cat > "$TMP/gh" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/gh.args"
printf '%s\n' "\${GH_TOKEN:-<unset>}" >> "$TMP/gh.token"
if [ "\$1" = "auth" ]; then echo "tok-from-keyring"; exit 0; fi
cat <<'ROWS'
$1
ROWS
EOF
  chmod +x "$TMP/gh"
  : > "$TMP/gh.args"; : > "$TMP/gh.token"
}

run() {  # run <last_checked_ts> <limit> [extra_json_fields]
  GH_BIN="$TMP/gh" timeout 30 python3 "$SCRIPT_DIR/checkers/github_issue.py" \
    "{\"type\":\"github_issue\",\"repo\":\"o/r\",\"last_checked_ts\":\"$1\",\"limit\":$2${3:-}}" 2>&1
}
field() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get(sys.argv[1],''))" "$2"; }

WM=1785931200   # 2026-08-05T12:00:00Z

echo "── it searches ISSUES, not PRs ────────────────────────────────────────────"
mk_gh '[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
run "$WM" 10 >/dev/null
grep -q "search issues" "$TMP/gh.args" \
  && ok "queries the issues surface" || bad "not querying issues"
grep -q -- "--include-prs" "$TMP/gh.args" \
  && bad "passes --include-prs, so every PR double-reports with the github_pr watch" \
  || ok "never passes --include-prs, so the sibling watch cannot double-report"

echo
echo "── the inherited query shape: bounded low, ascending ──────────────────────"
# Asserted here and not delegated to github_pr.test.sh: this is a COPIED fix, and a copy is
# where a fix rots without any count assertion noticing.
grep -q -- "--order asc" "$TMP/gh.args" \
  && ok "sorts ASCENDING, so the page is a prefix and not a suffix" \
  || bad "sorting descending — the page would be a suffix the watermark cannot cross"
grep -q "updated:>=2026-08-05T11:59:59Z" "$TMP/gh.args" \
  && ok "bounds the low end one second under the watermark" \
  || bad "no updated:>= bound, or the inclusive-qualifier backoff is missing"

echo
echo "── a full page is committable up to a boundary it holds in full ───────────"
ROWS='[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"},
       {"number":2,"title":"b","updatedAt":"2026-08-05T14:00:00Z","state":"open"},
       {"number":3,"title":"c","updatedAt":"2026-08-05T15:00:00Z","state":"open"}]'
mk_gh "$ROWS"
OUT=$(run "$WM" 3)
eq "a FULL page still reports complete=true" "$(field "$OUT" complete)" "True"
eq "...advancing only BELOW the final timestamp, never onto it" \
   "$(field "$OUT" advance_to)" "1785938400.000000"
eq "...counting only the rows inside that safe prefix" "$(field "$OUT" count)" "2"
printf '%s' "$OUT" | grep -q "backlog" \
  && ok "...and surfaces the remaining backlog in the preview" || bad "backlog not surfaced"

echo
echo "── the watermark advances MONOTONICALLY (no livelock) ─────────────────────"
mk_gh "$ROWS"
A=$(field "$(run "$WM" 2)" advance_to)
mk_gh '[{"number":3,"title":"c","updatedAt":"2026-08-05T15:00:00Z","state":"open"}]'
B=$(field "$(run "$A" 2)" advance_to)
python3 -c "import sys; sys.exit(0 if float('$B') > float('$A') else 1)" \
  && ok "tick 2 starts strictly above tick 1 ($A -> $B)" \
  || bad "watermark did not advance — this is the livelock"

echo
echo "── tie safety: the cases where no advance is provable ─────────────────────"
mk_gh '[{"number":1,"title":"x","updatedAt":"2026-08-05T13:00:00Z","state":"open"},
        {"number":2,"title":"y","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
OUT=$(run "$WM" 2)
eq "a full page of identical timestamps holds" "$(field "$OUT" outcome)" "hold"
eq "...and is explicitly incomplete" "$(field "$OUT" complete)" "False"
printf '%s' "$OUT" | grep -q "limit" \
  && ok "...and the reason names the knob to raise" || bad "reason does not say what to do"

# The dangerous one: a full page with nothing past the watermark. Reading that as clean lets
# triage advance to now-lag and skip every row beyond the page.
mk_gh '[{"number":1,"title":"x","updatedAt":"2026-08-05T11:59:59Z","state":"open"},
        {"number":2,"title":"y","updatedAt":"2026-08-05T12:00:00Z","state":"open"}]'
OUT=$(run "$WM" 2)
eq "a full page with nothing new HOLDS rather than reading clean" \
   "$(field "$OUT" outcome)" "hold"
eq "...and never reports complete" "$(field "$OUT" complete)" "False"

echo
echo "── nothing new ────────────────────────────────────────────────────────────"
mk_gh '[]'
OUT=$(run "$WM" 10)
eq "an empty result is clean, not an error" "$(field "$OUT" outcome)" "clean"
eq "...and complete" "$(field "$OUT" complete)" "True"
mk_gh '[{"number":9,"title":"old","updatedAt":"2026-08-05T11:59:59Z","state":"open"}]'
eq "a row at/below the watermark is post-filtered out" \
   "$(field "$(run "$WM" 10)" outcome)" "clean"

echo
echo "── gh_account: token handling ─────────────────────────────────────────────"
mk_gh '[{"number":1,"title":"a","updatedAt":"2026-08-05T13:00:00Z","state":"open"}]'
OUT=$(run "$WM" 10 ',"gh_account":"someone_crcl"')
grep -q -- "auth token -u someone_crcl" "$TMP/gh.args" \
  && ok "resolves the token for the named account" || bad "never resolved a token"
grep -q "auth switch" "$TMP/gh.args" \
  && bad "ran 'gh auth switch' — that mutates GLOBAL gh state and breaks other repos" \
  || ok "never switches the active account"
# The token must reach the SEARCH call (line 2 of the token log) but the resolve call itself
# (line 1) must inherit whatever the environment already had — passing a token to the command
# that mints the token is how you get an account you did not ask for.
eq "the search subprocess gets GH_TOKEN" "$(sed -n 2p "$TMP/gh.token")" "tok-from-keyring"
eq "the token-resolve subprocess does not" "$(sed -n 1p "$TMP/gh.token")" "<unset>"
printf '%s' "$OUT" | grep -q "tok-from-keyring" \
  && bad "the token appears in checker OUTPUT, which triage writes to the log" \
  || ok "the token never appears in the emitted result line"

# No gh_account at all: the active account's credentials, unchanged.
mk_gh '[]'
run "$WM" 10 >/dev/null
grep -q "auth token" "$TMP/gh.args" \
  && bad "resolved a token even though no gh_account was set" \
  || ok "no gh_account means no token indirection at all"

echo
echo "── failure classification ─────────────────────────────────────────────────"
# A logged-out account is PERMANENT until a human logs in. Reporting `error` would sink it
# into exponential backoff and quietly stop checking; `misconfig` surfaces it.
cat > "$TMP/gh" <<'SH'
#!/bin/bash
if [ "$1" = "auth" ]; then echo "no oauth token found for github.com account x" >&2; exit 1; fi
echo '[]'
SH
chmod +x "$TMP/gh"
eq "a logged-out gh_account is misconfig, not error" \
   "$(field "$(run "$WM" 10 ',"gh_account":"ghost"')" outcome)" "misconfig"

cat > "$TMP/gh" <<'SH'
#!/bin/bash
echo "API rate limit exceeded" >&2
exit 1
SH
chmod +x "$TMP/gh"
eq "a rate limit is ratelimited, so the tick skips instead of dispatching" \
   "$(field "$(run "$WM" 10)" outcome)" "ratelimited"

# A 404 on a private repo the active account cannot see. Not transient, not clean: it must be
# loud. Reading it as clean would advance the watermark over real activity forever.
# A 404 on a private repo the active account cannot see. Never clean — reading it that way
# would advance the watermark over real activity forever. And never a retryable `error`
# either: no amount of retrying grants permission, so it goes straight to misconfig instead
# of burning a day of backoff before being promoted there anyway.
cat > "$TMP/gh" <<'SH'
#!/bin/bash
echo "The listed users and repositories cannot be searched either because the resources do not exist or you do not have permission to view them." >&2
exit 1
SH
chmod +x "$TMP/gh"
eq "a private-repo 404 is misconfig, not clean and not retryable error" \
   "$(field "$(run "$WM" 10)" outcome)" "misconfig"

echo
echo "── \`search\` may carry qualifiers, never gh flags ──────────────────────────"
# Found by an adversarial review of this file. The `search` string is spliced into argv
# AHEAD of the flags gh parses, so a leading dash is a real flag, not a qualifier. The one
# that matters: --include-prs puts pull requests back into an issues query, and every PR
# then reports twice — once here, once on the sibling github_pr watch — paying for two
# dispatches on one event.
mk_gh '[]'
OUT=$(run "$WM" 10 ',"search":"--include-prs type:pr"')
eq "a smuggled --include-prs is refused" "$(field "$OUT" outcome)" "misconfig"
[ ! -s "$TMP/gh.args" ] \
  && ok "...before gh is invoked at all" || bad "gh ran anyway with the injected flag"
printf '%s' "$OUT" | grep -q -- "--include-prs" \
  && ok "...and the reason names the offending token" || bad "reason does not name the flag"

mk_gh '[]'
OUT=$(run "$WM" 10 ',"search":"--json number --limit 1"')
eq "any other gh flag is refused too, not just --include-prs" \
   "$(field "$OUT" outcome)" "misconfig"

mk_gh '[]'
OUT=$(run "$WM" 10 ',"search":"label:bug author:someone"')
eq "ordinary qualifiers still pass through" "$(field "$OUT" outcome)" "clean"
grep -q "label:bug author:someone" "$TMP/gh.args" \
  && ok "...and reach gh verbatim" || bad "qualifiers were mangled"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "github_issue: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
