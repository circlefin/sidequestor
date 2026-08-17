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
health-monitor.py — the dead-man switch. Runs OUTSIDE triage.

Why this exists
───────────────
The whole system can be dead for hours while every surface still says it is fine. Two
ways that happens:

  * A crashing tick. `triage-loop.sh` swallows the orchestrator's exit code behind
    `|| true`, so if every tick crashes on import, launchd still reports a healthy
    long-running job.
  * launchd's StartInterval delivery silently stopping (a macOS update is enough to do
    it). Same outcome: nothing looks wrong.

Both share a property: a health check living inside triage cannot detect triage being
dead. So this runs as its own launchd job (`com.yaas.heartbeat`), shares no code path
with the triage loop, and is deliberately dependency-free: it reads state files and
shells out to `osascript`. Nothing it does can be broken by the thing it watches.

What it checks
──────────────
  triage_stalled     no tick has COMPLETED recently — the loop is dead or wedged
  tick_hung          a tick started and never finished (the .pth crash shape)
  tick_failures      the orchestrator has exited non-zero N times in a row
  checker_stuck      a watch's checker has failed enough to be promoted to misconfig
  approval_stuck     an approval has sat in `executing` past any plausible run
  health_events      misconfigured watch / budget breach / saturated window / breaker
  state_unreadable   last-run.json is missing or corrupt

Usage
─────
  health-monitor.py [--notify] [--repo <path>] [--json]

  --notify   fire desktop notifications (the launchd job passes this; tests do not)
  --repo     override the repo root, for tests
  --json     print the full status object rather than a one-line summary

Always writes state/health-status.json so the dashboard can render the same verdict.
Exits 0 when healthy, 1 when any condition is firing, so it is usable by hand and in
tests. Notification dedup lives in state/triage/health-alerts.json: each condition
re-notifies only when its signature changes or after a cooldown, so a persistent
fault does not shout every five minutes.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Thresholds. Defaults are deliberately loose enough not to cry wolf; every one is
# overridable so a slower install can tune it.
STALL_MIN     = float(os.environ.get("YAAS_HEALTH_STALL_MIN", "10"))
# Must exceed YAAS_TICK_DISPATCH_BUDGET (default 3600s = 60min), because a tick that
# dispatches several targets can legitimately run that long. 75 gives it headroom.
HUNG_MIN      = float(os.environ.get("YAAS_HEALTH_HUNG_MIN", "75"))
FAIL_STREAK   = int(os.environ.get("YAAS_HEALTH_FAIL_STREAK", "5"))
CHECKER_PROMOTE = int(os.environ.get("YAAS_CHECKER_ERROR_PROMOTE", "6"))
APPROVAL_STUCK_MIN = float(os.environ.get("YAAS_HEALTH_APPROVAL_STUCK_MIN", "45"))
EVENT_LOOKBACK_MIN = float(os.environ.get("YAAS_HEALTH_EVENT_LOOKBACK_MIN", "60"))
COOLDOWN_MIN  = float(os.environ.get("YAAS_HEALTH_COOLDOWN_MIN", "360"))

WATCHED_EVENTS = {
    "gate_watch_misconfigured":      "a watch is misconfigured and has stopped being checked",
    "gate_budget_exceeded":          "a spend/dispatch ceiling was hit; dispatch is withheld",
    "gate_watch_backlog":            "a watch's window saturated; its cursor is held",
    "gate_target_breaker_open":      "a target's hourly breaker opened",
    "gate_ack_manifest_unreadable":  "an ack manifest could not be read; work is held",
    "gate_quest_unreadable":         "a quest's watch.json is invalid",
}


def _now():
    return datetime.now(timezone.utc)


