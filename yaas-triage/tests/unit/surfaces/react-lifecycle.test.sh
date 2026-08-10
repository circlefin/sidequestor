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

# react-lifecycle.test.sh — the reaction lifecycle, as one atomic, logged verb.
#
# The failure this closes: the emoji lifecycle (trigger → claudeloading → updatedone) was
# hand-composed prose, unlogged, and decoupled from the state file, so it drifted silently —
# a message could stay stuck at the trigger while the work was recorded done. This helper
# makes each transition one verb that removes every OTHER lifecycle emoji, adds the target,
# and logs it. These cases pin: exactly one lifecycle emoji ends up present, a missing emoji
# on remove is not an error, and a failed ADD is reported so the caller does not mark done.

set -u
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
LC="$SCRIPT_DIR/surfaces/react-lifecycle.py"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }

# A fake slack-react.sh that maintains the message's emoji SET in a file, so we can assert
# the end state. Records every call, and can be told to fail the ADD of a given emoji.
mk_react() {  # mk_react [fail_add_emoji]
  : > "$TMP/emojis"        # current emoji set, one per line
  : > "$TMP/calls"
  cat > "$TMP/react.sh" <<EOF
#!/bin/bash
verb="\$1"; emoji="\$4"
echo "\$verb \$emoji" >> "$TMP/calls"
if [ "\$verb" = "add" ]; then
  [ "\$emoji" = "${1:-__none__}" ] && exit 1        # simulate a failed add
  grep -qxF "\$emoji" "$TMP/emojis" || echo "\$emoji" >> "$TMP/emojis"
  exit 0
else  # remove
  if grep -qxF "\$emoji" "$TMP/emojis"; then
    # grep -vxF exits 1 when it filters out the LAST line (zero matches), so guard the mv
    # with || true — otherwise removing the final emoji is silently dropped.
    grep -vxF "\$emoji" "$TMP/emojis" > "$TMP/emojis.new" || true
    mv "$TMP/emojis.new" "$TMP/emojis"
    exit 0
  fi
  exit 1        # nothing to remove — the real tool returns nonzero, and that is NOT fatal
fi
EOF
  chmod +x "$TMP/react.sh"
}
set_emojis() { printf '%s\n' "$@" > "$TMP/emojis"; }
emojis()     { sort "$TMP/emojis" | tr '\n' ' ' | sed 's/ $//'; }
adv() { python3 "$LC" advance C1 1.0 "$1" --react "$TMP/react.sh" --log "$TMP/log"; }

echo "── trigger → loading: the trigger is removed, claudeloading added ─────────"
mk_react; set_emojis "claude-intensifies"
adv loading >/dev/null; eq "only claudeloading remains" "$(emojis)" "claudeloading"

echo
echo "── loading → done: claudeloading removed, updatedone added ────────────────"
mk_react; set_emojis "claudeloading"
adv done >/dev/null; eq "only updatedone remains" "$(emojis)" "updatedone"

echo
echo "── never two lifecycle emojis at once, even from a messy start ────────────"
# A partial previous run left BOTH the trigger and claudeloading on the message.
mk_react; set_emojis "claude-intensifies" "claudeloading"
adv done >/dev/null
eq "advancing to done clears both and leaves only updatedone" "$(emojis)" "updatedone"

echo
echo "── a foreign reaction (a human's thumbsup) is left untouched ──────────────"
mk_react; set_emojis "claude-intensifies" "+1"
adv loading >/dev/null
eq "the human's +1 survives; lifecycle advanced" "$(emojis)" "+1 claudeloading"

echo
echo "── a missing emoji on remove is NOT an error ──────────────────────────────"
# Fresh message with only the trigger; advancing to done must still succeed even though
# claudeloading was never there to remove.
mk_react; set_emojis "claude-intensifies"
adv done >/dev/null; RC=$?
eq "advance succeeded despite claudeloading absent" "$RC" "0"
eq "...and updatedone is present" "$(emojis)" "updatedone"

echo
echo "── a FAILED add is reported so the caller does not mark done ──────────────"
mk_react updatedone            # make the add of updatedone fail
set_emojis "claudeloading"
adv done >/dev/null; RC=$?
eq "advance returns nonzero when the add fails" "$RC" "1"
# The removes still happened, but the target never landed, so the message is now bare of a
# lifecycle emoji — the caller must NOT record done, and the reaction re-surfaces.
printf '%s' "$(emojis)" | grep -q updatedone && bad "updatedone should NOT be present" || ok "updatedone is absent, as it failed"

echo
echo "── idempotent: advancing to the state it is already in keeps that emoji ───"
mk_react; set_emojis "updatedone"
adv done >/dev/null; eq "re-advancing to done leaves updatedone" "$(emojis)" "updatedone"

echo
echo "── every transition is LOGGED (drift becomes visible) ─────────────────────"
mk_react; set_emojis "claude-intensifies"
adv loading >/dev/null
grep -q "REACTION LIFECYCLE C1/1.0 -> :claudeloading:" "$TMP/log" \
  && ok "the loading transition is logged" || bad "no lifecycle log line"

echo
echo "── bad usage is rejected ──────────────────────────────────────────────────"
python3 "$LC" advance C1 1.0 bogus >/dev/null 2>&1 && bad "an unknown state was accepted" || ok "unknown state rejected"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "react lifecycle: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
