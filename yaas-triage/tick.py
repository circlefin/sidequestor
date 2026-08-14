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
tick.py — the yaas idle-triage orchestrator (v3), a faithful port of the original shell orchestrator.

This is the coordinator that the original shell orchestrator was: it loops every active quest's watch.json, runs the
per-type checkers, decides per watch whether the watermark holds or advances, and — when there
is genuinely new activity — dispatches one paid worker per dirty target and commits only the
watermarks the worker's acks earned. Same gates (lock, bad-env, idle, budget, slack), same
evidence-based commit, same fairness rotation. (Stale replies are handled by slack-send.py's
always-on 24h draft guard, not by a whole-tick hold.)

What changed is HOW the decisions are made, not WHICH: the risky pure logic now lives in tested
modules (tick_state / tick_check / tick_dispatch, and the pre-existing ledger/dispatch helpers),
and this file is the sequencer that calls them and does the I/O they deliberately avoid — the
watch.json writes, the run-log events, the subprocess fan-out. The differential harness held
this file to the same goldens the original shell orchestrator produced (`run.sh check tick.py`),
so the port is behaviour-preserving by construction: same watch movements, same run-log event
sequence, same ack manifests, same exit code.

This is the live orchestrator — `triage-loop.sh` runs it. The port is complete and validated;
the original shell orchestrator has been retired to archive/.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import tick_state
import tick_check
import tick_dispatch
from reaction_config import load_reaction_emojis


# ── small I/O helpers ────────────────────────────────────────────────────────

def _now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Tick:
    """One triage tick. Holds the derived config + clock and the accumulated check results,
    and owns every side effect (run-log, watch.json, subprocess). Mirrors the original shell orchestrator's globals
    as instance attributes so the control flow reads the same top to bottom."""

    def __init__(self):
        self.cfg = tick_state.Config(str(_HERE))
        r = self.cfg.repo_root
        self.repo_root = r
        self.script_dir = _HERE
        self.quests_dir = self.cfg.quests_dir
        self.triage_state = self.cfg.triage_state
        self.run_log = self.cfg.run_log
        self.log_dir = self.cfg.log_dir
        self.log_file = self.cfg.log_file
        self.manifest_dir = self.cfg.manifest_dir
        self.unacked_file = self.manifest_dir / "unacked-counts.json"
        self.checker_health = self.manifest_dir / "checker-health.json"
        self.pending_reactions = self.manifest_dir / "pending_reactions.json"
        self.approvals_file = r / "state" / "pending-approvals.json"
        self.lag_map = self.cfg.lag_map
        # cfg.env is os.environ with REPO_ROOT/.env merged in (real env wins), exactly what
        # the original shell orchestrator gets from `set -a; source .env`. Subprocesses inherit it, and the agent
        # backend + knobs are read from it — NOT bare os.environ, which misses .env-only vars
        # like YAAS_AGENT (the fixture sets it to "stub"; the shell honours that, so must we).
        self.env = dict(self.cfg.env)
        try:
            self.reaction_emojis = load_reaction_emojis(self.env)
        except ValueError as exc:
            raise SystemExit(f"invalid reaction emoji configuration: {exc}")
        # Knobs (validated in Config.__init__; here as ints/strings for the flow).
        self.agent = self.env.get("YAAS_AGENT", "claude")
        self.unacked_promote = self.cfg.knob("YAAS_UNACKED_PROMOTE")
        self.error_promote = self.cfg.knob("YAAS_CHECKER_ERROR_PROMOTE")
        self.max_parallel = self.cfg.knob("YAAS_TRIAGE_MAX_PARALLEL")
        self.max_fanout = self.cfg.knob("YAAS_MAX_DISPATCH_FANOUT")
        self.tick_budget = self.cfg.knob("YAAS_TICK_DISPATCH_BUDGET")
        self.min_slice = self.cfg.knob("YAAS_MIN_DISPATCH_SLICE")
        self.worker_timeout = int(self.env.get("YAAS_WORKER_TIMEOUT", "1800") or "1800")
        self.now_utc = _now_utc()
        self.now_ts = time.time()
        # subprocess env: mirror the exports the original shell orchestrator makes.
        self.env["MCP_CALL"] = str(self.script_dir / "surfaces" / "mcp-call.sh")
        self.env.setdefault("YAAS_AGENT", self.agent)
        self.mcp_call = self.env["MCP_CALL"]
        # snapshot checker-health once (in-memory), like the shell.
        self.checker_health_json = self._read_json(self.checker_health, {})
        if not isinstance(self.checker_health_json, dict):
            self.checker_health_json = {}
        # accumulators
        self.dirty_quests = []
        self.clean_watches = []   # {quest_id, watch_id, type, advance_to}
        self.dirty_watches = []   # {quest_id, watch_id, type, checker_outcome, advance_to, complete}
        self.skipped_quests = []
        self.results = []         # every per-watch row (dicts), for counters + digest
        self.reactions_dirty = False
        self.dispatch_run_id = ""
        self.dispatch_exit = 1
        self.dispatch_wall = 0
        # Last worker-visible failure text for the run in flight, recorded onto the no-progress
        # counter so the dashboard can show WHY a watch is backing off, not just that it is.
        self.dispatch_last_error = ""
        self.dispatch_start_utc = ""
        self.dispatch_slack_read_ok = 0
        self.dispatched = 0
        self._log_lock = threading.Lock()

    # ---- logging / io ----
    def log(self, msg):
        line = f"{_now_utc()}  {msg}\n"
        # A lock so concurrent quest checks (run_tick's ThreadPoolExecutor) cannot interleave
        # partial lines into triage.log / stderr.
        with self._log_lock:
            try:
                with open(self.log_file, "a") as f:
                    f.write(line)
            except OSError:
                pass
            sys.stderr.write(line)

    def slog(self, msg):
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())}  {msg}")

    def event(self, obj):
        """Append one run-log event. `ts` is stamped here so callers pass only the payload.
        Compact separators to match the original shell orchestrator's `jq -c`/`echo` output, so the run-log stays a
        single uniform format and grep-based consumers keep working."""
        rec = {"ts": self.now_utc}
        rec.update(obj)
        try:
            with open(self.run_log, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    @staticmethod
    def _read_json(path, default=None):
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return default

    def run(self, argv, capture=True, check=False):
        """Run a helper subprocess with the tick's env. Returns CompletedProcess."""
        return subprocess.run(argv, capture_output=capture, text=True, env=self.env, check=check)

    def py(self, *args):
        return ["python3", *[str(a) for a in args]]

    def helper(self, *parts):
        return str(self.script_dir.joinpath(*parts))

    # ---- state counters (best-effort, matching the shell's inline python) ----
    def _bump_state(self, **kv):
        d = self._read_json(self.triage_state, {}) or {}
        if not isinstance(d, dict):
            d = {}
        for k, v in kv.items():
            if v is None:
                continue
            if k.endswith("+"):
                base = k[:-1]
                d[base] = int(d.get(base, 0)) + v
            else:
                d[k] = v
        try:
            tmp = str(self.triage_state) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, self.triage_state)
        except OSError:
            pass


# ── The single place a watermark ever moves ──────────────────────────────────
# Both the clean path and the post-dispatch commit call this. One writer, one rule: use the
# checker's own advance_to when it gave one, else advance to now minus the type's lag. Mirrors
# the original shell orchestrator _advance_watches (append-only to the watermark field; never touches other fields).
def advance_watches(t, qid, moves):
    """moves: list of {watch_id, advance_to}. Returns True on success (or nothing to do)."""
    watch = t.quests_dir / qid / "watch.json"
    if not watch.exists() or not moves:
        return True
    data = t._read_json(watch, None)
    if not isinstance(data, dict):
        t.log(f"WATCH WRITE FAILED: {qid} — unreadable watch.json")
        return False
    by_id = {m["watch_id"]: m for m in moves}
    for w in data.get("watches", []) or []:
        m = by_id.get(w.get("watch_id"))
        if m is None:
            continue
        adv = m.get("advance_to")
        if adv is not None and str(adv) != "":
            w["last_checked_ts"] = str(adv)
        else:
            lag = t.lag_map.get(w.get("type"), 0)
            w["last_checked_ts"] = str(t.now_ts - lag)
    try:
        tmp = watch.parent / f".watch.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, watch)
    except OSError:
        t.log(f"WATCH WRITE FAILED: {qid} — {len(moves)} watermark(s) not advanced")
        return False
    return True