def _parse(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_min(stamp):
    t = _parse(stamp)
    return None if t is None else (_now() - t).total_seconds() / 60.0


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


class Health:
    def __init__(self, repo: Path):
        self.repo = repo
        self.state = repo / "state"
        self.problems = []   # (key, signature, headline, detail)
        self.notes = []

    def flag(self, key, signature, headline, detail=""):
        self.problems.append({"key": key, "signature": str(signature),
                              "headline": headline, "detail": detail})

    # ── the triage loop itself ────────────────────────────────────────────────
    def check_triage_liveness(self):
        path = self.state / "triage" / "last-run.json"
        d = _read_json(path)
        if not isinstance(d, dict):
            self.flag("state_unreadable", "missing" if not path.exists() else "corrupt",
                      "triage state unreadable",
                      f"{path} is {'missing' if not path.exists() else 'not valid JSON'}")
            return

        completed = d.get("last_triage_completed_utc")
        started   = d.get("tick_started_utc")

        comp_age = _age_min(completed)
        if comp_age is None:
            self.flag("triage_stalled", "no-completion-stamp",
                      "triage has never recorded a completed tick",
                      "last_triage_completed_utc is missing or unparseable")
        elif comp_age > STALL_MIN:
            self.flag("triage_stalled", f"{int(comp_age)}m",
                      "triage is not running",
                      f"no tick has completed for {int(comp_age)} minutes "
                      f"(threshold {int(STALL_MIN)}m). Check: launchctl list com.yaas.triage")
        else:
            self.notes.append(f"last tick completed {comp_age:.1f}m ago")

        # A tick that STARTED but never finished is the crash-loop shape that went
        # undetected for 6.5 hours. It is only meaningful when the start stamp is
        # newer than the completion stamp.
        st, cp = _parse(started), _parse(completed)
        if st and (cp is None or st > cp):
            hung_for = (_now() - st).total_seconds() / 60.0
            if hung_for > HUNG_MIN:
                self.flag("tick_hung", f"{int(hung_for)}m",
                          "a triage tick started and never finished",
                          f"tick began {int(hung_for)} minutes ago and has not completed "
                          f"(threshold {int(HUNG_MIN)}m). Check logs/worker-latest.log")

    def check_tick_failures(self):
        path = self.state / "triage" / "consecutive-tick-failures"
        try:
            n = int(path.read_text().strip() or 0)
        except Exception:
            return
        if n >= FAIL_STREAK:
            self.flag("tick_failures", str(n),
                      f"the orchestrator has failed {n} ticks in a row",
                      "triage-loop.sh discards exit codes, so this counter is the only "
                      "signal. Check logs/triage.err.log")
        elif n:
            self.notes.append(f"{n} consecutive tick failure(s), under the threshold")

    # ── work that has silently stopped moving ────────────────────────────────
    def check_checker_health(self):
        d = _read_json(self.state / "triage" / "checker-health.json", {}) or {}
        stuck = {k: v for k, v in d.items()
                 if isinstance(v, dict) and int(v.get("consecutive_errors", 0)) >= CHECKER_PROMOTE}
        if stuck:
            first = next(iter(stuck.items()))
            self.flag("checker_stuck", ",".join(sorted(stuck)),
                      f"{len(stuck)} watch checker(s) have failed past the promotion threshold",
                      f"e.g. {first[0]}: {first[1].get('last_error', '')[:90]}")
        elif d:
            self.notes.append(f"{len(d)} watch(es) in checker backoff, none promoted")

    def check_stuck_approvals(self):
        d = _read_json(self.state / "pending-approvals.json", {}) or {}
        stuck = []
        for item in d.get("items", []):
            if not isinstance(item, dict) or item.get("status") != "executing":
                continue
            # lease_expires_at is item 7's mechanism and may not exist yet; fall back
            # to the age of executing_at, which does.
            lease = _parse(item.get("lease_expires_at"))
            if lease is not None:
                if lease < _now():
                    stuck.append(item)
                continue
            age = _age_min(item.get("executing_at"))
            if age is not None and age > APPROVAL_STUCK_MIN:
                stuck.append(item)
        if stuck:
            ids = ",".join(sorted(i.get("id", "?") for i in stuck))
            self.flag("approval_stuck", ids,
                      f"{len(stuck)} approved action(s) stuck mid-execution",
                      "its execution lease has expired, so the send may or may not have "
                      f"landed and it needs reconciling rather than resending. ids: {ids[:120]}")

    def check_recent_events(self):
        path = self.state / "run-log.ndjson"
        if not path.exists():
            return
        cutoff = _now() - timedelta(minutes=EVENT_LOOKBACK_MIN)
        seen = {}
        try:
            with open(path) as f:
                for line in f:
                    if not line.startswith("{"):
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(e, dict):
                        continue
                    ev = e.get("event")
                    if ev not in WATCHED_EVENTS:
                        continue
                    t = _parse(e.get("ts"))
                    if t is None or t < cutoff:
                        continue
                    seen.setdefault(ev, []).append(e)
        except OSError:
            return
        for ev, hits in seen.items():
            detail = hits[-1].get("reason") or hits[-1].get("quest") or hits[-1].get("target") or ""
            self.flag(f"event:{ev}", f"{len(hits)}@{hits[-1].get('ts','')}",
                      WATCHED_EVENTS[ev],
                      f"{len(hits)} in the last {int(EVENT_LOOKBACK_MIN)}m — {str(detail)[:100]}")

    def run(self):
        self.check_triage_liveness()
        self.check_tick_failures()
        self.check_checker_health()
        self.check_stuck_approvals()
        self.check_recent_events()
        return self


def _should_notify(alerts: dict, prob: dict) -> bool:
    """Alert on a genuinely new condition, on a changed signature, or after the
    cooldown. Without this a permanent fault would notify every five minutes and get
    muted by the human, which is worse than not alerting at all."""
    prev = alerts.get(prob["key"])
    if not isinstance(prev, dict):
        return True
    if prev.get("signature") != prob["signature"]:
        return True
    age = _age_min(prev.get("at"))
    return age is None or age > COOLDOWN_MIN


def _notify(headline, detail, cmd=None):
    title, subtitle, body = "YAAS health", headline, detail[:200]
    if cmd:
        subprocess.run([cmd, title, subtitle, body], check=False)
        return
    script = (f'display notification {json.dumps(body)} '
              f'with title {json.dumps(title)} subtitle {json.dumps(subtitle)}')
    subprocess.run(["osascript", "-e", script], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def main():
    args = sys.argv[1:]
    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default



    repo = Path(opt("--repo") or os.environ.get("YAAS_HEALTH_REPO_ROOT")
                or _repo_root(__file__))
    notify_cmd = os.environ.get("YAAS_HEALTH_NOTIFY_CMD")

    h = Health(repo).run()
    healthy = not h.problems
    status = {
        "ts": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "healthy": healthy,
        "problems": h.problems,
        "notes": h.notes,
    }

    # Always publish, so the dashboard shows the same verdict the notifier acted on.
    out = repo / "state" / "health-status.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        os.replace(tmp, out)
    except OSError as exc:
        print(f"warn: could not write {out}: {exc}", file=sys.stderr)

    if "--notify" in args and h.problems:
        alerts_path = repo / "state" / "triage" / "health-alerts.json"
        alerts = _read_json(alerts_path, {}) or {}
        fired = 0
        for prob in h.problems:
            if not _should_notify(alerts, prob):
                continue
            _notify(prob["headline"], prob["detail"], notify_cmd)
            alerts[prob["key"]] = {"signature": prob["signature"], "at": status["ts"]}
            fired += 1
        # Drop bookkeeping for conditions that have cleared, so a recurrence alerts
        # again rather than being suppressed by a stale cooldown.
        live = {p["key"] for p in h.problems}
        alerts = {k: v for k, v in alerts.items() if k in live}
        try:
            alerts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = alerts_path.with_name(alerts_path.name + ".tmp")
            tmp.write_text(json.dumps(alerts, indent=2))
            os.replace(tmp, alerts_path)
        except OSError:
            pass
        if fired:
            print(f"notified {fired} condition(s)", file=sys.stderr)

    if "--json" in args:
        print(json.dumps(status, indent=2))
    elif healthy:
        print("healthy — " + "; ".join(h.notes) if h.notes else "healthy")
    else:
        for prob in h.problems:
            print(f"PROBLEM {prob['key']}: {prob['headline']} — {prob['detail']}")

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
