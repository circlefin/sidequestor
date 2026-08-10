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
spend-window.py — rolling dispatch spend and count, read from run-log.ndjson.

Feeds the pre-dispatch budget gate in the original shell orchestrator. No new accounting: every dispatch
already writes a `gate_dispatch_tokens` event carrying `cost_usd`, and every tick
that dispatches writes `gate_dispatch`.

Usage:
  spend-window.py <run_log> [--target <name>]
                            [--cap-1h N] [--cap-6h N] [--cap-24h N]
                            [--cap-dispatch-6h N]

Prints one JSON object:

  {"spend_1h": 2.10, "spend_6h": 4.21, "spend_24h": 19.87,
   "dispatches_1h": 4, "dispatches_6h": 31, "dispatches_24h": 164,
   "uncosted_24h": 12,                    # dispatches with no cost_usd field
   "target_dispatches_1h": 3,             # only with --target
   "breach": "6h spend $81.20 over cap $75"}   # "" when within every cap

The cap comparison lives here rather than in the shell so the floats are compared
in one language, with no dependency on bc.

the original shell orchestrator enforces `--cap-1h`, `--cap-24h` and `--cap-dispatch-6h`. The hourly cap is
the responsive dollar tripwire; the 24h cap catches slow drift; the count cap is the
only one that works under the Codex and Cursor backends, which report no cost figure.
`--cap-6h` is accepted but unused: 6h sits awkwardly between the hourly tripwire and
the daily backstop and adds nothing. `spend_6h` is still reported for observability.

`uncosted_24h` matters: the Codex and Cursor backends report raw tokens with no
cost figure, so a dollars-only ceiling is blind under those. the original shell orchestrator therefore
also enforces a dispatch-COUNT ceiling, which is backend-agnostic.

Reads only the live run log. `rotate-logs.py` keeps 7 days there, which covers the
24h window comfortably, but the monthly archive files are deliberately NOT read:
including them would be pointless for a 24h window, and a truncated live log must
never be mistaken for a cheap window. Exits non-zero if the log cannot be read at
all, and triage treats that as fail-open (a corrupt log must not wedge dispatch
forever) while the heartbeat job is what notices the corruption.
"""

import json
import sys
from datetime import datetime, timedelta, timezone


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def main():
    if len(sys.argv) < 2:
        print("usage: spend-window.py <run_log> [--target <name>]", file=sys.stderr)
        return 2

    path = sys.argv[1]
    def opt(flag, default=None):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return default

    target      = opt("--target")
    cap_1h      = opt("--cap-1h")
    cap_6h      = opt("--cap-6h")
    cap_24h     = opt("--cap-24h")
    cap_disp_6h = opt("--cap-dispatch-6h")

    now = datetime.now(timezone.utc)
    c6, c24, c1 = now - timedelta(hours=6), now - timedelta(hours=24), now - timedelta(hours=1)

    out = {
        "spend_1h": 0.0, "spend_6h": 0.0, "spend_24h": 0.0,
        "dispatches_1h": 0, "dispatches_6h": 0, "dispatches_24h": 0,
        "uncosted_24h": 0,
    }
    if target is not None:
        out["target_dispatches_1h"] = 0

    try:
        fh = open(path)
    except OSError as exc:
        print(f"error:unreadable_run_log:{exc}", file=sys.stderr)
        return 1

    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ev = d.get("event")
            if ev not in ("gate_dispatch_tokens", "gate_dispatch"):
                continue
            t = _parse_ts(d.get("ts"))
            if t is None or t < c24:
                continue

            if ev == "gate_dispatch_tokens":
                cost = d.get("cost_usd")
                if cost is None:
                    out["uncosted_24h"] += 1
                    cost = 0.0
                try:
                    cost = float(cost)
                except (TypeError, ValueError):
                    cost = 0.0
                out["spend_24h"] += cost
                out["dispatches_24h"] += 1
                if t >= c6:
                    out["spend_6h"] += cost
                    out["dispatches_6h"] += 1
                if t >= c1:
                    out["spend_1h"] += cost
                    out["dispatches_1h"] += 1

            if target is not None and t >= c1:
                tg = d.get("targets")
                if isinstance(tg, list) and target in tg and ev == "gate_dispatch":
                    out["target_dispatches_1h"] += 1

    out["spend_1h"] = round(out["spend_1h"], 4)
    out["spend_6h"] = round(out["spend_6h"], 4)
    out["spend_24h"] = round(out["spend_24h"], 4)

    def over(value, cap):
        if cap is None:
            return False
        try:
            return float(value) > float(cap)
        except (TypeError, ValueError):
            return False

    # Most responsive window first, so the breach message names the tightest cap
    # that was actually crossed.
    breach = ""
    if over(out["spend_1h"], cap_1h):
        breach = f"1h spend ${out['spend_1h']:.2f} over cap ${float(cap_1h):.2f}"
    elif over(out["spend_6h"], cap_6h):
        breach = f"6h spend ${out['spend_6h']:.2f} over cap ${float(cap_6h):.2f}"
    elif over(out["spend_24h"], cap_24h):
        breach = f"24h spend ${out['spend_24h']:.2f} over cap ${float(cap_24h):.2f}"
    elif over(out["dispatches_6h"], cap_disp_6h):
        breach = f"6h dispatch count {out['dispatches_6h']} over cap {cap_disp_6h}"
    out["breach"] = breach

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
