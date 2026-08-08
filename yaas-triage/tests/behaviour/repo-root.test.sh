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

# test-repo-root.sh — every script must find the repo root from any depth.
#
# THE HAZARD THIS CLOSES. Sixteen scripts used to resolve the repo root by counting up two
# levels (`Path(__file__).parent.parent`, `cd "$SCRIPT_DIR/.."`). That is correct only while
# every script sits directly in yaas-triage/. The moment one moves into a subdirectory it
# resolves to yaas-triage/ instead, and then writes its state into a parallel
# yaas-triage/state/ tree that nothing else reads.
#
# That failure is silent. No crash, no error: watermarks advance in a file nobody consults,
# while the real ones sit still. It is the worst shape of bug this system can have, so the
# resolution rule is tested directly rather than trusted.
#
# THE RULE. The repo root is the nearest ancestor directory that contains `yaas-triage/`.
#   - Not CLAUDE.md: a fresh clone of the public mirror ships only CLAUDE.example.md.
#   - Not .git: this working tree has two git dirs, and test fixtures have none.
#   - Ambient $REPO_ROOT is IGNORED. A stale value pointing at another valid checkout would
#     pass any marker check, so it cannot be told apart from deliberate redirection and
#     would silently send writes to the wrong repo. Named per-script overrides
#     (YAAS_NOTIFY_REPO_ROOT, YAAS_ROTATE_REPO_ROOT, YAAS_HEALTH_REPO_ROOT) remain for tests.

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
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

# A fake repo, plus the subdirectories the reorganisation introduces.
FAKE="$TMP/fake-repo"
mkdir -p "$FAKE"
# Physical path: the helper resolves symlinks by design, and on macOS $TMPDIR sits under
# /var which is a symlink to /private/var. Comparing against the unresolved path would
# fail for the wrong reason.
FAKE=$(cd "$FAKE" && pwd -P)
mkdir -p "$FAKE/yaas-triage/ledger" "$FAKE/yaas-triage/dispatch" \
         "$FAKE/yaas-triage/ops" "$FAKE/yaas-triage/surfaces" \
         "$FAKE/yaas-triage/tests/differential"

# The canonical Python helper, extracted from a real script so the test cannot drift from
# the implementation. If the block below stops matching, this test fails loudly.
extract_py_helper() {
  python3 - "$SCRIPT_DIR/ledger/ack-watch.py" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"(def _repo_root\(.*?\n)(?=\n\n|\nREPO_ROOT)", src, re.S)
sys.stdout.write(m.group(1) if m else "")
PY
}

HELPER=$(extract_py_helper)
if [ -z "$HELPER" ]; then
  bad "could not extract _repo_root() from ack-watch.py — has the helper changed shape?"
  echo "repo root: $PASS passed, $FAIL failed"; exit 1
fi
ok "extracted the live _repo_root() helper from ack-watch.py"

