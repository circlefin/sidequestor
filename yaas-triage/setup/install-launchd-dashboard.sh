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

# install-launchd-dashboard.sh — install the yaas dashboard launchd agent
#
# Installs ~/Library/LaunchAgents/com.yaas.dashboard.plist pointing at the
# checked-out repo's dashboard-server.py. After install, the dashboard server
# runs continuously on port 8877 (KeepAlive — launchd restarts it if it ever
# dies) and survives reboots (RunAtLoad).
#
# Usage:
#   ./install-launchd-dashboard.sh            # install
#   ./install-launchd-dashboard.sh uninstall  # remove
#   ./install-launchd-dashboard.sh status     # show current status

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TRIAGE_DIR/.." && pwd)"
YAAS_LAUNCHD_TEMPLATE="$SCRIPT_DIR/com.yaas.dashboard.plist.template"
YAAS_LAUNCHD_LABEL="com.yaas.dashboard"
YAAS_LAUNCHD_LOADED="runs continuously on http://localhost:8877"

yaas_launchd_install_guidance() {
  echo
  echo "Logs: $REPO_ROOT/logs/dashboard.{out,err}.log"
  echo "Open: open http://localhost:8877"
}

. "$SCRIPT_DIR/install-launchd-common.sh"
yaas_install_launchd_job "${1:-install}"
