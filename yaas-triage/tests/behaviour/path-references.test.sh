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

# test-path-references.sh — every documented script path must actually exist.
#
# WHY THIS EXISTS. Around 118 places in this repo name a script by path
# (`yaas-triage/ledger/add-watch.py` and friends). Most are ordinary code, but a large share sit
# in CLAUDE.md and yaas-triage/skills/*/SKILL.md, which are INSTRUCTIONS READ BY AN LLM
# AT RUNTIME. A stale path there does not fail at import time or startup: it fails
# halfway through a live dispatch, as a command-not-found the worker may quietly work
# around, and the only symptom is work that silently did not happen.
#
# So a moved or renamed script is a whole-repo change, and this test is what makes that
# safe. It extracts every path reference from docs, configs, plists and HTML and asserts
# each one resolves.
#
# It also guards the reverse direction: launchd plists and the two loop scripts must keep
# pointing at real files, since a broken plist means the agent simply never runs.

set -u
# Walk up rather than count "..": this suite moved from tests/ to tests/behaviour/ and
# counting would have silently pointed TRIAGE at the wrong directory.
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$HERE"
while [ "$REPO" != "/" ] && [ ! -d "$REPO/yaas-triage" ]; do REPO="$(dirname "$REPO")"; done
[ -d "$REPO/yaas-triage" ] || { echo "cannot locate repo root above $0" >&2; exit 1; }
TRIAGE="$REPO/yaas-triage"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

cd "$REPO"

# Files that may contain path references. Deliberately includes the LLM-facing ones.
SEARCH=(--include='*.md' --include='*.json' --include='*.sh' --include='*.py'
        --include='*.html' --include='*.plist' --include='*.template' --include='*.toml'
        --include='*.example')

# Directories that legitimately contain stale or historical references and must not fail
# the build: the plan document records what things were called before, and git internals
# and node_modules are not ours.
# repo-root.test.sh is excluded because its whole job is resolving HYPOTHETICAL paths
# inside a temp fixture: those strings are inputs to a function, not references to files
# that should exist. (Careful writing prose here: naming such a path in a comment makes
# THIS file fail its own check, which is exactly what happened the first time.)
# code-reviews/ holds architecture reviews and their remediation plans. A plan's whole job is
# to name files that do NOT exist yet (that is the proposal), so it belongs with docs/ here.
EXCLUDE_RE='^(\.git/|node_modules/|\.local/|logs/|state/|yaas-robustness-plan\.md|documentation-complaints\.md|docs/|code-reviews/|yaas-triage/tests/behaviour/repo-root\.test\.sh)'

# Paths that are documentation EXAMPLES rather than real files. Each needs a reason:
# a bare allowlist becomes a place to hide broken references.
is_allowed() {
  case "$1" in
    # yaas-ops SKILL.md walks the reader through creating a new checker from scratch.
    yaas-triage/checkers/telegram_chat.py) return 0 ;;
  esac
  return 1
}

echo "── every referenced yaas-triage script resolves ───────────────────────────"
MISSING=0
TOTAL=0
while IFS= read -r hit; do
  file="${hit%%:*}"; file="${file#./}"   # grep -r prefixes "./"; strip it or the
  ref="${hit#*:}"                        # anchored EXCLUDE_RE below never matches
  printf '%s\n' "$file" | grep -qE "$EXCLUDE_RE" && continue
  is_allowed "$ref" && continue
  TOTAL=$((TOTAL+1))
  if [ ! -e "$REPO/$ref" ]; then
    MISSING=$((MISSING+1))
    printf '    \033[31mmissing\033[0m %-42s referenced by %s\n' "$ref" "$file"
  fi
done < <(grep -rHoE "${SEARCH[@]}" \
           'yaas-triage/[a-zA-Z0-9_/-]+\.(py|sh|json|toml)' . 2>/dev/null \
         | sort -u)