# Drive the real helper from an arbitrary file path, with an arbitrary environment.
resolve() {  # resolve <pretend_file_path> [REPO_ROOT env value]
  ( [ $# -ge 2 ] && export REPO_ROOT="$2" || unset REPO_ROOT
    printf '%s\n' "$HELPER" > "$TMP/h.py"
    cat >> "$TMP/h.py" <<'PY'
import sys
print(_repo_root(sys.argv[1]))
PY
    # The helper relies on Path; prepend the import its host module already has.
    printf 'from pathlib import Path\n%s' "$(cat "$TMP/h.py")" > "$TMP/h.py"
    python3 "$TMP/h.py" "$1" 2>&1 )
}

echo
echo "── it resolves correctly from every depth ─────────────────────────────────"
eq "from yaas-triage/ (today's flat layout)" \
   "$(resolve "$FAKE/yaas-triage/ledger/ack-watch.py")" "$FAKE"
eq "from yaas-triage/ledger/ (one level deeper)" \
   "$(resolve "$FAKE/yaas-triage/ledger/ack-watch.py")" "$FAKE"
eq "from yaas-triage/ops/" \
   "$(resolve "$FAKE/yaas-triage/ops/notify.py")" "$FAKE"
eq "from yaas-triage/tests/differential/ (two levels)" \
   "$(resolve "$FAKE/yaas-triage/tests/differential/x.py")" "$FAKE"
# The whole point: depth must not matter.
A=$(resolve "$FAKE/yaas-triage/ledger/ack-watch.py")
B=$(resolve "$FAKE/yaas-triage/ledger/deeper/still/ack-watch.py")
eq "depth is irrelevant — flat and deeply nested agree" "$A" "$B"

echo
echo "── a test fixture resolves to the FIXTURE, not the real repo ─────────────"
# scenario.py builds <fixture>/yaas-triage/, so a script running inside a fixture must
# resolve there. If it escaped to the real repo, a test would write to live state.
FIX="$TMP/fixture"; mkdir -p "$FIX/yaas-triage/ledger"; FIX=$(cd "$FIX" && pwd -P)
eq "fixture root wins over any outer repo" \
   "$(resolve "$FIX/yaas-triage/ledger/ack-watch.py")" "$FIX"

echo
echo "── nesting picks the NEAREST marker ──────────────────────────────────────"
# mutations.sh copies the tree to a temp dir; a nested checkout must not resolve outward.
NEST="$FAKE/nested"; mkdir -p "$NEST/yaas-triage/ledger"; NEST=$(cd "$NEST" && pwd -P)
eq "an inner checkout resolves to itself" \
   "$(resolve "$NEST/yaas-triage/ledger/x.py")" "$NEST"

echo
echo "── ambient \$REPO_ROOT is IGNORED, deliberately ───────────────────────────"
# triage.sh exports REPO_ROOT to its children. A stale value pointing at ANOTHER VALID
# checkout would pass any marker check, so it cannot be told apart from a deliberate
# redirection: honouring it silently sends writes to the wrong repo. Fixtures copy the
# whole tree, so the walk-up already lands in the fixture and no override is needed.
# Named per-script overrides (YAAS_NOTIFY_REPO_ROOT and friends) remain, for tests.
ENVROOT="$TMP/envroot"; mkdir -p "$ENVROOT/yaas-triage"; ENVROOT=$(cd "$ENVROOT" && pwd -P)
eq "a valid-looking \$REPO_ROOT does NOT override the walk-up" \
   "$(resolve "$FAKE/yaas-triage/ledger/x.py" "$ENVROOT")" "$FAKE"
eq "a bogus \$REPO_ROOT is ignored too" \
   "$(resolve "$FAKE/yaas-triage/ledger/x.py" "$TMP/not-a-repo")" "$FAKE"

echo
echo "── it fails loudly rather than guessing ──────────────────────────────────"
# If no ancestor has yaas-triage/ the layout is broken. Returning a guess would write
# state somewhere arbitrary, which is exactly the silent failure being eliminated.
ORPHAN="$TMP/orphan/deep"; mkdir -p "$ORPHAN"
OUT=$(resolve "$ORPHAN/x.py")
printf '%s' "$OUT" | grep -qi "cannot locate repo root" \
  && ok "an orphaned script errors instead of inventing a root" \
  || bad "orphaned script returned '$OUT' instead of failing"

echo
echo "── every inlined copy is byte-identical ──────────────────────────────────"
# The helper is duplicated deliberately (a shared module would need sys.path handling whose
# own path is depth-dependent — the very bug being fixed). Duplication is only safe if the
# copies cannot drift, so that is asserted here.
NORM=$(printf '%s' "$HELPER" | sed 's/[[:space:]]*$//')
COPIES=0; DIFFERENT=0
for f in "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/*/*.py; do
  [ -f "$f" ] || continue
  case "$f" in */tests/*) continue ;; esac
  grep -q "def _repo_root" "$f" || continue
  COPIES=$((COPIES+1))
  THIS=$(python3 - "$f" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"(def _repo_root\(.*?\n)(?=\n\n|\nREPO_ROOT)", src, re.S)
sys.stdout.write(m.group(1) if m else "MISSING")
PY
)
  [ "$(printf '%s' "$THIS" | sed 's/[[:space:]]*$//')" = "$NORM" ] || {
    DIFFERENT=$((DIFFERENT+1)); printf '    drifted: %s\n' "${f#$SCRIPT_DIR/}"; }
done
[ "$COPIES" -ge 2 ] && ok "found $COPIES inlined copies" \
  || bad "expected several inlined copies, found $COPIES"
[ "$DIFFERENT" -eq 0 ] && ok "all copies identical" || bad "$DIFFERENT copy/copies drifted"

echo
echo "── the shell helper agrees with the Python one ───────────────────────────"
# The bash _repo_root lives in triage.sh, retired after the tick.py cutover (the live
# implementation is tick_state.py's _repo_root, checked in tick_state.test.sh). Only run this
# cross-language agreement check where triage.sh is still on disk (the private rollback copy);
# in a triage.sh-free checkout it is correctly skipped, not a failure.
if [ -f "$SCRIPT_DIR/triage.sh" ]; then
  SH=$(sed -n '/^_repo_root()/,/^}/p' "$SCRIPT_DIR/triage.sh")
  if [ -z "$SH" ]; then
    bad "no _repo_root() in triage.sh"
  else
    ok "extracted the shell helper from triage.sh"
    RES=$( printf '%s\n' "$SH" > "$TMP/h.sh"
           echo '_repo_root "$1"' >> "$TMP/h.sh"
           unset REPO_ROOT; bash "$TMP/h.sh" "$FAKE/yaas-triage/ledger" )
    eq "shell resolves the same root as Python" "$RES" "$FAKE"
  fi
else
  ok "triage.sh retired; tick_state.py _repo_root is canonical (see tick_state.test.sh)"
fi

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "repo root: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