# ── Per-quest check ───────────────────────────────────────────────────────────
# Ports the original shell orchestrator check_quest: run each watch's checker (local watches before slack_ ones so a
# rate-limit skip can't shadow a local dirty signal), turn each result into a verdict via the
# pure tick_check.classify(), and layer the side effects classify() deliberately omits: persist
# checker-health on error/recovery, and STOP the quest's remaining watches after a ratelimit.
# Emits per-watch rows (dicts) onto t.results.
def check_quest(t, qid):
    """Check one quest's watches and RETURN its list of result rows. Returns rather than
    mutating shared state so quests can run concurrently (see run_tick's ThreadPoolExecutor)
    and their rows be reassembled in a deterministic quest order. Side effects it does make —
    checker-health persist/recover and t.log — are individually thread-safe."""
    rows = []
    watch = t.quests_dir / qid / "watch.json"
    if not watch.exists():
        rows.append({"qid": qid, "status": "clean", "reason": "no_watch_file"})
        return rows
    data = t._read_json(watch, None)
    if not isinstance(data, dict):
        rows.append({"qid": qid, "status": "skip", "reason": "unreadable watch.json"})
        return rows
    watches = data.get("watches", []) or []
    # Grouping by slack_ prefix is intentional here: the manifest's upstream field is the
    # declaration, and checker-contract.test.sh asserts the prefix and upstream stay aligned.
    # local (non-slack) first, then slack_ — the ordering that stops a rate-limit skip from
    # short-circuiting a local dirty/approval watch at the array tail.
    order = ([w for w in watches if not str(w.get("type", "")).startswith("slack_")]
             + [w for w in watches if str(w.get("type", "")).startswith("slack_")])
    slack_expected = sum(1 for w in watches if str(w.get("type", "")).startswith("slack_"))
    slack_succeeded = 0
    had_dirty = had_skip = False

    unacked_counts = t._read_json(t.unacked_file, {}) or {}
    if not isinstance(unacked_counts, dict):
        unacked_counts = {}

    for entry in order:
        wid = entry.get("watch_id")
        wtype = entry.get("type", "unknown")
        # health for this watch: in_backoff (next_retry_ts still ahead) + consecutive_errors.
        hrec = t.checker_health_json.get(wid) if wid else None
        health = None
        if isinstance(hrec, dict):
            health = dict(hrec)
            health["in_backoff"] = not tick_check.is_due(hrec, t.now_ts)
        unacked = 0
        # Due unless a no-progress backoff window is still open. Mirrors the checker-health
        # in_backoff computation directly above, on purpose: same shape, same failure mode,
        # same reasoning — a repeatedly-failing thing is retried at a decaying rate, never
        # abandoned and never parked for a human.
        unacked_due = True
        if wid:
            urec = unacked_counts.get(f"{qid}|{wid}")
            if isinstance(urec, dict):
                try:
                    unacked = int(urec.get("count", 0))
                except (TypeError, ValueError):
                    unacked = 0
                unacked_due = tick_check.is_due(urec, t.now_ts)
        checker = t.script_dir / "checkers" / f"{wtype}.py"
        checker_exists = os.access(checker, os.X_OK)

        # Structural gates first — exactly the shell's order — and only run the checker once
        # they pass (so a held watch costs nothing and side-effects nothing). Each maps to a
        # classify() verdict, but we decide them inline because whether to EXEC the checker
        # depends on them; classify then owns the result→verdict routing.
        structural = tick_check.structural_verdict(entry, health, unacked, unacked_due,
                                                   t.unacked_promote, checker_exists)
        if structural is not None:
            # A structural gate fired; all of them precede classify's None→error branch, so
            # classify(None) returns exactly that structural verdict (misconfig or in-backoff).
            verdict = structural
        else:
            cp = t.run(t.py(checker, json.dumps(entry)))
            result = _parse_checker((cp.stdout or "").strip())
            verdict = tick_check.classify(result, entry, health=health, unacked=unacked,
                                          unacked_due=unacked_due,
                                          unacked_promote=t.unacked_promote,
                                          error_promote=t.error_promote,
                                          checker_exists=checker_exists)

        v = verdict["verdict"]
        reason = verdict.get("reason", "")

        if v == tick_check.MISCONFIG:
            rows.append({"qid": qid, "status": "misconfig", "watch_id": wid,
                              "type": wtype, "reason": reason})
            had_skip = True
            # persist the error side effect if this was the error-promotion path
            if verdict.get("error"):
                t.run(t.py(t.helper("ledger", "checker-health.py"), "fail", wid, reason))
            # No-progress no longer reaches here: it backs off forever instead of parking as
            # misconfig, so it never needs to ask a human for anything. The remaining misconfig
            # reasons (bad watch_id, missing checker) are repo defects, not stranded work.
            continue
        if v == tick_check.SKIP:
            # `ratelimited: True` distinguishes a genuine Slack rate-limit skip from the other
            # skip rows (watch_id migration failure, unreadable watch.json), so analyze() emits
            # gate_watch_ratelimited only for the real thing — the signal the dashboard surfaces.
            rows.append({"qid": qid, "status": "skip", "watch_id": wid,
                              "type": wtype, "reason": reason, "ratelimited": True})
            had_skip = True
            break  # a rate-limit stops further slack calls in this quest
        if v == tick_check.HOLD:
            # complete=False so the watches_truncated counter (which keys off complete is False)
            # counts held-undrained windows, matching the original shell orchestrator — a hold is by definition an
            # undrained window.
            rows.append({"qid": qid, "status": "hold", "watch_id": wid,
                              "type": wtype, "reason": reason, "complete": False})
            had_skip = True
            if str(wtype).startswith("slack_"):
                slack_succeeded += 1
            continue
        if v == tick_check.BACKOFF:
            rows.append({"qid": qid, "status": "backoff", "watch_id": wid,
                              "type": wtype, "reason": reason})
            had_skip = True
            if verdict.get("error"):
                t.run(t.py(t.helper("ledger", "checker-health.py"), "fail", wid, reason))
            continue

        # clean or dirty: a successful checker read. Count slack coverage + recover health.
        if str(wtype).startswith("slack_"):
            slack_succeeded += 1
        if wid and wid in t.checker_health_json:
            t.run(t.py(t.helper("ledger", "checker-health.py"), "ok", wid))
            t.log(f"CHECKER RECOVERED: {qid} [{wtype}] {wid}")

        advance_to = verdict.get("advance_to")
        complete = verdict.get("complete", True)
        if v == tick_check.DIRTY:
            rows.append({"qid": qid, "status": "dirty", "watch_id": wid, "type": wtype,
                              "advance_to": advance_to, "complete": complete, "reason": reason})
            had_dirty = True
        else:  # CLEAN
            rows.append({"qid": qid, "status": "ok", "watch_id": wid, "type": wtype,
                              "advance_to": advance_to, "complete": complete, "reason": reason})

    if slack_expected > 0 and slack_succeeded == slack_expected:
        rows.append({"qid": qid, "status": "source_recovered", "type": "slack",
                          "reason": "all Slack watches checked successfully"})
    if not had_dirty and not had_skip:
        rows.append({"qid": qid, "status": "clean", "reason": "all_checks_passed"})
    return rows


def _parse_checker(raw):
    """Two accepted shapes: one line of result.py JSON, or legacy `count|preview`. Returns the
    dict tick_check.classify expects, or None if unparseable (classify routes None → error)."""
    if not raw:
        return {"outcome": "error", "preview": "checker produced no output"}
    if raw.lstrip().startswith("{"):
        try:
            d = json.loads(raw)
        except ValueError:
            return {"outcome": "error", "preview": "malformed checker json"}
        reason = d.get("reason") or ""
        preview = d.get("preview") or ""
        if reason:
            preview = f"{preview} — {reason}" if preview else reason
        d["preview"] = preview
        return d
    # legacy count|preview
    count, _, preview = raw.partition("|")
    try:
        n = int(count)
        return {"outcome": "dirty" if n > 0 else "clean", "count": n, "preview": preview}
    except ValueError:
        # a bare word like "error|..." — treat the head as the outcome
        return {"outcome": count, "preview": preview}


# The tick's connectivity precondition. Probed against the API the configured worker backend
# actually needs, not a generic ping target: a captive-portal wifi that resolves and routes but
# cannot reach the model API is, for our purposes, offline, and a generic probe would call it
# online. Keyed by YAAS_AGENT because this repo also dispatches to Codex and Cursor, and
# probing Anthropic on a Codex host would freeze a perfectly healthy system.
NETWORK_PROBE_HOSTS = {
    "claude": "api.anthropic.com",
    "codex":  "chatgpt.com",
    "cursor": "api2.cursor.sh",
}
NETWORK_PROBE_DEFAULT = "api.anthropic.com"
NETWORK_PROBE_PORT    = 443
NETWORK_PROBE_TIMEOUT = 3.0


