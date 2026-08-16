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

# Invalid watch types come from watch.json, so they must fail closed as
# misconfigurations rather than escape checkers/ or crash the whole tick.

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

python3 - "$SCRIPT_DIR" <<'PY'
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

triage = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("tick", triage / "tick.py")
tick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tick)


class FakeTick:
    def __init__(self, root):
        self.quests_dir = root / "quests"
        self.script_dir = triage
        self.unacked_file = root / "unacked.json"
        self.checker_health_json = {}
        self.now_ts = 100.0
        self.unacked_promote = 3
        self.error_promote = 6

    @staticmethod
    def _read_json(path, default):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default

    def run(self, _args):
        raise AssertionError("invalid watch type executed a checker")


bad_types = ["../../surfaces/slack-send", None, 7, {"checker": "slack_thread"}]
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    quest = root / "quests" / "q"
    quest.mkdir(parents=True)
    watches = [
        {"watch_id": f"watch-{i:016d}", "type": value}
        for i, value in enumerate(bad_types)
    ]
    (quest / "watch.json").write_text(json.dumps({"watches": watches}))
    rows = tick.check_quest(FakeTick(root), "q")

misconfigured = [row for row in rows if row.get("status") == "misconfig"]
assert len(misconfigured) == len(bad_types), rows
assert [row["type"] for row in misconfigured] == bad_types, rows
print(f"watch type validation: {len(misconfigured)} passed, 0 failed")
PY
