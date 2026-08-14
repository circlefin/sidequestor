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

# install-launchd-heartbeat.sh — install the independent yaas heartbeat monitor
#
# Installs ~/Library/LaunchAgents/com.yaas.heartbeat.plist pointing at the checked-out
# repo's health monitor. After install, it runs every 300 seconds until uninstalled.
#
# Usage:
#   ./install-launchd-heartbeat.sh            # install
#   ./install-launchd-heartbeat.sh uninstall  # remove
#   ./install-launchd-heartbeat.sh status     # show current status

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TRIAGE_DIR/.." && pwd)"
YAAS_LAUNCHD_TEMPLATE="$SCRIPT_DIR/com.yaas.heartbeat.plist.template"
YAAS_LAUNCHD_LABEL="com.yaas.heartbeat"
YAAS_LAUNCHD_LOADED="fires every 300s"

yaas_launchd_install_guidance() {
  echo
  echo "Logs: $REPO_ROOT/logs/heartbeat.{out,err}.log"
  echo "Tail live: tail -f $REPO_ROOT/logs/heartbeat.out.log"
  echo
  echo "First check fires immediately, then every 300s. To run now:"
  echo "  python3 $TRIAGE_DIR/ops/health-monitor.py"
}

. "$SCRIPT_DIR/install-launchd-common.sh"
yaas_install_launchd_job "${1:-install}"
