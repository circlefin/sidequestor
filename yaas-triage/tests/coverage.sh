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

# coverage.sh — which source files have no unit test?
#
# This is the whole point of mirroring the source tree under tests/unit/. A missing file is
# a VISIBLE gap rather than an assumed one: for every source file there either is a test at
# the matching path, or it appears in the report below.
#
# It deliberately does NOT fail the build. Some files legitimately need no unit test, and a
# check that cannot pass gets ignored. Instead every exemption must carry a reason, because
# a bare allowlist becomes the place gaps go to hide.
#
# Behaviour tests are not counted here. They are named by failure class and span several
# files by design (the approval lease spans three), so no single source file is their home.

set -u
HERE="$(cd "$(dirname "$0")" && pwd -P)"
TRIAGE="$(cd "$HERE/.." && pwd -P)"

# Exempt, with a reason each.
exempt_reason() {
  case "$1" in
    dispatch/worker.mcp.json)   echo "config, not code" ;;
    checkers/*.lag)             echo "config: one integer" ;;
    checkers/*.watch.json)      echo "validated collectively by behaviour/checker-contract + doc-contracts" ;;
    approval_state.py)          echo "covered via behaviour/approval-transitions + approval-lease" ;;
    checkers/github.py)         echo "shared adapter covered via unit/checkers/github_issue + github_pr" ;;
    ops/dashboard-start.sh)     echo "fifteen lines that open a browser" ;;
    triage-loop.sh)             echo "a sleep loop around tick.py; launchd owns it" ;;
    ops/heartbeat-loop.sh)      echo "a sleep loop around health-monitor.py; launchd owns it" ;;
    dispatch/manual-dispatch.sh) echo "covered via unit/dispatch/run-agent (shared pipeline)" ;;
    ops/sync-yaas-v2.sh)        echo "mirrors the public repo; exercised by using it" ;;
    ops/dashboard-server.py)    echo "covered indirectly by behaviour/approval-lease" ;;
    dispatch/dispatch-agent.sh) echo "backend launcher; covered via dispatch/run-agent" ;;
    dispatch/format-stream.py)  echo "covered via dispatch/run-agent" ;;
    dispatch/translate-stream.py) echo "covered via dispatch/run-agent" ;;
    dispatch/extract-tokens.py) echo "covered via behaviour/budget-gate" ;;
    dispatch/slack-read-health.py) echo "covered via unit/dispatch/slack-read-health" ;;
    dispatch/spend-window.py)   echo "covered via behaviour/budget-gate" ;;
    reaction_config.py)         echo "covered via unit/surfaces/react-lifecycle" ;;
    ledger/ack-watch.py)        echo "covered via behaviour/dirty-watch-dispatch + goldens" ;;
    ledger/checker-health.py)   echo "covered via behaviour/dirty-watch-dispatch" ;;
    ledger/approval-helper.py)  echo "covered via behaviour/approval-lease" ;;
    checkers/result.py)         echo "covered via behaviour/checker-contract" ;;
    checkers/cron-due.py)       echo "covered via behaviour/checker-contract" ;;
    checkers/approval.py)       echo "covered via behaviour/approval-lease" ;;
    checkers/reactions.py)      echo "covered via behaviour/dirty-watch-dispatch" ;;
    checkers/email.py|checkers/schedule.py|\
    checkers/slack_thread.py|checkers/slack_channel.py|checkers/slack_dm.py|\
    checkers/slack_mention.py)  echo "contract-checked via behaviour/checker-contract" ;;
    surfaces/mcp-call.sh|surfaces/jira-call.sh|surfaces/slack-react.sh)
                                echo "11-line shim; covered via unit/surfaces/client" ;;
    tick.py)                    echo "the orchestrator; covered end-to-end by the differential goldens + mutations (differential/run.sh check tick.py), which run-all.sh runs" ;;
    *) return 1 ;;
  esac
}

TESTED=0; MISSING=0; EXEMPT=0
echo
printf '\033[1mUnit-test coverage\033[0m\n\n'
for src in $(cd "$TRIAGE" && find . -type f \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.lag' \) \
             ! -path './tests/*' ! -path './setup/*' ! -path './skills/*' ! -path '*__pycache__*' \
             | sed 's|^\./||' | sort); do
  base=$(basename "$src"); base=${base%.*}
  dir=$(dirname "$src"); [ "$dir" = "." ] && dir=""
  want="$HERE/unit/${dir:+$dir/}$base.test.sh"
  if [ -f "$want" ]; then
    TESTED=$((TESTED+1))
  elif reason=$(exempt_reason "$src"); then
    EXEMPT=$((EXEMPT+1))
    printf '  \033[33m—\033[0m %-34s %s\n' "$src" "$reason"
  else
    MISSING=$((MISSING+1))
    printf '  \033[31mNO TEST\033[0m %-30s (expected tests/unit/%s)\n' "$src" "${dir:+$dir/}$base.test.sh"
  fi
done

echo
printf '  %s with a unit test, %s exempt (with reasons), \033[31m%s with none\033[0m\n' \
  "$TESTED" "$EXEMPT" "$MISSING"
echo
echo "  Exempt is not the same as untested: most exempt files are covered by a behaviour"
echo "  suite or the differential goldens. What matters is that nothing is silently absent."
