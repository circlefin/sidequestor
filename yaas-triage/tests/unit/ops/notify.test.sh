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

# test-notify.sh — unit test for notify.py's detection/filtering logic.
#
# Decoupled from real macOS delivery: notify.py is run with YAAS_NOTIFY_CMD
# pointed at a recorder script that appends "<title>\t<subtitle>\t<body>" to a
# file, so we assert WHAT notify.py decided to notify without depending on
# Notification Center actually displaying anything. Each case uses a fresh
# throwaway REPO_ROOT and WATERMARK via the env overrides notify.py supports.
#
# Usage: yaas-triage/tests/unit/ops/notify.test.sh   (exits 0 = all pass, 1 = a failure)

set -u
# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
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
. "$SCRIPT_DIR/tests/lib/harness.sh"
NOTIFY="$SCRIPT_DIR/ops/notify.py"

# Recorder used as YAAS_NOTIFY_CMD: argv = title, subtitle, body.
RECORDER="$(mktemp)"
cat > "$RECORDER" <<'EOF'
#!/bin/bash
printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$YAAS_TEST_RECORD"
EOF
chmod +x "$RECORDER"

# --- helpers ----------------------------------------------------------------

# epoch helpers (relative to now)
now()       { python3 -c 'import time;print(f"{time.time():.6f}")'; }
iso_ago()   { python3 -c "import time,datetime;print(datetime.datetime.fromtimestamp(time.time()-$1,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
epoch_ago() { python3 -c "import time;print(f'{time.time()-$1:.6f}')"; }

# run notify.py against a fixture root; returns recorder lines on stdout
run_notify() {
  local root="$1" wm="$2" rec="$3"
  : > "$rec"
  YAAS_NOTIFY_REPO_ROOT="$root" \
  YAAS_NOTIFY_WATERMARK="$wm" \
  YAAS_NOTIFY_CMD="$RECORDER" \
  YAAS_TEST_RECORD="$rec" \
  python3 "$NOTIFY"
}

# scaffold an empty fixture repo, echo its root
new_root() {
  local r; r="$(mktemp -d)"
  mkdir -p "$r/state/quests/active"
  echo "$r"
}
add_quest() { # root, id, title
  local d="$1/state/quests/active/$2"; mkdir -p "$d"
  printf '{"title":"%s"}\n' "$3" > "$d/meta.json"
  : > "$d/timeline.ndjson"; echo "$d/timeline.ndjson"
}

echo "notify.py unit tests"

# === CASE 1: first run (no watermark) writes watermark, fires nothing ========
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
add_quest "$R" q1 "Quest One" >/dev/null
# put an event that WOULD notify, to prove first-run still suppresses it
printf '{"ts":"%s","event":"message_sent","note":"hi"}\n' "$(iso_ago 10)" \
  > "$R/state/quests/active/q1/timeline.ndjson"
run_notify "$R" "$WM" "$REC"
if [ ! -s "$REC" ] && [ -f "$WM" ]; then ok "first run: suppresses all, seeds watermark"
else bad "first run: should fire nothing and create watermark" "record=$(cat "$REC")"; fi

# === CASE 2: new message_sent after watermark fires once =====================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 3600 > "$WM"   # watermark = 1h ago
TL="$(add_quest "$R" q1 "Quest One")"
printf '{"ts":"%s","event":"message_sent","note":"sent the thing"}\n' "$(iso_ago 60)" > "$TL"
run_notify "$R" "$WM" "$REC"
N=$(grep -c . "$REC")
if [ "$N" = "1" ] && grep -q "message sent" "$REC" && grep -q "Quest One" "$REC"; then
  ok "new message_sent: one notification, correct title+quest"
else bad "new message_sent: expected 1 with 'message sent'/'Quest One'" "got $N: $(cat "$REC")"; fi

# === CASE 3: event OLDER than watermark is ignored ===========================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 60 > "$WM"     # watermark = 1m ago
TL="$(add_quest "$R" q1 "Quest One")"
printf '{"ts":"%s","event":"draft_posted","note":"old"}\n' "$(iso_ago 600)" > "$TL"  # 10m ago = stale
run_notify "$R" "$WM" "$REC"
if [ ! -s "$REC" ]; then ok "stale event (pre-watermark): ignored"
else bad "stale event: should fire nothing" "got: $(cat "$REC")"; fi

# === CASE 4: only message_sent/draft_posted/executed count ===================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 3600 > "$WM"
TL="$(add_quest "$R" q1 "Quest One")"
{
  printf '{"ts":"%s","event":"info_received","note":"noise"}\n' "$(iso_ago 50)"
  printf '{"ts":"%s","event":"note","note":"noise"}\n'          "$(iso_ago 40)"
  printf '{"ts":"%s","event":"draft_posted","note":"real"}\n'   "$(iso_ago 30)"
  printf '{"ts":"%s","event":"executed","note":"real"}\n'       "$(iso_ago 20)"
} > "$TL"
run_notify "$R" "$WM" "$REC"
N=$(grep -c . "$REC")
if [ "$N" = "2" ] && grep -q "draft created" "$REC" && grep -q "action executed" "$REC" \
   && ! grep -q noise "$REC"; then
  ok "event filter: only draft_posted+executed fire, info/note ignored"
else bad "event filter: expected 2 (draft+executed)" "got $N: $(cat "$REC")"; fi

# === CASE 5: reaction state files fire by epoch watermark ====================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 100 > "$WM"
NEW_TS="$(epoch_ago 10)"; OLD_TS="$(epoch_ago 500)"
printf '{"replied_timestamps":["%s","%s"]}\n' "$OLD_TS" "$NEW_TS" \
  > "$R/state/claude_intensifies_replied.json"
printf '{"replied_timestamps":["%s"]}\n' "$NEW_TS" \
  > "$R/state/writing_hand_replied.json"
run_notify "$R" "$WM" "$REC"
N=$(grep -c . "$REC")
if [ "$N" = "2" ] && grep -q "claude-intensifies" "$REC" && grep -q "writing_hand" "$REC"; then
  ok "reaction files: new ts fires, old ts filtered, both emojis labelled"
else bad "reaction files: expected 2 (1 claude-intensifies + 1 writing_hand)" "got $N: $(cat "$REC")"; fi

# === CASE 6: 10-notification cap =============================================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 3600 > "$WM"
TL="$(add_quest "$R" q1 "Quest One")"
: > "$TL"
for i in $(seq 1 15); do
  printf '{"ts":"%s","event":"message_sent","note":"m%s"}\n' "$(iso_ago $((100-i)))" "$i" >> "$TL"
done
run_notify "$R" "$WM" "$REC"
N=$(grep -c . "$REC")
if [ "$N" = "10" ]; then ok "cap: 15 eligible events → exactly 10 notifications"
else bad "cap: expected 10" "got $N"; fi

# === CASE 7: watermark advances after a run ==================================
R="$(new_root)"; WM="$R/state/last_notified.ts"; REC="$(mktemp)"
epoch_ago 3600 > "$WM"; BEFORE="$(cat "$WM")"
run_notify "$R" "$WM" "$REC"
AFTER="$(cat "$WM")"
if python3 -c "import sys;sys.exit(0 if float('$AFTER')>float('$BEFORE') else 1)"; then
  ok "watermark advances to now after a run"
else bad "watermark should advance" "before=$BEFORE after=$AFTER"; fi

# --- summary ----------------------------------------------------------------
echo "------------------------------------------------------------"
printf 'notify.py: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ]
