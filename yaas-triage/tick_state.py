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
tick_state.py — the config and loading foundation of the tick.py orchestrator.

This is the first module of the real orchestrator rewrite: everything the tick reads before it
decides anything. It reproduces, faithfully, what triage.sh derives in its first ~90 lines plus
its "Per-checker watermark lag map" and "Gather quests" sections — repo root, paths, the numeric
env knobs (with the same refuse-on-garbage validation), the per-type watermark lag map, and the
list of active quests.

It is deliberately small and side-effect-light: it reads files and the environment, and it does
NOT write state, dispatch, or log to the run log. tick.py owns those. Keeping loading pure makes
it unit-testable against a fixture tree, which the shell version never was.

Named tick_state.py, not state.py, because the repo already has a top-level state/ directory and
a second thing called "state" invites confusion; the tick_ prefix marks the orchestrator's own
modules.
"""

import json
import os
from pathlib import Path


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


# The numeric knobs that gate spend and data loss, with their defaults. A malformed value here
# must FAIL the tick loudly, never silently read as "no cap" — the same rule triage.sh enforces,
# because a ceiling that quietly disables itself is worse than no ceiling.
NUMERIC_KNOBS = {
    "YAAS_TICK_DISPATCH_BUDGET": 1800,
    "YAAS_MAX_DISPATCH_FANOUT": 4,
    "YAAS_MAX_TARGET_DISPATCH_PER_HOUR": 25,
    "YAAS_UNACKED_PROMOTE": 3,
    "YAAS_CHECKER_ERROR_PROMOTE": 6,
    "YAAS_RETIRE_DEFAULT_DAYS": 30,
    "YAAS_LOG_RETAIN_DAYS": 14,
    "YAAS_MANIFEST_RETAIN_DAYS": 7,
    "YAAS_CHECKER_HEALTH_RETAIN_DAYS": 30,
    "YAAS_TRIAGE_MAX_PARALLEL": 8,
    "YAAS_MIN_DISPATCH_SLICE": 60,
    "YAAS_STALE_REPLY_HOURS": 24,
    "YAAS_CATCHUP_AFTER_HOURS": 6,
}


class BadEnvKnob(Exception):
    """A gate knob has a non-numeric value. tick.py turns this into gate_bad_env_knob + exit 2."""


def _load_env_file(repo_root, environ):
    """Merge REPO_ROOT/.env into a copy of the environment, without overriding values already
    set in the real environment (which is how `set -a; source .env` behaves after the caller
    has exported its own). Returns a dict; does not mutate os.environ.

    Parsing is intentionally minimal and matches what a shell `source` accepts for the simple
    KEY=VALUE / KEY="VALUE" lines this project uses: it is NOT a general shell parser, and a
    line it cannot parse is skipped rather than executed (the shell would execute it, which is
    exactly the .env-syntax hazard doctor.sh warns about — here we simply do not honour it).
    """
    env = dict(environ)
    envf = Path(repo_root) / ".env"
    if not envf.exists():
        return env
    for raw in envf.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        env.setdefault(key, val)
    return env


def validate_knobs(env):
    """Raise BadEnvKnob if any gate knob (or a YAAS_MAX_SPEND_* window) is set but non-numeric.

    Mirrors triage.sh: `.` and `1.2.3` and `twenty` are all rejected; empty means "use the
    default" and is fine. The dangerous direction is a ceiling silently reading as zero/absent,
    so this refuses to run rather than proceed with a disabled cap.
    """
    def bad(v):
        v = str(v)
        if v == "":
            return False  # empty → default, fine
        if v == "." or v.count(".") > 1:
            return True
        return any(c not in "0123456789." for c in v)

    offenders = []
    for k in NUMERIC_KNOBS:
        if k in env and bad(env[k]):
            offenders.append(f"{k}={env[k]}")
    for k, v in env.items():
        if k.startswith("YAAS_MAX_SPEND_") and bad(v):
            offenders.append(f"{k}={v}")
    if offenders:
        raise BadEnvKnob(" ".join(offenders))


def load_lag_map(triage_dir):
    """{watch_type: lag_seconds} from checkers/*.lag. A non-integer .lag is skipped, matching
    the shell. The lag offsets the 'advance to now' fallback for sources that settle late."""
    lags = {}
    for f in sorted(Path(triage_dir, "checkers").glob("*.lag")):
        raw = "".join(f.read_text().split())
        if raw.isdigit():
            lags[f.stem] = int(raw)
    return lags


def gather_quests(quests_dir):
    """Active quest ids (directories with a watch.json), sorted for a stable, fair dispatch
    order — the same stability the fairness rotation depends on. A dir without watch.json is
    not a quest yet and is skipped."""
    base = Path(quests_dir)
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and (d / "watch.json").exists())


class Config:
    """Everything the tick reads before deciding. A plain data holder; no behaviour."""

    def __init__(self, triage_dir, environ=None):
        environ = os.environ if environ is None else environ
        self.triage_dir = Path(triage_dir).resolve()
        self.repo_root = _repo_root(self.triage_dir)
        self.env = _load_env_file(self.repo_root, environ)
        validate_knobs(self.env)

        r = self.repo_root
        self.quests_dir = r / "state" / "quests" / "active"
        self.triage_state = r / "state" / "triage" / "last-run.json"
        self.run_log = r / "state" / "run-log.ndjson"
        self.log_dir = r / "logs"
        self.log_file = self.log_dir / "triage.log"
        self.manifest_dir = r / "state" / "triage"
        self.pending_reactions = self.manifest_dir / "pending_reactions.json"
        self.mcp_call = self.triage_dir / "surfaces" / "mcp-call.sh"

        self.lag_map = load_lag_map(self.triage_dir)

    def knob(self, name):
        """A validated numeric knob as an int, or its default if unset/empty."""
        v = str(self.env.get(name, "")).strip()
        return int(float(v)) if v else NUMERIC_KNOBS[name]


def main():
    # A tiny CLI so the loader can be inspected and tested from the shell: prints the resolved
    # config as JSON, or the bad-knob reason on exit 2 (the shape tick.py will use).
    import sys
    triage_dir = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
    try:
        c = Config(triage_dir)
    except BadEnvKnob as e:
        print(json.dumps({"bad_env_knob": str(e)}))
        return 2
    print(json.dumps({
        "repo_root": str(c.repo_root),
        "quests": gather_quests(c.quests_dir),
        "lag_map": c.lag_map,
        "run_log": str(c.run_log),
    }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
