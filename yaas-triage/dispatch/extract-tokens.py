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
extract-tokens.py — parse the final 'result' event from a worker stream-json ndjson,
write a gate_dispatch_tokens entry to run-log.ndjson, and append a cost summary
line to triage.log and the human worker log.

Usage:
  python3 extract-tokens.py <ndjson_path> <exit_code> <wall_sec> <target_list> \
                             <run_log> <triage_log> <worker_log>

  target_list — comma-separated quest IDs / "reactions"

Prints a one-line cost summary to stderr (also appended to triage.log and worker log).
Always exits 0; missing result event is a soft warning, not a failure.
"""
import sys
import json
from datetime import datetime, timezone


def main():
    ndjson_path, exit_code, wall, targets, run_log, tri_log, human_log = sys.argv[1:8]

    result = None
    with open(ndjson_path) as f:
        for line in f:
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                d = json.loads(s)
            except Exception:
                continue
            if d.get("type") == "result":
                result = d

    if not result:
        print("WARN: no result event found in worker log", file=sys.stderr)
        return

    u = result.get("usage", {}) or {}
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "ts":          now_utc,
        "event":       "gate_dispatch_tokens",
        "targets":     [t for t in targets.split(",") if t],
        "input":       u.get("input_tokens", 0),
        "output":      u.get("output_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read":  u.get("cache_read_input_tokens", 0),
        "cost_usd":    result.get("total_cost_usd", 0.0),
        "wall_sec":    int(wall),
        "exit":        int(exit_code),
    }
    with open(run_log, "a") as f:
        f.write(json.dumps(event) + "\n")

    line = (
        f"Tokens: in={event['input']:,} out={event['output']:,} "
        f"cw={event['cache_write']:,} cr={event['cache_read']:,} "
        f"| ${event['cost_usd']:.4f} | {event['wall_sec']}s"
    )
    with open(tri_log, "a") as f:
        f.write(f"{now_utc}  {line}\n")
    with open(human_log, "a") as f:
        f.write(f"\n=== {line} ===\n")
    print(line, file=sys.stderr)


if __name__ == "__main__":
    main()
