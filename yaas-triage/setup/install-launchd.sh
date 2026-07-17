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
# repo's triage.sh. After install, triage.sh runs every 60 seconds until uninstalled.
#
# Usage:
#   ./install-launchd.sh            # install
#   ./install-launchd.sh uninstall  # remove
#   ./install-launchd.sh status     # show current status

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TRIAGE_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.yaas.triage.plist.template"
LABEL="com.yaas.triage"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

ACTION="${1:-install}"

case "$ACTION" in
  install)
    if [ ! -f "$TEMPLATE" ]; then
      echo "ERROR: template not found at $TEMPLATE" >&2
      exit 1
    fi

    # Unload any existing version first (idempotent)
    if [ -f "$PLIST_DEST" ]; then
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$REPO_ROOT/logs"

    # Render template
    sed -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" -e "s|{{HOME}}|$HOME|g" "$TEMPLATE" > "$PLIST_DEST"

    launchctl load "$PLIST_DEST"
    echo "✓ Installed: $PLIST_DEST"
    echo "✓ Loaded: $LABEL (fires every 60s)"
    echo
    echo "Logs: $REPO_ROOT/logs/triage.{out,err}.log"
    echo "Tail live: tail -f $REPO_ROOT/logs/triage.out.log"
    echo
    echo "First run will fire within 60s. To run now:"
    echo "  $TRIAGE_DIR/triage.sh"
    ;;

  uninstall)
    if [ -f "$PLIST_DEST" ]; then
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
      rm -f "$PLIST_DEST"
      echo "✓ Uninstalled $LABEL"
    else
      echo "Not installed."
    fi
    ;;

  status)
    echo "=== launchctl list ==="
    launchctl list | grep -E "PID|$LABEL" || echo "Not loaded."
    echo
    if [ -f "$PLIST_DEST" ]; then
      echo "=== plist at $PLIST_DEST ==="
      cat "$PLIST_DEST"
    else
      echo "Plist not installed."
    fi
    ;;

  *)
    echo "Usage: $0 [install|uninstall|status]" >&2
    exit 1
    ;;
esac