def network_probe_host(t):
    """Which host this tick's connectivity depends on. `YAAS_NETWORK_PROBE_HOST` overrides,
    for a deployment behind a proxy or a backend this map does not know about."""
    override = str(t.cfg.env.get("YAAS_NETWORK_PROBE_HOST", "")).strip()
    if override:
        return override
    return NETWORK_PROBE_HOSTS.get(str(t.agent).strip().lower(), NETWORK_PROBE_DEFAULT)


def have_network(t):
    """True if a TCP connection to the worker backend's API can be opened right now.

    FAILS OPEN on anything that is not unambiguously a network failure. A probe that wrongly
    reports "offline" silently stops the entire orchestrator, which is far worse than the
    wasted dispatch it exists to prevent. So only DNS-resolution failure, connection refusal /
    unreachability, and timeout count as offline; every other OSError (fd exhaustion, a
    sandbox denial, anything else local) is treated as "probe inconclusive, carry on".

    Bounded by a watchdog thread rather than by the socket timeout alone, because
    `create_connection` resolves the name BEFORE the timeout applies to anything — a stalled
    resolver would otherwise hang the tick well past NETWORK_PROBE_TIMEOUT. A probe that does
    not answer in time is inconclusive, so it too fails open.
    """
    if str(t.cfg.env.get("YAAS_SKIP_NETWORK_PROBE", "")).strip() == "1":
        return True

    host = network_probe_host(t)
    verdict = {}

    def probe():
        try:
            with socket.create_connection((host, NETWORK_PROBE_PORT), NETWORK_PROBE_TIMEOUT):
                verdict["online"] = True
        except (socket.gaierror, socket.timeout, TimeoutError, ConnectionError) as e:
            verdict["online"] = False
            verdict["why"] = f"{type(e).__name__}: {e}"
        except OSError as e:
            # Unreachable network/host is a real offline signal; anything else local is not.
            if e.errno in (errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTDOWN,
                           errno.EHOSTUNREACH, errno.ENETRESET):
                verdict["online"] = False
                verdict["why"] = f"{type(e).__name__}: {e}"
            else:
                verdict["online"] = True
        except Exception:
            verdict["online"] = True

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    th.join(NETWORK_PROBE_TIMEOUT + 1.0)
    if "online" not in verdict:
        return True  # watchdog fired: inconclusive, fail open
    if not verdict["online"]:
        t.log(f"network probe {host}:{NETWORK_PROBE_PORT} failed — {verdict.get('why', '')}")
    return verdict["online"]


def run_tick(t):
    """The whole tick after config is loaded. Returns the process exit code."""
    # tick start stamp (health-monitor watches started-vs-completed)
    t._bump_state(tick_started_utc=t.now_utc)

    # Dashboard instructions are written to the approval ledger immediately, even
    # while a previous tick owns the global lock. Arm their watches only now, under
    # this tick's lock, so no concurrent watermark/housekeeping write can erase an
    # append made from the dashboard process.
    cp = t.run(t.py(t.helper("ledger", "approval-helper.py"),
                    "arm-pending-instructions"))
    if cp.returncode != 0:
        t.log(f"MANUAL QUEUE ARM FAILED — {(cp.stderr or cp.stdout or '').strip()[:300]}")

    # Nothing this tick does works without a network, and everything it does when the network
    # is missing is harmful: checkers fail, dispatches burn ~3 minutes each reaching nothing,
    # and every dispatched watch takes a no-progress strike it did not earn. So the offline
    # case is a SKIP, not a failure — the tick simply does not happen, exactly as if launchd
    # had not fired. Nothing is checked, nothing is dispatched, no counter moves, and the next
    # tick 60 seconds later tries again. A laptop that loses wifi for an hour therefore comes
    # back with its state untouched rather than with an hour of accumulated backoff.
    if not have_network(t):
        t.log("OFFLINE — no network; skipping this tick entirely (no checks, no dispatch).")
        t.event({"event": "gate_tick_offline", "probe": f"{network_probe_host(t)}:{NETWORK_PROBE_PORT}"})
        t.slog("Run OK — offline, tick skipped (will retry).")
        t._bump_state(**{"runs_offline+": 1})
        return 0

    quest_dirs = sorted(d for d in t.quests_dir.iterdir()
                        if d.is_dir()) if t.quests_dir.is_dir() else []
    quest_count = len(quest_dirs)
    t.log(f"Triage starting. Active quests: {quest_count}")

    # NOTE: zero active quests does NOT mean the tick is idle — the global reaction
    # sweep (below) is independent of quests. Returning here would silently stop the
    # bot from ever answering an emoji-triggered message whenever no quest is active
    # (bug: 2026-08-08, a process-reaction DM went unanswered while quest_count==0).
    # With an empty quest_dirs the check block below is a natural no-op, so we fall
    # through to the reaction sweep and the normal dispatch decision.

    # Ensure watch_ids (migrate legacy files); a failure marks the quest unreadable.
    unreadable = set()
    for qd in quest_dirs:
        qid = qd.name
        watch = qd / "watch.json"
        if not watch.exists():
            continue
        cp = t.run(t.py(t.helper("ledger", "ensure-watch-ids.py"), qid, str(watch)))
        if cp.returncode != 0:
            unreadable.add(qid)
            t.log(f"SKIP: {qid} — invalid watch.json; watch IDs could not be ensured")
            t.event({"event": "gate_quest_unreadable", "quest": qid,
                     "reason": "invalid watch.json; watch IDs could not be ensured"})

    # ── Check every quest, a few at a time ──────────────────────────────────────
    # Bounded concurrency (YAAS_TRIAGE_MAX_PARALLEL, default 3) is the peak number of
    # simultaneous Slack API calls, and low is deliberate: burst concurrency is what trips
    # Slack's rate-limit detection (the 2026-07-24 storm). Each check_quest RETURNS its rows,
    # which we reassemble in the original quest order so t.results — and therefore analyze() —
    # is deterministic regardless of which checker finished first. The dispatch order is
    # separately re-sorted anyway, so ordering here is about a stable log/diff, not correctness.
    checkable = [qd.name for qd in quest_dirs if qd.name not in unreadable]
    # Fairness rotation for the CHECK phase. The Slack budget runs out partway through a
    # tick, and with a fixed (alphabetical) order the same tail lost every time: on
    # 2026-08-09 four quests were rate-limited on 100% of ticks purely for sorting last.
    # Rotating the START each tick spreads that loss, so a quest waits a few ticks instead
    # of forever. Execution order only — results are reassembled in quest_dirs order below,
    # so every log, diff and golden stays deterministic.
    st0 = t._read_json(t.triage_state, {}) or {}
    check_cursor = st0.get("check_cursor", 0) if isinstance(st0, dict) else 0
    checkable, next_check_cursor = tick_check.rotate_check_order(checkable, check_cursor)
    # Persist BEFORE the checks, not after: a tick killed mid-check (watchdog, reboot, the
    # flock being stolen) would otherwise replay the same starting point next time and the
    # rotation would silently stop rotating for exactly the quests it exists to protect.
    if checkable:
        t._bump_state(check_cursor=next_check_cursor)
    computed = {}
    if checkable:
        with ThreadPoolExecutor(max_workers=max(1, t.max_parallel)) as pool:
            for qid, rows in zip(checkable, pool.map(lambda q: check_quest(t, q), checkable)):
                computed[qid] = rows
    # Reassemble in the original quest_dirs order (unreadable skips interleaved where they
    # belong), so t.results — and every log/diff built from it — is deterministic regardless
    # of which checker finished first.
    for qd in quest_dirs:
        qid = qd.name
        if qid in unreadable:
            t.results.append({"qid": qid, "status": "skip",
                              "reason": "watch_id migration failed; watermark held"})
        else:
            t.results.extend(computed.get(qid, []))

    analyze(t)

    # ── Global reaction sweep ───────────────────────────────────────────────────
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(t.now_ts - 60 * 86400))
    cp = t.run(t.py(t.helper("checkers", "reactions.py"), t.mcp_call, cutoff,
                    str(t.repo_root), str(t.pending_reactions)))
    react_out = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        t.log("REACTIONS checker failed to execute (non-fatal) — reaction sweep skipped this cycle")
    if "REACTIONS_TRUNCATED=1" in react_out:
        t.log("REACTIONS TRUNCATED — the emoji search hit its page cap; older reacted messages were not seen.")
        t.event({"event": "gate_watch_backlog", "quest": "reactions",
                 "reason": "reaction search truncated at page cap"})
    if t.pending_reactions.exists():
        t.reactions_dirty = True
        t.log(f"DIRTY: reactions — pending in {t.pending_reactions}")

    # ── Advance clean watch watermarks ──────────────────────────────────────────
    clean_by_quest = {}
    for w in t.clean_watches:
        clean_by_quest.setdefault(w["quest_id"], []).append(
            {"watch_id": w["watch_id"], "advance_to": w.get("advance_to")})
    for qid, moves in clean_by_quest.items():
        advance_watches(t, qid, moves)

    # ── Update check-phase counters + housekeeping ──────────────────────────────
    dirty_count = len(t.dirty_quests)
    clean_count = sum(1 for r in t.results if r["status"] == "clean")
    skipped_count = len(t.skipped_quests)
    watches_skipped = sum(1 for r in t.results
                          if r["status"] in ("skip", "misconfig", "backoff", "hold"))
    watches_misconfigured = sum(1 for r in t.results if r["status"] == "misconfig")
    watches_backoff = sum(1 for r in t.results if r["status"] == "backoff")
    watches_truncated = sum(1 for r in t.results
                            if r["status"] in ("dirty", "hold") and r.get("complete") is False)
    t._bump_state(**{"runs_total+": 1, "quests_checked": quest_count,
                     "quests_dirty": dirty_count, "quests_clean": clean_count,
                     "quests_skipped": skipped_count, "watches_skipped": watches_skipped,
                     "watches_misconfigured": watches_misconfigured,
                     "watches_in_backoff": watches_backoff,
                     "watches_truncated": watches_truncated})
    housekeep(t, quest_dirs)

    # ── Decide: idle? ────────────────────────────────────────────────────────────
    if dirty_count == 0 and not t.reactions_dirty:
        t.event({"event": "gate_idle", "quests_checked": quest_count,
                 "quests_skipped": skipped_count, "watches_skipped": watches_skipped,
                 "watches_misconfigured": watches_misconfigured,
                 "watches_in_backoff": watches_backoff, "watches_truncated": watches_truncated})
        t._bump_state(**{"runs_idle+": 1})
        t.log(f"IDLE — {quest_count} quest(s) checked, 0 dirty, {skipped_count} fully skipped "
              f"quest(s), {watches_skipped} held watch(es). Watermarks advanced where safe.")
        t.slog(f"Run OK — idle. {quest_count} quest(s) swept, 0 activity.")
        return 0

    # ── Build the dispatch target list (sorted quests + optional 'reactions') ──
    # Appended here for a stable, sorted target list; the dispatch loop moves it to the
    # FRONT after the fairness rotation, because a reaction is the one target a human is
    # actively waiting on. See the reordering below.
    dispatch_targets = sorted(t.dirty_quests)
    if t.reactions_dirty:
        dispatch_targets.append("reactions")
    targets_json = dispatch_targets

    if t.env.get("DRY_RUN", "0") == "1":
        t.event({"event": "gate_dirty_dry_run", "targets": targets_json,
                 "dirty_watches": t.dirty_watches})
        t.log(f"DRY_RUN=1 — would dispatch for {' '.join(dispatch_targets)}.")
        t.slog(f"[DRY RUN] Would dispatch worker for: {' '.join(dispatch_targets)}")
        return 0

    # ── Budget gate ──────────────────────────────────────────────────────────────
    if budget_exceeded(t, dispatch_targets, targets_json):
        return 0

    # ── Pre-dispatch Slack health gate (per target) ────────────────────────────────
    needs_any = any(tick_dispatch.needs_slack(x, t.dirty_watches) for x in dispatch_targets)
    if needs_any and not slack_health_ok(t):
        kept, gated = tick_dispatch.slack_gate(dispatch_targets, False, t.dirty_watches)
        t.event({"event": "gate_slack_down", "targets": gated, "still_dispatched": kept})
        if not kept:
            t.log(f"SLACK DOWN — every dirty target needs Slack {gated}. Skipping dispatch.")
            t.slog("Run OK — Slack unreachable, dispatch skipped (will retry).")
            return 0
        t.log(f"SLACK DOWN — gating {gated}; still dispatching {kept}.")
        dispatch_targets = kept
        targets_json = kept

    # ── Dispatch ──────────────────────────────────────────────────────────────────
    return dispatch_loop(t, dispatch_targets, targets_json)


