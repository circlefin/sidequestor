#!/usr/bin/env python3
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

"""
catchup.py — after a long silence, read everything before answering anything.

THE PROBLEM. Stop triage for a week and 500-1000 messages pile up. Watermarks hold, so
nothing is lost, but on resume the checkers hand the worker the OLDEST unseen slice first
(they must: a watermark can only cross a prefix of the gap). Left alone, the worker answers
a seven-day-old question and then walks forward through the backlog, every reply blind to
the hundreds of messages that followed it. In live customer threads.

TWO DEFENCES, and this file is the second.

  1. Per-message, always on: surfaces/slack-send.py holds any reply to a thread that has
     been quiet longer than YAAS_STALE_REPLY_HOURS (24 by default). That makes a backlog
     SAFE — the replies become drafts instead of sends.

  2. This file, for a long gap specifically: triage stops before it dispatches or commits
     ANYTHING, writes a digest of what accumulated, and waits for a human to release it.
     That makes a backlog VISIBLE, and puts the decision about a week of missed
     conversation where it belongs.

Defence 1 alone would still generate dozens of drafts about resolved threads. Defence 2
alone would leave a slow trickle of stale sends after release. They are complementary.

WHY IT HOLDS EVERYTHING, INCLUDING CLEAN WATERMARKS. "Nothing is sent or committed" is the
stronger and simpler promise. Advancing clean watches while holding dirty ones would mean a
half-applied tick, and the state after an interrupted catch-up would depend on how far it
got. Holding all of it means release is a clean resume from exactly where the pause began.

Commands:
    detect            arm catch-up if the gap exceeds the threshold; prints JSON
    status            print the current state; exit 0 iff a hold is active
    digest <json>     write/refresh the human-readable digest
    release           clear the hold and let the next tick run normally
    clear             remove all catch-up state (for tests and fresh installs)

Env:
    YAAS_CATCHUP_AFTER_HOURS   gap that arms a hold (default 6)
    YAAS_CATCHUP_REPO_ROOT     point at a fixture tree (tests)
"""

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_AFTER_HOURS = 6.0

# Events a tick writes WITHOUT doing any work. Counting these as activity would mask a real
# silence: a tick that refuses to start still logs.
NON_ACTIVITY_EVENTS = {
    "gate_bad_env_knob",    # refused to run on a malformed ceiling
    "gate_skip_locked",     # another tick held the lock
    "triage_paused",        # deliberately stopped
    "gate_catchup_hold",    # the hold itself
}


def _repo_root(start):
    """The repo root is the nearest ancestor directory that contains yaas-triage/.

    NOT counted as `parent.parent`: that is correct only while every script sits directly
    in yaas-triage/, and silently resolves to yaas-triage/ itself once a script moves into
    a subdirectory, producing a parallel state/ tree nothing reads. NOT keyed on CLAUDE.md
    (a fresh clone has only CLAUDE.example.md) and NOT on .git (two git dirs here, none in
    fixtures). Ambient $REPO_ROOT is deliberately ignored: a stale value pointing at another
    checkout would pass any marker check and silently redirect writes. Test fixtures copy
    the whole tree, so the walk-up finds the fixture on its own.

    Kept byte-identical across every file that needs it; tests/behaviour/repo-root.test.sh
    asserts that, because a shared module would need sys.path handling whose own path is
    depth-dependent, which is the bug being fixed.
    """
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


def repo():
    return Path(os.environ.get("YAAS_CATCHUP_REPO_ROOT") or _repo_root(__file__))


def paths():
    r = repo()
    return (r / "state" / "catchup.json",
            r / "state" / "catchup-digest.md",
            r / "state" / "run-log.ndjson",
            r / "state" / "triage" / "last-run.json")


