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

# doc-contracts.test.sh — documentation tables and skill guidance stay aligned with manifests.

set -u
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$HERE"
while [ "$REPO" != "/" ] && [ ! -d "$REPO/yaas-triage" ]; do REPO="$(dirname "$REPO")"; done
[ -d "$REPO/yaas-triage" ] || { echo "cannot locate repo root above $0" >&2; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

run_python_check() {
  local label="$1"
  local out
  if out="$(python3 - "$REPO" "$label" <<'PY'
import json
import re
import sys
from pathlib import Path

repo = Path(sys.argv[1])
label = sys.argv[2]
triage = repo / "yaas-triage"
checkers = triage / "checkers"

manifest_types = sorted(p.stem.replace(".watch", "") for p in checkers.glob("*.watch.json"))
manifest_set = set(manifest_types)

def fail(msg):
    raise SystemExit(msg)

def read(rel):
    return (repo / rel).read_text()

def table_types(rel, anchor):
    lines = read(rel).splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if anchor in line)
    except StopIteration:
        fail(f"{rel}: missing anchor {anchor!r}")
    rows = []
    started = False
    for line in lines[start + 1:]:
        if line.startswith("|"):
            started = True
            rows.append(line)
            continue
        if started:
            break
    if not rows:
        fail(f"{rel}: expected a watch-type table after {anchor!r}")
    found = {
        match
        for row in rows
        for match in re.findall(r"`([a-z_]+)`", row)
        if match in manifest_set
    }
    return found

def load_lags():
    lags = {}
    for path in sorted(checkers.glob("*.lag")):
        lags[path.stem] = int(path.read_text().strip())
    return lags

def doc_lags(rel):
    matches = re.findall(r"([a-z_]+)\.lag = (\d+)", read(rel))
    return {name: int(value) for name, value in matches}

def creation_bullets(rel):
    found = set()
    approval_line = None
    for line in read(rel).splitlines():
        m = re.match(r"\s*-\s+\*\*`([a-z_]+)`\*\*", line)
        if not m:
            continue
        wtype = m.group(1)
        if wtype in manifest_set:
            found.add(wtype)
            if wtype == "approval":
                approval_line = line
    if approval_line is None:
        fail(f"{rel}: expected an `approval` bullet marked runtime-only")
    return found, approval_line

def require_checker_authoring_contract(rel):
    content = read(rel)
    required = ("<type>.py", "<type>.watch.json")
    missing = [term for term in required if term not in content]
    if missing:
        fail(f"{rel}: checker authoring contract is missing {missing}")
    if "executable" not in content:
        fail(f"{rel}: checker authoring contract must require an executable checker")
    stale = ("Nothing else changes", "nothing else changes", "registration points")
    found = [term for term in stale if term in content]
    if found:
        fail(f"{rel}: checker authoring contract retains obsolete wording {found}")

def require_operator_commands():
    setup = read("yaas-triage/setup/setup.sh")
    if "$TRIAGE_DIR/ops/dashboard-start.sh" not in setup:
        fail("setup.sh: manual dashboard command does not use ops/dashboard-start.sh")
    if "$TRIAGE_DIR/dashboard-start.sh" in setup:
        fail("setup.sh: retains the moved dashboard-start.sh path")

    heartbeat = read("yaas-triage/setup/install-launchd-heartbeat.sh")
    if "./install-launchd-heartbeat.sh" not in heartbeat:
        fail("install-launchd-heartbeat.sh: usage names the wrong installer")
    if "python3 $TRIAGE_DIR/ops/health-monitor.py" not in heartbeat:
        fail("install-launchd-heartbeat.sh: manual run does not invoke health-monitor.py")
    if "python3 $TRIAGE_DIR/tick.py" in heartbeat:
        fail("install-launchd-heartbeat.sh: manual run incorrectly invokes tick.py")

    readme = read("README.md")
    for label in ("com.yaas.triage", "com.yaas.dashboard"):
        if label not in readme:
            fail(f"README.md: restart guidance is missing {label}")

if label == "README watch table":
    found = table_types("README.md", "Things you can watch:")
    if found != manifest_set:
        fail(
            "README.md: expected watch table types "
            f"{sorted(manifest_set)}, got {sorted(found)}"
        )
elif label == "ARCHITECTURE watch table":
    found = table_types(
        "ARCHITECTURE.md",
        "## 6. Watch types",
    )
    if found != manifest_set:
        fail(
            "ARCHITECTURE.md: expected watch table types "
            f"{sorted(manifest_set)}, got {sorted(found)}"
        )
elif label == "yaas-ops lag map":
    expected = load_lags()
    found = doc_lags("yaas-triage/skills/yaas-ops/SKILL.md")
    if found != expected:
        fail(
            "yaas-triage/skills/yaas-ops/SKILL.md: expected documented `<type>.lag = <int>` "
            f"pairs {expected}, got {found}"
        )
elif label == "quest creation offer set":
    rel = "yaas-triage/skills/yaas-quest-creation/SKILL.md"
    found, approval_line = creation_bullets(rel)
    expected_creatable = {
        path.stem.replace(".watch", "")
        for path in checkers.glob("*.watch.json")
        if json.loads(path.read_text())["user_creatable"]
    }
    expected_all = set(expected_creatable) | {"approval"}
    if found != expected_all:
        fail(
            f"{rel}: expected watch-type bullets {sorted(expected_all)} "
            f"(user_creatable plus runtime-only approval), got {sorted(found)}"
        )
    if "runtime-only" not in approval_line or "Do not put one in a creation spec" not in approval_line:
        fail(
            f"{rel}: expected the `approval` bullet to say it is runtime-only and "
            "not for creation specs"
        )
elif label == "checker authoring contract":
    for rel in (
        "README.md",
        "ARCHITECTURE.md",
        "CLAUDE.example.md",
        "yaas-triage/skills/yaas-checker-authoring/SKILL.md",
    ):
        require_checker_authoring_contract(rel)
elif label == "operator command contract":
    require_operator_commands()
else:
    fail(f"unknown check label: {label}")
PY
)" ; then
    ok "$label"
  else
    bad "${out:-$label}"
  fi
}

echo "── docs stay aligned with watch manifests ─────────────────────────────────"
run_python_check "README watch table"
run_python_check "ARCHITECTURE watch table"
run_python_check "yaas-ops lag map"
run_python_check "quest creation offer set"
run_python_check "checker authoring contract"
run_python_check "operator command contract"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "doc contracts: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