def analyze(t):
    """Fold the per-watch rows into dirty/clean/skipped sets + the dirty/clean watch records,
    emitting the same run-log events (gate_watch_backlog, gate_watch_misconfigured) as the shell.
    Mirrors the original shell orchestrator's analyze while-loop."""
    for r in t.results:
        status, qid = r["status"], r["qid"]
        if status == "ok":
            if r.get("complete") is not False:
                t.clean_watches.append({"quest_id": qid, "watch_id": r["watch_id"],
                                        "type": r["type"], "advance_to": r.get("advance_to")})
        elif status == "dirty":
            if qid not in t.dirty_quests:
                t.dirty_quests.append(qid)
            t.dirty_watches.append({"quest_id": qid, "watch_id": r["watch_id"], "type": r["type"],
                                    "checker_outcome": "dirty", "advance_to": r.get("advance_to"),
                                    "complete": r.get("complete") is not False})
            t.log(f"DIRTY: {qid} — {r.get('reason','')}")
        elif status == "hold":
            if qid not in t.skipped_quests:
                t.skipped_quests.append(qid)
            t.log(f"HOLD: {qid} — {r.get('reason','')}")
            _bump_unacked(t, f"{qid}|{r.get('watch_id')}", r.get("type", ""),
                          "held_incomplete_window")
            t.event({"event": "gate_watch_backlog", "quest": qid, "watch_id": r.get("watch_id"),
                     "type": r.get("type"), "reason": "clean but window not drained"})
        elif status == "backoff":
            if qid not in t.skipped_quests:
                t.skipped_quests.append(qid)
            t.log(f"BACKOFF: {qid} — {r.get('reason','')}")
        elif status == "skip":
            if qid not in t.skipped_quests:
                t.skipped_quests.append(qid)
            t.log(f"SKIP: {qid} — {r.get('reason','')}")
            # A genuine Slack rate-limit skip leaves no persisted state (the watermark is just
            # held), so without a run-log event the dashboard cannot tell a quest is throttled.
            # Emit one — the dashboard reads recent ones as a transient "rate limited" flag,
            # the same shape as gate_watch_misconfigured. Only for real ratelimits, not the
            # migration/unreadable skips (which carry no `ratelimited` tag).
            if r.get("ratelimited"):
                t.event({"event": "gate_watch_ratelimited", "quest": qid,
                         "watch_id": r.get("watch_id"), "type": r.get("type"),
                         "reason": r.get("reason", "")})
        elif status == "misconfig":
            if qid not in t.skipped_quests:
                t.skipped_quests.append(qid)
            t.log(f"MISCONFIG: {qid} — {r.get('reason','')} (will not self-heal)")
            t.event({"event": "gate_watch_misconfigured", "quest": qid,
                     "watch_id": r.get("watch_id"), "type": r.get("type"),
                     "reason": r.get("reason", "")})
        elif status == "clean":
            pass
        elif status == "source_recovered":
            pass

    # A quest with both dirty and skipped watches counts as dirty only.
    if t.dirty_quests and t.skipped_quests:
        t.skipped_quests = [q for q in t.skipped_quests if q not in t.dirty_quests]


# _bump_unacked and _record_progress both run in the sequential phase and need no lock;
# cross-process races are already excluded by the tick's own flock.