[ "$TOTAL" -gt 0 ] \
  && ok "checked $TOTAL distinct path reference(s)" \
  || bad "found no path references at all — the extraction regex is broken"
[ "$MISSING" -eq 0 ] \
  && ok "all referenced paths resolve" \
  || bad "$MISSING referenced path(s) do not exist"

echo
echo "── the LLM-facing instruction files specifically ──────────────────────────"
# Separated out because these are the ones that fail at runtime rather than at startup.
for doc in CLAUDE.md CLAUDE.example.md; do
  [ -f "$doc" ] || continue
  n=0; miss=0
  while IFS= read -r ref; do
    n=$((n+1))
    is_allowed "$ref" && continue
    [ -e "$REPO/$ref" ] || { miss=$((miss+1)); printf '    \033[31mmissing\033[0m %s (in %s)\n' "$ref" "$doc"; }
  done < <(grep -ohE 'yaas-triage/[a-zA-Z0-9_/-]+\.(py|sh)' "$doc" | sort -u)
  if [ "$n" -eq 0 ]; then
    bad "$doc names no scripts at all — did the worker instructions lose them?"
  elif [ "$miss" -eq 0 ]; then
    ok "$doc: all $n script path(s) resolve"
  else
    bad "$doc: $miss of $n script path(s) are stale — a live dispatch would break"
  fi
done

for skill in "$TRIAGE"/skills/*/SKILL.md .claude/skills/*/SKILL.md; do
  [ -f "$skill" ] || continue
  miss=0
  while IFS= read -r ref; do
    is_allowed "$ref" && continue
    [ -e "$REPO/$ref" ] || { miss=$((miss+1)); printf '    \033[31mmissing\033[0m %s (in %s)\n' "$ref" "${skill#$REPO/}"; }
  done < <(grep -ohE 'yaas-triage/[a-zA-Z0-9_/-]+\.(py|sh)' "$skill" | sort -u)
  [ "$miss" -eq 0 ] || bad "${skill#$REPO/}: $miss stale path(s)"
done
[ "$FAIL" -eq 0 ] && ok "no skill file references a missing script"

echo
echo "── launchd plists point at real files ─────────────────────────────────────"
# A broken plist does not error anywhere visible; the agent simply never runs.
PLISTS=0
for pl in "$HOME"/Library/LaunchAgents/com.yaas.*.plist "$TRIAGE"/setup/*.plist.template; do
  [ -f "$pl" ] || continue
  PLISTS=$((PLISTS+1))
  while IFS= read -r prog; do
    # Templates carry {{REPO_ROOT}}; only a fully-resolved absolute path is checkable.
    case "$prog" in
      *'{{'*|*'__'*) continue ;;
      /*) ;;
      *) continue ;;
    esac
    [ -e "$prog" ] || bad "plist $(basename "$pl") points at missing $prog"
  done < <(sed -n 's|.*<string>\(.*\.\(sh\|py\)\)</string>.*|\1|p' "$pl" | sort -u)
done
[ "$PLISTS" -gt 0 ] && ok "checked $PLISTS plist(s)/template(s)" \
  || printf '  \033[33mSKIP\033[0m no plists found (fine on a machine with nothing installed)\n'

echo
echo "── no script writes state into yaas-triage/ by mistake ────────────────────"
# THE MOVE HAZARD. 16 scripts resolve the repo root as their own parent's parent. Moving
# one into a subdirectory silently makes that yaas-triage/ instead, so it would create a
# parallel state tree that nothing else reads. This asserts nothing did.
for stray in "$TRIAGE/state" "$TRIAGE/logs" "$TRIAGE/dispatch/state" "$TRIAGE/ledger/state" \
             "$TRIAGE/ops/state" "$TRIAGE/surfaces/state"; do
  [ -e "$stray" ] && bad "stray state tree at ${stray#$REPO/} — a script has the wrong repo root"
done
[ "$FAIL" -eq 0 ] && ok "no stray state/ or logs/ tree inside yaas-triage"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "path references: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
