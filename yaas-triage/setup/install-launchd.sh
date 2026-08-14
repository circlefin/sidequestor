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

# install-launchd.sh — install the yaas triage launchd agent
#
# Installs ~/Library/LaunchAgents/com.yaas.triage.plist pointing at the checked-out
# repo's triage loop (tick.py). After install, it runs every ~60 seconds until uninstalled.
#
# Usage:
#   ./install-launchd.sh            # install
#   ./install-launchd.sh uninstall  # remove
#   ./install-launchd.sh status     # show current status

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TRIAGE_DIR/.." && pwd)"
YAAS_LAUNCHD_TEMPLATE="$SCRIPT_DIR/com.yaas.triage.plist.template"
YAAS_LAUNCHD_LABEL="com.yaas.triage"
YAAS_LAUNCHD_LOADED="fires every 60s"

yaas_launchd_install_guidance() {
  echo
  echo "Logs: $REPO_ROOT/logs/triage.{out,err}.log"
  echo "Tail live: tail -f $REPO_ROOT/logs/triage.out.log"
  echo
  echo "First run will fire within 60s. To run now:"
  echo "  python3 $TRIAGE_DIR/tick.py"
}

. "$SCRIPT_DIR/install-launchd-common.sh"
yaas_install_launchd_job "${1:-install}"
