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

# test-slack-drain.sh — slack_utils.drain(), against a fake Slack. No network.
#
# drain() decides how much of a source we can HONESTLY claim to have covered, so it is
# the single most consequential function in the checker layer. It is also pure: give it
# a fetch callback and it needs nothing else, which makes it properly unit testable.
#
# The failure it exists to prevent: Slack returns newest-first, so reading the newest N
# of a large backlog gives you a SUFFIX of the gap. The unread part sits directly above
# the watermark, so the cursor can never move, and the next tick reads the same newest N
# and is stuck identically. That is a livelock which either burns a dispatch every tick
# forever or (for a filtered watch) sits silent forever.

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

python3 - "$SCRIPT_DIR/checkers" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import slack_utils as u

PASS = FAIL = 0
def ok(m):
    global PASS; PASS += 1; print(f"  \033[32mPASS\033[0m {m}")
def bad(m):
    global FAIL; FAIL += 1; print(f"  \033[31mFAIL\033[0m {m}")
def eq(label, got, want):
    ok(label) if got == want else bad(f"{label} (got {got!r}, want {want!r})")


def fake_slack(timestamps, page_limit=50):
    """Newest-first, honours oldest/latest bounds and a cursor, like the real thing."""
    calls = {"n": 0}
    def fetch(cursor, oldest, latest):
        calls["n"] += 1
        sel = [t for t in sorted(timestamps, reverse=True)
               if (oldest is None or t > oldest) and (latest is None or t <= latest)]
        start = int(cursor) if cursor else 0
        page = sel[start:start + page_limit]
        nxt = str(start + page_limit) if start + page_limit < len(sel) else None
        txt = "".join(
            "=== Message from A <a@b> (U1) at now ===\nMessage TS: %.6f\nbody\n" % t
            for t in page)
        return txt, nxt, None
    return fetch, calls


def drain_to_empty(timestamps, max_ticks=60, **kw):
    """Simulate successive ticks. Returns (ticks, requests, leftover, stalled)."""
    wm, reqs, now = 0.0, 0, max(timestamps) + 1
    for tick in range(1, max_ticks + 1):
        f, c = fake_slack(timestamps)
        count, preview, advance_to, complete, transient = u.drain(f, wm, now=now, **kw)
        reqs += c["n"]
        if not complete:
            return tick, reqs, sum(1 for t in timestamps if t > wm), True
        if advance_to is None or float(advance_to) <= wm:
            return tick, reqs, sum(1 for t in timestamps if t > wm), True   # no progress
        wm = float(advance_to)
        left = sum(1 for t in timestamps if t > wm)
        if left == 0:
            return tick, reqs, 0, False
    return max_ticks, reqs, sum(1 for t in timestamps if t > wm), True


print("── the ordinary cases ─────────────────────────────────────────────────────")
f, c = fake_slack([100.0 + i for i in range(10)])
count, _p, adv, complete, _t = u.drain(f, 90.0, now=10_000.0)
eq("10 new messages: counted", count, 10)
eq("...and fully covered", complete, True)
eq("...in one request", c["n"], 1)
eq("...advancing to the newest, not to now", float(adv), 109.0)

f, c = fake_slack([500.0])
count, _p, adv, complete, _t = u.drain(f, 1000.0, now=10_000.0)
eq("nothing newer than the watermark: clean", (count, complete), (0, True))

print()
print("── the livelock this function exists to prevent ───────────────────────────")
sparse = [1.0 + i * 500 for i in range(5000)]          # 5000 over ~29 days
ticks, reqs, left, stalled = drain_to_empty(sparse)
if stalled:
    bad(f"a 5000-message backlog STALLED with {left} unread")
else:
    ok(f"a 5000-message backlog drains completely ({ticks} ticks, {reqs} requests)")

burst = [100.0 + i for i in range(400)]                # 400 in one stretch
ticks, reqs, left, stalled = drain_to_empty(burst)
if stalled:
    bad(f"a 400-message backlog STALLED with {left} unread")
else:
    ok(f"a 400-message backlog drains ({ticks} ticks, {reqs} requests)")

# Density, not just span: a slice can be too dense to read in one page at ANY width,
# so the slice phase pages through it rather than only halving it.
tight = [1000.0 + i * 0.01 for i in range(3000)]
ticks, reqs, left, stalled = drain_to_empty(tight, max_ticks=150)
if stalled:
    bad(f"a dense 3000-message burst STALLED with {left} unread")
else:
    ok(f"a dense 3000-message burst drains ({ticks} ticks, {reqs} requests)")