def _bump_unacked(t, key, wtype, status):
    """Bump the no-progress counter for a watch held OUTSIDE the dispatch path.

    Same ledger, same backoff. `analyze()` calls this for a clean-but-saturated window, which is
    a livelock, not an unacked dispatch: the watermark cannot move, so the same window re-fires
    every tick forever (observed on a github_pr watch: 424 ticks over 14 hours). Writing the
    backoff fields here too means the dashboard's "backing off" badge is telling the truth about
    these records rather than labelling a record that has no retry schedule at all — and it
    makes the livelock decay instead of spinning at tick cadence.
    """
    counts = t._read_json(t.unacked_file, {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    rec = counts.get(key) or {}
    rec["count"] = int(rec.get("count", 0)) + 1
    rec["first_utc"] = rec.get("first_utc") or t.now_utc
    rec["last_utc"] = t.now_utc
    rec["type"] = wtype
    rec["last_status"] = status
    _apply_unacked_backoff(t, rec)
    counts[key] = rec
    try:
        tmp = str(t.unacked_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(counts, f, indent=2)
        os.replace(tmp, t.unacked_file)
    except OSError:
        pass


def housekeep(t, quest_dirs):
    """Retire dead watches (housekeep.py owns the predicate) + prune ledgers. Best-effort."""
    for qd in quest_dirs:
        watch = qd / "watch.json"
        meta = qd / "meta.json"
        if not watch.exists():
            continue
        cp = t.run(t.py(t.helper("ledger", "housekeep.py"), "retire", str(watch),
                        str(meta) if meta.exists() else "/nonexistent", str(t.approvals_file)))
        for line in (cp.stdout or "").splitlines():
            if line.strip():
                t.log(f"{line} ({qd.name})")
    t.run(t.py(t.helper("ledger", "ack-watch.py"), "prune",
               t.env.get("YAAS_MANIFEST_RETAIN_DAYS", "7")))
    t.run(t.py(t.helper("ledger", "checker-health.py"), "prune",
               t.env.get("YAAS_CHECKER_HEALTH_RETAIN_DAYS", "30")))
    _prune_reaction_state(t)
    _prune_worker_logs(t)


def _prune_reaction_state(t):
    """Cap each reaction state file to its newest 1000 timestamps. Mirrors the original shell orchestrator: without
    this the replied/saved arrays grow unbounded and every reaction sweep pays to read them."""
    for name in ("claude_intensifies_replied.json", "writing_hand_replied.json",
                 "floppy_disk_saved.json", "incoming_envelope_adopted.json"):
        p = t.repo_root / "state" / name
        data = t._read_json(p, None)
        if not isinstance(data, dict) or not data:
            continue
        key = next(iter(data))  # replied_timestamps or saved_timestamps
        arr = data.get(key)
        if isinstance(arr, list) and len(arr) > 1000:
            data[key] = sorted(arr)[-1000:]
            try:
                tmp = str(p) + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, p)
                t.log(f"Pruned {p} to 1000 entries (was {len(arr)})")
            except OSError:
                pass


def _prune_worker_logs(t):
    """Delete per-dispatch worker-*.{log,ndjson} older than YAAS_LOG_RETAIN_DAYS (default 14;
    0 disables). Mirrors the original shell orchestrator; rotate-logs.py handles triage.log, not these."""
    try:
        days = int(t.env.get("YAAS_LOG_RETAIN_DAYS", "14") or "14")
    except ValueError:
        days = 14
    if days <= 0:
        return
    cutoff = t.now_ts - days * 86400
    pruned = 0
    for pat in ("worker-*.log", "worker-*.ndjson"):
        for f in t.log_dir.glob(pat):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    pruned += 1
            except OSError:
                pass
    if pruned:
        t.log(f"Pruned {pruned} worker log file(s) older than {days}d")


def slack_health_ok(t):
    cp = t.run([t.mcp_call, "slack_search_public_and_private",
                '{"query":"yaas-health-ping","limit":1}'])
    return cp.returncode == 0


def budget_exceeded(t, dispatch_targets, targets_json):
    cp = t.run(t.py(t.helper("dispatch", "spend-window.py"), str(t.run_log),
                    "--cap-1h", t.env.get("YAAS_MAX_SPEND_1H", "40"),
                    "--cap-24h", t.env.get("YAAS_MAX_SPEND_24H", "250"),
                    "--cap-dispatch-6h", t.env.get("YAAS_MAX_DISPATCH_6H", "250")))
    if cp.returncode != 0 or not cp.stdout.strip():
        return False  # fails OPEN
    try:
        b = json.loads(cp.stdout)
    except ValueError:
        return False
    breach = b.get("breach") or ""
    if breach:
        t.event({"event": "gate_budget_exceeded", "reason": breach, "budget": b,
                 "targets": targets_json})
        t.log(f"BUDGET EXCEEDED — {breach}. Dispatch withheld for {dispatch_targets}.")
        t.slog(f"Run OK — budget cap hit ({breach}). Dispatch withheld.")
        return True
    return False


_RUN_DISCIPLINE = ("watch.json is not editable: append with yaas-triage/ledger/add-watch.py per "
                   "§ 3a. ACT SILENTLY. OUTPUT CONTRACT: emit the summary ONLY if something "
                   "material happened; else exit with no text.")


def dispatch_loop(t, dispatch_targets, targets_json):
    """The per-target dispatch: emit the plan, rotate for fairness, then for each target apply
    the fanout/budget defer and per-target breaker, dispatch one worker, and commit. Mirrors
    the original shell orchestrator's dispatch section; returns the worst non-zero exit as the tick's code."""
    # Match the original shell orchestrator's `cd "$REPO_ROOT"` before dispatching, so the worker subprocess (and any
    # helper it shells out to) runs with the repo as cwd — some workers/helpers resolve state
    # paths relative to it. Everything tick.py itself touches is an absolute path, so this is
    # safe; it only fixes the child processes' working directory.
    os.chdir(t.repo_root)
    dirty_watches_json = t.dirty_watches
    t.log(f"DISPATCH — {len(dispatch_targets)} target(s) (backend={t.agent}): {dispatch_targets}")
    t.event({"event": "gate_dispatch", "targets": targets_json,
             "dirty_watches": dirty_watches_json})
    t.slog(f"Run OK — {len(dispatch_targets)} dirty target(s): {dispatch_targets}. Dispatching...")

    # Fairness rotation (plan.py rotate; stable input order already guaranteed by the sort).
    cursor = 0
    st = t._read_json(t.triage_state, {}) or {}
    if isinstance(st, dict):
        try:
            cursor = int(st.get("dispatch_cursor", 0))
        except (TypeError, ValueError):
            cursor = 0
    cp = t.run(t.py(t.helper("dispatch", "plan.py"), "rotate",
                    json.dumps(dispatch_targets), str(cursor)))
    rotated, offset = dispatch_targets, 0
    try:
        rot = json.loads(cp.stdout)
        if rot.get("order"):
            rotated = rot["order"]
        offset = int(rot.get("offset", 0))
    except (ValueError, AttributeError):
        pass
    if offset:
        t.log(f"Rotated dispatch order by {offset} for fairness: {rotated}")

    # Reactions jump the queue, AFTER the rotation so the cursor cannot push them back.
    # A reaction is the one target with a human watching: you add the emoji and wait for the
    # bot to acknowledge it. Everything else is background work nobody is staring at. Queued
    # last (the previous behaviour) a reaction waited for every dirty quest to finish first —
    # measured 2026-08-08, a trigger sat 4.5 minutes behind three quest dispatches before its
    # worker even started, so the emoji showed nothing for minutes and looked broken.
    # Excluded from the rotation rather than merely sorted first: the rotation exists to stop
    # a quest starving, and there is only ever one reactions target, so rotating it buys
    # nothing and would just reintroduce the delay on some ticks.
    if "reactions" in rotated:
        # index/slice rather than a filter comprehension: a filter would COLLAPSE two
        # entries into one, and if a quest folder were ever literally named "reactions"
        # that would silently drop a real target. Moving the first occurrence preserves
        # the list exactly. (dispatch_one already treats the name as the reaction fast
        # path, so such a folder is unsupported anyway — but unsupported should not mean
        # a vanished dispatch.)
        _i = rotated.index("reactions")
        rotated = [rotated[_i]] + rotated[:_i] + rotated[_i + 1:]

    tick_spent = 0
    worst_exit = 0
    quest_dispatched = 0   # fan-out counts QUESTS; see the reactions exemption below
    for target in rotated:
        remaining = t.tick_budget - tick_spent
        # Reactions does not consume a quest's fan-out slot. Without this exemption, putting
        # reactions first turns the priority into starvation: with YAAS_MAX_DISPATCH_FANOUT=1
        # and a reaction pending across ticks (a slow, failing or partially-acking reaction
        # worker), reactions takes the only slot every tick and every dirty quest is deferred
        # indefinitely — trading one starvation bug for a worse one. There is only ever a
        # single reactions target, so exempting it costs at most one extra invocation per
        # tick. The TIME budget below still applies to it, so it cannot overrun the tick.
        over_fanout = target != "reactions" and quest_dispatched >= t.max_fanout
        if over_fanout or remaining < t.min_slice:
            t.log(f"DEFERRED: {target} (dispatched={t.dispatched} spent={tick_spent}s)")
            t.event({"event": "gate_dispatch_deferred", "target": target,
                     "dispatched": t.dispatched, "spent_sec": tick_spent})
            continue
        # per-target hourly breaker
        cpb = t.run(t.py(t.helper("dispatch", "spend-window.py"), str(t.run_log),
                         "--target", target))
        recent = 0
        try:
            recent = int(json.loads(cpb.stdout).get("target_dispatches_1h", 0))
        except (ValueError, AttributeError, TypeError):
            recent = 0
        cap = t.env.get("YAAS_MAX_TARGET_DISPATCH_PER_HOUR", "25")
        cpo = t.run(t.py(t.helper("dispatch", "plan.py"), "breaker-open", str(recent), str(cap)))
        if cpo.returncode == 0:
            t.log(f"TARGET BREAKER OPEN: {target} dispatched {recent} time(s) in the last hour; skipping.")
            t.event({"event": "gate_target_breaker_open", "target": target,
                     "dispatches_1h": recent})
            continue

        timeout = min(t.worker_timeout, remaining)
        dispatch_one(t, target, timeout, dirty_watches_json)
        t.dispatched += 1
        if target != "reactions":
            quest_dispatched += 1
        tick_spent += t.dispatch_wall
        if t.dispatch_exit != 0:
            worst_exit = t.dispatch_exit
        if target == "reactions":
            commit_reactions(t)
        else:
            commit_quest(t, target, dirty_watches_json)

    # persist rotation cursor
    if isinstance(st, dict) and t.triage_state.exists():
        st["dispatch_cursor"] = offset + t.dispatched
        try:
            tmp = str(t.triage_state) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f, indent=2)
            os.replace(tmp, t.triage_state)
        except OSError:
            t.log("CURSOR WRITE FAILED — dispatch rotation not advanced")

    if t.dispatched > 0:
        t._bump_state(**{"runs_dispatched+": 1, "last_dispatch_utc": _now_utc()})
    t.log(f"DISPATCH DONE — {t.dispatched} invocation(s), {tick_spent}s total, "
          f"last non-zero exit {worst_exit}")
    return worst_exit


def dispatch_one(t, target, timeout, dirty_watches_json):
    """One agent invocation for one target. Sets t.dispatch_* for the commit step."""
    t.dispatch_exit = 1
    t.dispatch_wall = 0
    t.dispatch_slack_read_ok = 0
    t.dispatch_start_utc = _now_utc()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    t.dispatch_run_id = f"run-{stamp}-{os.getpid()}-{t.dispatched}"

    if target == "reactions":
        kind = "reactions"
        pend = t._read_json(t.pending_reactions, {}) or {}
        items = [{"item_id": f"{emoji}:{ts}", "type": emoji}
                 for emoji, tss in pend.items() for ts in tss]
    else:
        kind = "quest"
        items = [{"item_id": w["watch_id"], "type": w["type"]}
                 for w in dirty_watches_json if w["quest_id"] == target]
    if not items:
        t.log(f"DISPATCH SKIPPED: {target} — no dispatchable items in manifest")
        t.dispatch_exit = 8
        return
    # Compact separators to match the original shell orchestrator's `jq -c` output — the manifest JSON is embedded in
    # the worker prompt verbatim, and consumers (and the worker's own eyes) expect the compact
    # {"item_id":"..."} form, not json.dumps's spaced default.
    items_json = json.dumps(items, separators=(",", ":"))
    cp = t.run(t.py(t.helper("ledger", "ack-watch.py"), "open", t.dispatch_run_id,
                    target, kind, items_json))
    if cp.returncode != 0:
        t.log(f"ACK MANIFEST FAILED: {target} — dispatch skipped, watermarks held")
        t.event({"event": "gate_ack_manifest_failed", "target": target})
        t.dispatch_exit = 8
        return

    if target != "reactions":
        t.run(t.py(t.helper("ledger", "watch-guard.py"), "snapshot", target))

    ack_block = (f"ACK LEDGER (REQUIRED): this dispatch has run_id {t.dispatch_run_id}. Before "
                 f"you exit, close EVERY item listed above with exactly one call each: python3 "
                 f"yaas-triage/ledger/ack-watch.py ack {t.dispatch_run_id} <item_id> "
                 f"handled|nothing_to_do|blocked \"<one-line note>\".")
    if target == "reactions":
        prompt = (f"Yaas worker dispatch: dirty target: reactions. Ack items (JSON): {items_json}"
                  f" — each item_id is \"<emoji>:<msg_ts>\". Load and follow "
                  f"yaas-triage/skills/yaas-reactions/SKILL.md (the Reactions Fast Path). "
                  f"{ack_block} {_RUN_DISCIPLINE}")
    else:
        prompt = (f"Yaas worker dispatch: dirty target: {target}. Exact dirty watches (JSON): "
                  f"{items_json} — each item_id is a watch_id. Process EVERY listed watch_id. "
                  f"Load and follow yaas-triage/skills/yaas-quest-dispatch/SKILL.md. "
                  f"{ack_block} {_RUN_DISCIPLINE}")

    cp = t.run(t.py(t.helper("dispatch", "run-agent.py"), "--prompt", prompt,
                    "--label", target, "--timeout", str(timeout),
                    "--header", f"Target: {target}", "--header", f"Run ID: {t.dispatch_run_id}",
                    "--header", f"Ack manifest items: {items_json}"))
    agent_json = {}
    try:
        agent_json = json.loads(cp.stdout)
    except ValueError:
        pass
    try:
        t.dispatch_exit = int(agent_json.get("exit", 1))
    except (TypeError, ValueError):
        t.dispatch_exit = 1
    try:
        t.dispatch_wall = int(agent_json.get("wall_sec", 0))
    except (TypeError, ValueError):
        t.dispatch_wall = 0
    worker_ndjson = agent_json.get("ndjson", "") or ""
    worker_log = agent_json.get("log", "")
    t.log(f"Worker [{target}] exited with {t.dispatch_exit} in {t.dispatch_wall}s (readable: {worker_log})")

    t.dispatch_last_error = ""
    if t.dispatch_exit != 0 and worker_ndjson and os.path.exists(worker_ndjson):
        t.dispatch_last_error = _worker_error_text(worker_ndjson)
        if t.dispatch_last_error:
            t.log(f"WORKER ERROR [{target}] — {t.dispatch_last_error}")
            t.event({"event": "gate_dispatch_error", "targets": [target],
                     "exit_code": t.dispatch_exit, "error": t.dispatch_last_error})

    # Slack infra-failure guard (Claude backend: read init .mcp_servers status).
    if t.dispatch_exit == 0 and worker_ndjson and os.path.exists(worker_ndjson):
        status = _slack_init_status(worker_ndjson)
        if status in ("failed", "needs-auth") and tick_dispatch.needs_slack(target, dirty_watches_json):
            t.log(f"INFRA FAILURE [{target}] — Slack MCP status='{status}'. Forcing exit 9.")
            t.dispatch_exit = 9

    if t.dispatch_exit == 0:
        cpe = t.run(t.py(t.helper("dispatch", "slack-read-health.py"), worker_ndjson))
        if cpe.returncode == 0:
            t.dispatch_slack_read_ok = 1
            t.log(f"WORKER SLACK READ OK [{target}]: successful Slack read observed")

    # Token accounting — emits gate_dispatch_tokens (the event snapshot reads for `dispatches`).
    if t.agent == "claude":
        t.run(t.py(t.helper("dispatch", "extract-tokens.py"), worker_ndjson, str(t.dispatch_exit),
                   str(t.dispatch_wall), target, str(t.run_log), str(t.log_file), str(worker_log)))
    else:
        cpt = t.run(t.py(t.helper("dispatch", "translate-stream.py"), t.agent, worker_ndjson,
                         str(t.dispatch_exit)))
        try:
            tok = json.loads(cpt.stdout)
            t.event({"event": "gate_dispatch_tokens", "backend": t.agent,
                     "input_tokens": tok.get("input_tokens", 0),
                     "output_tokens": tok.get("output_tokens", 0),
                     "wall_sec": t.dispatch_wall, "targets": target,
                     "note": "raw tokens; no cost (non-claude backend)"})
        except (ValueError, AttributeError):
            pass


def _worker_error_text(ndjson_path):
    """The worker's own failure message from its final result record, or "".

    Not parsed or classified — whatever the backend put in `result` is what the dashboard
    shows. The point is that a human reading "backing off" can see "Can't reach the API server
    (ENOTFOUND)" next to it and know it is the network, without opening a log file.
    """
    try:
        last = None
        for line in Path(ndjson_path).read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("type") == "result":
                last = rec
        if not last or not last.get("is_error"):
            return ""
        txt = str(last.get("result") or last.get("terminal_reason") or "").strip()
        return " ".join(txt.split())
    except OSError:
        return ""


def _slack_init_status(ndjson_path):
    try:
        for line in Path(ndjson_path).read_text().splitlines():
            rec = json.loads(line)
            if rec.get("type") == "system" and rec.get("subtype") == "init":
                for srv in rec.get("mcp_servers", []) or []:
                    if srv.get("name") == "slack":
                        return srv.get("status")
    except Exception:
        pass
    return None


# No-progress backoff: 5 minutes doubling to a 24h cap, and it never stops retrying.
#
# Applied only once the count reaches YAAS_UNACKED_PROMOTE, so the first few retries stay at
# tick cadence and a one-tick blip costs nothing. From there: 5m, 10m, 20m, 40m, 80m, 160m,
# 5.3h, 10.6h, 21.3h, then 24h forever. A watch failing for a real reason therefore settles at
# about one dispatch a day — cheap enough to leave running indefinitely, frequent enough that
# a fixed credential or a restored network heals it on its own within a day, with no card to
# clear. Deliberately the same shape as checker-health's backoff, one tier slower, because the
# thing being retried here is a paid dispatch rather than a local checker process.
UNACKED_BASE_BACKOFF = 300
UNACKED_MAX_BACKOFF  = 86400



def unacked_backoff_for(n, promote):
    """Seconds to wait before the next dispatch of a watch with `n` no-progress dispatches.
    0 below the promote threshold (retry at normal tick cadence). PURE."""
    if n < promote:
        return 0
    return min(UNACKED_BASE_BACKOFF * (2 ** (n - promote)), UNACKED_MAX_BACKOFF)


def _apply_unacked_backoff(t, rec):
    wait = unacked_backoff_for(rec["count"], t.unacked_promote)
    rec["next_retry_ts"] = f"{t.now_ts + wait:.6f}" if wait else "0"
    rec["backoff_sec"] = wait


def _record_progress(t, scope, committed_ids):
    """Bump the no-progress counter for every manifest item NOT in committed_ids; clear it for
    those that were. Mirrors the original shell orchestrator _record_progress."""
    manifest_path = t.manifest_dir / f"dispatch-{t.dispatch_run_id}.json"
    counts = t._read_json(t.unacked_file, {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    manifest = t._read_json(manifest_path, None)

    def bump(key, itype="", status=""):
        rec = counts.get(key) or {}
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["first_utc"] = rec.get("first_utc") or t.now_utc
        rec["last_utc"] = t.now_utc
        if itype:
            rec["type"] = itype
        if status:
            rec["last_status"] = status
        _apply_unacked_backoff(t, rec)
        # Why the dispatch made no progress, in the worker's own words where we have them.
        # Without this the dashboard can say a watch is backing off but not what is wrong,
        # which is the whole reason someone opens the dashboard.
        note = (t.dispatch_last_error or "").strip()
        if note:
            rec["last_error"] = note[:300]
        counts[key] = rec

    if manifest is None:
        bump(f"{scope}|<unreadable-manifest>")
    else:
        committed = set(committed_ids or [])
        for item in manifest.get("items", []):
            iid = item.get("item_id", "")
            key = f"{scope}|{iid}"
            if iid in committed:
                counts.pop(key, None)
            else:
                bump(key, item.get("type", ""), item.get("status", "pending"))
    try:
        tmp = str(t.unacked_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(counts, f, indent=2)
        os.replace(tmp, t.unacked_file)
    except OSError:
        pass


import re as _re

# The slack-tooling-outage recovery matcher, mirroring the original shell orchestrator's jq in mark_recovered_if_blocked.
_SLACK_TOOL = _re.compile(r"slack[_ *-]+(mcp|tools?)")
_OUTAGE = _re.compile(r"unavailable|outage|not (exposed|registered|authenticated|connected)|"
                      r"absent|no[ -]such[ -]tool|protocol|malformed|failed to connect|"
                      r"needs authentication")


def quest_has_recovery_evidence(t, qid, source):
    """True iff this tick saw all of the quest's `source` watches read cleanly (a source_recovered
    row) AND nothing unsafe (skip/error/misconfig) for the quest — the same awk over the results
    that the original shell orchestrator runs. Guards against clearing a blocker on a half-healthy tick."""
    recovered = any(r["qid"] == qid and r["status"] == "source_recovered" and r.get("type") == source
                    for r in t.results)
    unsafe = any(r["qid"] == qid and r["status"] in ("skip", "error", "misconfig")
                 for r in t.results)
    return recovered and not unsafe


def mark_recovered_if_blocked(t, qid, source, note, run_start_utc):
    """If the quest's last timeline event is a `blocked` from BEFORE this dispatch, and it is a
    Slack-tooling outage that the worker's successful Slack read now clears, append a recovery
    `note` so the dashboard blocker lifts. Faithful port of the original shell orchestrator; fails closed on anything
    ambiguous (missing/malformed ts, non-slack source, business-dependency blockers)."""
    timeline = t.quests_dir / qid / "timeline.ndjson"
    if not timeline.exists():
        return
    try:
        last = [ln for ln in timeline.read_text().splitlines() if ln.strip()][-1]
        rec = json.loads(last)
    except (IndexError, ValueError, OSError):
        return
    if rec.get("event") != "blocked":
        return
    # Never let this dispatch's own evidence clear a blocker created during the same dispatch.
    try:
        bt = dt_fromiso(rec.get("ts", ""))
        rs = dt_fromiso(run_start_utc)
        if not (bt and rs and bt < rs):
            return
    except Exception:
        return
    if source != "slack":
        return
    kind = rec.get("blocker_kind", "")
    if kind == "slack_tooling_outage":
        recoverable = True
    else:
        text = " ".join(str(rec.get(k, "")) for k in ("reason", "note")).lower()
        recoverable = bool(_SLACK_TOOL.search(text) and _OUTAGE.search(text))
    if not recoverable:
        return
    try:
        with open(timeline, "a") as f:
            f.write(json.dumps({"ts": t.now_utc, "event": "note", "note": note,
                                "recovered_from": "blocked", "recovered_source": source},
                               separators=(",", ":")) + "\n")
        t.log(f"RECOVERED: {qid} — {note}")
    except OSError:
        t.log(f"RECOVERY WRITE FAILED: {qid} — stale blocker left unchanged")


def dt_fromiso(s):
    try:
        return __import__("datetime").datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def commit_quest(t, qid, dirty_watches_json):
    watch = t.quests_dir / qid / "watch.json"
    if not watch.exists():
        return
    if t.dispatch_exit not in (0, 124):
        t.log(f"WORKER FAILURE [{qid}] — exit {t.dispatch_exit}; watermarks left intact.")
        t.event({"event": "gate_dispatch_failure", "exit_code": t.dispatch_exit, "targets": [qid]})
        _record_progress(t, qid, [])
        return

    # Watch-guard: revert any illegal rewrite of a pre-existing entry (appends are kept).
    cpg = t.run(t.py(t.helper("ledger", "watch-guard.py"), "verify", qid))
    if cpg.returncode != 0:
        detail = (cpg.stdout or "").strip()
        t.log(f"WATCH GUARD [{qid}] — worker modified existing entries; repaired: {detail}")
        try:
            t.event({"event": "gate_watch_tampered", "quest": qid, "detail": json.loads(detail)})
        except ValueError:
            pass
    t.run(t.py(t.helper("ledger", "watch-guard.py"), "clear", qid))

    cpa = t.run(t.py(t.helper("ledger", "ack-watch.py"), "acked", t.dispatch_run_id))
    if cpa.returncode != 0:
        t.log(f"ACK MANIFEST UNREADABLE [{qid}] — no watermark advanced.")
        t.event({"event": "gate_ack_manifest_unreadable", "quest": qid,
                 "run_id": t.dispatch_run_id})
        _record_progress(t, qid, [])
        return
    acked = [x for x in (cpa.stdout or "").splitlines() if x.strip()]

    decision_in = {"quest_id": qid, "acked": acked, "dirty_watches": dirty_watches_json}
    cpd = t.run(t.py(t.helper("ledger", "commit.py"), json.dumps(decision_in)))
    try:
        decision = json.loads(cpd.stdout)
    except ValueError:
        decision = {}

    cpsum = t.run(t.py(t.helper("ledger", "ack-watch.py"), "summary", t.dispatch_run_id))
    t.log(f"ACK SUMMARY [{qid}] {(cpsum.stdout or '').strip()}")

    if not (decision.get("acked") or []):
        t.log(f"NO ACKS [{qid}] — worker exited 0 without a committable item; every watermark held.")
        t.event({"event": "gate_dispatch_unacked", "quest": qid, "run_id": t.dispatch_run_id})
        _record_progress(t, qid, [])
        return

    moves = decision.get("moves") or []
    if not advance_watches(t, qid, moves):
        _record_progress(t, qid, [])
        return
    committed_ids = decision.get("committed_ids") or []
    truncated = decision.get("truncated", 0) or 0
    if truncated > 0:
        t.log(f"BACKLOG [{qid}] — {truncated} acked watch(es) had a saturated window; cursor held.")
        t.event({"event": "gate_watch_backlog", "quest": qid, "watches": truncated})
    t.log(f"Advanced {len(moves)} acked watch watermark(s) for dirty quest {qid} (post-worker-success)")
    t.event({"event": "gate_dispatch_success", "targets": [qid], "acked": len(moves)})

    # If the worker read Slack cleanly on a quest that was blocked by a Slack-tooling outage,
    # clear the dashboard blocker with a recovery note (matches the original shell orchestrator).
    if t.dispatch_slack_read_ok == 1 and quest_has_recovery_evidence(t, qid, "slack"):
        mark_recovered_if_blocked(
            t, qid, "slack",
            "Every Slack watch was readable and the worker completed a successful Slack read "
            "after the previous tooling outage.", t.dispatch_start_utc)

    _record_progress(t, qid, committed_ids)


def commit_reactions(t):
    if t.dispatch_exit not in (0, 124):
        t.log(f"WORKER FAILURE [reactions] — exit {t.dispatch_exit}; pending_reactions.json left intact.")
        t.event({"event": "gate_dispatch_failure", "exit_code": t.dispatch_exit,
                 "targets": ["reactions"]})
        return
    if not t.pending_reactions.exists():
        return
    cpsum = t.run(t.py(t.helper("ledger", "ack-watch.py"), "summary", t.dispatch_run_id))
    t.log(f"ACK SUMMARY [reactions] {(cpsum.stdout or '').strip()}")

    manifest = t._read_json(t.manifest_dir / f"dispatch-{t.dispatch_run_id}.json", None)
    pending = t._read_json(t.pending_reactions, None)
    if manifest is None or pending is None:
        return
    counts = t._read_json(t.unacked_file, {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    promote = t.unacked_promote
    done = {i.get("item_id") for i in manifest.get("items", [])
            if i.get("status") in ("handled", "nothing_to_do")}
    state_files = {t.reaction_emojis["process"]: "claude_intensifies_replied.json",
                   t.reaction_emojis["draft"]: "writing_hand_replied.json",
                   t.reaction_emojis["save"]: "floppy_disk_saved.json",
                   t.reaction_emojis["adopt"]: "incoming_envelope_adopted.json"}
    state_dir = t.repo_root / "state"
    parked, remaining = [], {}
    status_by_iid = {i.get("item_id"): i.get("status", "pending")
                     for i in manifest.get("items", [])}
    for emoji, ts_list in pending.items():
        keep = []
        for ts in ts_list:
            iid = f"{emoji}:{ts}"
            key = f"reactions|{iid}"
            if iid in done:
                counts.pop(key, None)
                continue
            rec = counts.get(key) or {}
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["first_utc"] = rec.get("first_utc") or t.now_utc
            rec["last_utc"] = t.now_utc
            rec["type"] = emoji
            counts[key] = rec
            if rec["count"] >= promote and emoji in state_files:
                sp = state_dir / state_files[emoji]
                sdata = t._read_json(sp, {}) or {}
                notes = sdata.setdefault("skipped_notes", {})
                notes[ts] = (f"parked by triage after {rec['count']} dispatch(es) with no "
                             f"progress (last status: {rec.get('last_status','pending')}) — needs review")
                try:
                    stmp = str(sp) + ".tmp"
                    with open(stmp, "w") as f:
                        json.dump(sdata, f, indent=2)
                    os.replace(stmp, sp)
                    counts.pop(key, None)
                    parked.append(iid)
                    continue
                except OSError:
                    pass
            rec["last_status"] = status_by_iid.get(iid, "pending")
            keep.append(ts)
        if keep:
            remaining[emoji] = keep
    try:
        ctmp = str(t.unacked_file) + ".tmp"
        with open(ctmp, "w") as f:
            json.dump(counts, f, indent=2)
        os.replace(ctmp, t.unacked_file)
    except OSError:
        pass
    if parked:
        t.log("reactions: parked " + ", ".join(parked) + " into skipped_notes (no progress)")
    if remaining:
        try:
            tmp = str(t.pending_reactions) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(remaining, f, indent=2)
            os.replace(tmp, t.pending_reactions)
        except OSError:
            pass
        t.log("REACTIONS PARTIAL — unacked/blocked reactions retained in pending_reactions.json.")
        t.event({"event": "gate_reactions_partial", "run_id": t.dispatch_run_id})
    else:
        try:
            os.unlink(t.pending_reactions)
        except OSError:
            pass
        t.log("Cleared pending_reactions.json (every reaction progressed)")
        t.event({"event": "gate_dispatch_success", "targets": ["reactions"]})


def main():
    # ── Config + knob validation (Config raises BadEnvKnob → exit 2 like the shell) ──
    try:
        t = Tick()
    except tick_state.BadEnvKnob as e:
        # Best-effort run-log; repo paths are derivable without the knobs.
        try:
            root = tick_state._repo_root(_HERE)
            with open(root / "state" / "run-log.ndjson", "a") as f:
                f.write(json.dumps({"ts": _now_utc(), "event": "gate_bad_env_knob",
                                    "bad": str(e)}) + "\n")
        except Exception:
            pass
        sys.stderr.write(f"BAD ENV KNOB — {e} must be numeric. Refusing to run.\n")
        return 2

    os.makedirs(t.log_dir, exist_ok=True)
    os.makedirs(t.quests_dir, exist_ok=True)
    os.makedirs(os.path.dirname(t.triage_state), exist_ok=True)

    # ── Single-instance lock ──────────────────────────────────────────────────
    # The loop fires regardless of whether the previous tick finished, and a worker
    # dispatch can take minutes, so two ticks could race watch.json / the run log. Take
    # an exclusive non-blocking flock; if another tick holds it, skip this one (exit 0).
    # The OS releases the lock when this process exits, so no cleanup is needed. Mirrors
    # the original shell orchestrator's flock — NOT covered by the differential harness (it runs a single tick),
    # so it lives here in the sequencer, not in a golden.
    import fcntl
    lockfile = t.log_dir / "triage.lock"
    holderfile = t.log_dir / "triage.lock.holder"
    lock_fd = open(lockfile, "a")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = "unknown"
        try:
            holder = holderfile.read_text().strip() or "unknown"
        except OSError:
            pass
        t.log(f"SKIP — previous triage still running (holder pid: {holder}). Will retry next tick.")
        t.event({"event": "gate_skip_locked", "holder_pid": holder})
        return 0
    try:
        holderfile.write_text(str(os.getpid()))
    except OSError:
        pass

    # _on_exit in a finally so log rotation / notify / v2-sync (and the completion stamp) run on
    # ANY exit, including an unhandled exception mid-tick — matching the original shell orchestrator's `trap _on_exit
    # EXIT`. Only a SIGKILL/hang skips it, which is exactly the "started but never completed"
    # state health-monitor is built to catch.
    try:
        return run_tick(t)
    finally:
        _on_exit(t)


def _on_exit(t):
    """Post-run hook — mirrors the original shell orchestrator _on_exit. Stamps completion, rotates logs, notifies,
    syncs. All best-effort; a failure here never changes the tick's verdict."""
    t._bump_state(last_triage_completed_utc=_now_utc())
    for argv in (t.py(t.helper("ops", "rotate-logs.py")),
                 t.py(t.helper("ops", "notify.py")),
                 ["bash", t.helper("ops", "sync-yaas-v2.sh")]):
        try:
            t.run(argv)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
