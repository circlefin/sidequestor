#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

# Shared mechanics for the three public launchd installer entry points.

yaas_install_launchd_job() {
  local action="${1:-install}"
  local plist_dest="$HOME/Library/LaunchAgents/$YAAS_LAUNCHD_LABEL.plist"

  case "$action" in
    install)
      if [ ! -f "$YAAS_LAUNCHD_TEMPLATE" ]; then
        echo "ERROR: template not found at $YAAS_LAUNCHD_TEMPLATE" >&2
        return 1
      fi

      if [ -f "$plist_dest" ]; then
        launchctl unload "$plist_dest" 2>/dev/null || true
      fi

      mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"
      sed -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" -e "s|{{HOME}}|$HOME|g" \
        "$YAAS_LAUNCHD_TEMPLATE" > "$plist_dest"

      launchctl load "$plist_dest"
      echo "✓ Installed: $plist_dest"
      echo "✓ Loaded: $YAAS_LAUNCHD_LABEL ($YAAS_LAUNCHD_LOADED)"
      yaas_launchd_install_guidance
      ;;

    uninstall)
      if [ -f "$plist_dest" ]; then
        launchctl unload "$plist_dest" 2>/dev/null || true
        rm -f "$plist_dest"
        echo "✓ Uninstalled $YAAS_LAUNCHD_LABEL"
      else
        echo "Not installed."
      fi
      ;;

    status)
      echo "=== launchctl list ==="
      launchctl list | grep -E "PID|$YAAS_LAUNCHD_LABEL" || echo "Not loaded."
      echo
      if [ -f "$plist_dest" ]; then
        echo "=== plist at $plist_dest ==="
        cat "$plist_dest"
      else
        echo "Plist not installed."
      fi
      ;;

    *)
      echo "Usage: $0 [install|uninstall|status]" >&2
      return 1
      ;;
  esac
}