print()
print("── coverage is about the WINDOW, not about what matched ───────────────────")
# A slice full of messages that all fail the filter has still only shown us one page of
# that slice. Judging coverage on the FILTERED count would advance the cursor past
# everything else in it. This bug shipped and was caught by a test.
dense = [1000.0 + i * 0.01 for i in range(3000)]       # 3000 inside 30 seconds
# Drive the cursor up to the burst first; the empty stretch before it is legitimately
# coverable and gets skipped cheaply, which is the behaviour we want.
wm, verdicts = 0.0, []
for _ in range(8):
    count, _p, adv, complete, _t = u.drain(fake_slack(dense)[0], wm,
                                           filter_user_ids=["U-nobody"],
                                           now=max(dense) + 1)
    verdicts.append(complete)
    if not complete or adv is None:
        break
    wm = float(adv)
eq("a filtered burst eventually reports NOT covered", verdicts[-1], False)
ok("...rather than advancing past 3000 unread messages because none matched the filter")

# And it must never claim progress it did not make: no cursor when not covered.
count, _p, adv, complete, _t = u.drain(fake_slack(dense)[0], 999.9,
                                       filter_user_ids=["U-nobody"], now=max(dense) + 1)
if complete:
    bad("a saturated slice at the burst was called covered")
else:
    eq("...and offers no cursor when it gives up", adv, None)

print()
print("── quiet stretches are skipped cheaply, not one message at a time ─────────")
# A watch that goes quiet for months must not need one tick per empty hour.
lonely = [1.0, 9_000_000.0]
f, c = fake_slack(lonely)
count, _p, adv, complete, _t = u.drain(f, 0.0, now=9_000_001.0)
eq("a 3-month gap with 2 messages is covered in one pass", complete, True)
ok(f"...using {c['n']} request(s)")

print()
print("── transient failures never fake coverage ─────────────────────────────────")
def flaky(cursor, oldest, latest):
    return "", None, "slack ratelimited"
count, _p, adv, complete, transient = u.drain(flaky, 0.0, now=10_000.0)
eq("a rate limit reports transient", bool(transient), True)
eq("...and never claims completeness", complete, False)
eq("...and offers no cursor", adv, None)

print()
print("── the bounds are actually sent ───────────────────────────────────────────")
seen = []
def recorder(cursor, oldest, latest):
    seen.append((oldest, latest))
    return "", None, None
u.drain(recorder, 1234.5, now=9999.0)
eq("the watermark is passed as the lower bound", seen[0][0], 1234.5)
ok("...which is what makes paging terminate at the gap instead of at channel history")

print()
print("────────────────────────────────────────────────────────────────────────────")
print(f"slack drain: {PASS} passed, {FAIL} failed")

# ── search_advance_to(): the watermark rule for SEARCH-backed checkers ──────────
# Read-backed checkers prove coverage with drain(). Search-backed ones cannot: Slack
# search reads an eventually-consistent INDEX, so "found nothing" never proves "nothing
# was posted" for the most recent seconds. Before this rule existed, slack_dm and
# slack_mention emitted no advance_to at all, tick.py fell back to `now - lag_map[type]`,
# and lag_map comes from optional checkers/<type>.lag files. slack_mention.lag existed
# (90s); slack_dm.lag did NOT, so a slack_dm watch advanced to exactly NOW on every clean
# search and any DM not yet indexed at that instant was buried permanently.
print()
print("-- search_advance_to: never claims the unindexed window --")
NOW = 1_000_000.0
L = u.SEARCH_INDEX_LAG
P2 = F2 = 0
def eq2(name, got, want):
    global P2, F2
    if abs(got - want) < 1e-6:
        P2 += 1; print(f"  PASS {name}")
    else:
        F2 += 1; print(f"  FAIL {name} (want {want}, got {got})")

eq2("nothing found -> now-lag, NOT now", u.search_advance_to(0, NOW), NOW - L)
eq2("an old message -> that message",    u.search_advance_to(NOW - 500, NOW), NOW - 500)
eq2("a very recent message -> clamped",  u.search_advance_to(NOW - 5, NOW), NOW - L)
eq2("exactly at the ceiling",            u.search_advance_to(NOW - L, NOW), NOW - L)
# A future-dated ts (clock skew between Slack and this host) must not slip the ceiling.
eq2("a future ts cannot skip the ceiling", u.search_advance_to(NOW + 999, NOW), NOW - L)

# The property that matters, over the whole input space: the watermark NEVER lands in the
# window that may still be unindexed. One counterexample here is a buried-message bug.
import random
random.seed(7)
viol = sum(1 for _ in range(5000)
           if u.search_advance_to(NOW - random.uniform(-500, 50000), NOW) > NOW - L + 1e-9)
if viol == 0:
    P2 += 1; print(f"  PASS property: 0/5000 random inputs advanced past now-{L:.0f}s")
else:
    F2 += 1; print(f"  FAIL property: {viol}/5000 advanced into the unindexed window")

print(f"search advance: {P2} passed, {F2} failed")
sys.exit(1 if (FAIL or F2) else 0)
PYEOF