def _load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _parse_iso(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception:
        return None


def last_activity():
    """When did triage last actually do something?

    Deliberately takes the MAX of two sources rather than trusting one. `last-run.json`'s
    completed_utc is not reliably populated (observed null on a live install), and the run
    log can be rotated. Taking the newest of both means a missing or stale field cannot fake
    a week-long gap and trigger a spurious hold.
    """
    _, _, runlog, lastrun = paths()
    newest = None

    lr = _load(lastrun, {})
    for key in ("completed_utc", "tick_started_utc", "ts"):
        e = _parse_iso(str(lr.get(key) or ""))
        if e and (newest is None or e > newest):
            newest = e

    try:
        # Only the tail matters, and the file can be large.
        lines = Path(runlog).read_text().splitlines()[-200:]
    except Exception:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        # Only events that mean a tick actually DID something count as activity. A tick that
        # refused to run still writes to this log, so counting every event lets six hours of
        # bad-env failures or lock contention masquerade as a healthy system — and then the
        # whole backlog dispatches at once the moment it is fixed, with no hold.
        if rec.get("event") in NON_ACTIVITY_EVENTS:
            continue
        e = _parse_iso(str(rec.get("ts") or ""))
        if e and (newest is None or e > newest):
            newest = e
    return newest


def cmd_detect(now=None):
    now = now if now is not None else time.time()
    state_p, _, _, _ = paths()
    st = _load(state_p, {})
    after = float(os.environ.get("YAAS_CATCHUP_AFTER_HOURS") or DEFAULT_AFTER_HOURS)

    if st.get("status") == "awaiting_release":
        print(json.dumps({"armed": True, "status": "awaiting_release",
                          "gap_hours": st.get("gap_hours"), "armed_at": st.get("armed_at")}))
        return 0

    seen = last_activity()
    if seen is None:
        # A fresh install has no history; a gap cannot be measured, so never hold.
        print(json.dumps({"armed": False, "reason": "no prior activity recorded"}))
        return 0

    gap_h = (now - seen) / 3600.0

    # After a release, the gap is still enormous until a normal tick logs something. Without
    # this the very next tick would re-arm the hold and it could never be cleared.
    resume_after = st.get("resume_after_epoch")
    if resume_after and seen <= float(resume_after):
        print(json.dumps({"armed": False, "reason": "just released; waiting for a normal tick",
                          "gap_hours": round(gap_h, 2)}))
        return 0

    if gap_h < after:
        print(json.dumps({"armed": False, "gap_hours": round(gap_h, 2), "threshold_hours": after}))
        return 0

    st = {"status": "awaiting_release", "armed_at": _iso(now),
          "gap_hours": round(gap_h, 2), "threshold_hours": after,
          "last_activity_utc": _iso(seen)}
    _write_atomic(state_p, json.dumps(st, indent=2) + "\n")
    print(json.dumps({"armed": True, "status": "awaiting_release",
                      "gap_hours": round(gap_h, 2), "newly_armed": True}))
    return 0


def cmd_status():
    state_p, digest_p, _, _ = paths()
    st = _load(state_p, {})
    active = st.get("status") == "awaiting_release"
    print(json.dumps({"active": active, **st,
                      "digest": str(digest_p) if digest_p.exists() else None}))
    return 0 if active else 1


def cmd_digest(payload_json):
    """Write the human-readable digest of what accumulated during the silence."""
    state_p, digest_p, _, _ = paths()
    st = _load(state_p, {})
    try:
        p = json.loads(payload_json)
    except Exception:
        p = {}

    items = p.get("dirty") or []
    lines = [
        "# Catch-up digest",
        "",
        f"Triage was silent for **{st.get('gap_hours', '?')} hours** "
        f"(last activity {st.get('last_activity_utc', '?')}). It has checked every watch and "
        "is now **holding**: nothing has been sent, and no watermark has moved.",
        "",
        f"Armed at {st.get('armed_at', '?')}. Quests checked: {p.get('quests_checked', '?')}.",
        "",
        "## What arrived while it was down",
        "",
    ]
    if not items:
        lines.append("Nothing new on any watch. Releasing will simply resume normal ticks.")
    else:
        lines.append(f"{len(items)} watch(es) have new activity:")
        lines.append("")
        by_quest = {}
        for it in items:
            by_quest.setdefault(it.get("quest_id", "?"), []).append(it)
        for q in sorted(by_quest):
            lines.append(f"### {q}")
            for it in by_quest[q]:
                detail = (it.get("detail") or "").replace("\n", " ").strip()[:150]
                lines.append(f"- {detail or it.get('type', '?')}")
            lines.append("")

    lines += [
        "## What happens when you release",
        "",
        "Normal ticks resume from exactly where the pause began, so nothing is skipped.",
        "Every reply to a thread that has been quiet more than "
        f"{os.environ.get('YAAS_STALE_REPLY_HOURS', '24')}h is routed to the approval queue "
        "instead of being sent, so a stale answer cannot go out on its own.",
        "",
        "```",
        "python3 yaas-triage/ops/catchup.py release",
        "```",
        "",
        "Until then triage checks its watches every tick and changes nothing.",
    ]
    _write_atomic(digest_p, "\n".join(lines) + "\n")
    print(str(digest_p))
    return 0


def cmd_release(now=None):
    now = now if now is not None else time.time()
    state_p, digest_p, _, _ = paths()
    st = _load(state_p, {})
    if st.get("status") != "awaiting_release":
        print("no catch-up hold is active")
        return 1
    st.update({"status": "released", "released_at": _iso(now),
               # Suppress immediate re-arming: the gap is still large until a tick logs.
               "resume_after_epoch": now})
    _write_atomic(state_p, json.dumps(st, indent=2) + "\n")
    print(f"released (held {st.get('gap_hours', '?')}h of backlog); next tick runs normally")
    return 0


def cmd_clear():
    state_p, digest_p, _, _ = paths()
    for p in (state_p, digest_p):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    print("catch-up state cleared")
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "detect":
        return cmd_detect()
    if cmd == "status":
        return cmd_status()
    if cmd == "digest":
        return cmd_digest(args[1] if len(args) > 1 else "{}")
    if cmd == "release":
        return cmd_release()
    if cmd == "clear":
        return cmd_clear()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
