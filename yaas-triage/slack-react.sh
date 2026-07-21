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

# slack-react.sh — add or remove a reaction on a Slack message via the Slack
# Web API. The Slack MCP surface exposes slack_add_reaction but NOT a remove
# tool, so the reaction-lifecycle swaps (§ Reactions Fast Path) go through here.
# Uses the same keychain xoxp token as mcp-call.sh, so the reaction owner is
# always Guangmian's own user — which is required for reactions.remove (you can
# only remove your own reaction).
#
# Usage:
#   ./slack-react.sh <add|remove> <channel_id> <message_ts> <emoji>
#
# Example:
#   ./slack-react.sh remove D0A0LMEFWBY 1784590105.979059 claude-intensifies
#   ./slack-react.sh add    D0A0LMEFWBY 1784590105.979059 claudeloading
#
# emoji is the name WITHOUT colons (e.g. claudeloading, updatedone).
#
# Exit codes: 0 = ok, 1 = auth/token issue, 2 = Slack API error, 3 = bad args.
# On success prints nothing; on API error prints the Slack error to stderr.
# already_reacted (add) and no_reaction (remove) are treated as success — the
# desired end-state already holds, which is all the caller cares about.

set -euo pipefail

if [ $# -ne 4 ]; then
  echo "Usage: $0 <add|remove> <channel_id> <message_ts> <emoji>" >&2
  exit 3
fi

ACTION="$1"; CHANNEL="$2"; TS="$3"; EMOJI="$4"
case "$ACTION" in
  add)    METHOD="reactions.add" ;;
  remove) METHOD="reactions.remove" ;;
  *) echo "ERROR: action must be 'add' or 'remove', got '$ACTION'" >&2; exit 3 ;;
esac

TOKEN=$(security find-generic-password -s slack-xoxp-token -a yaas -w 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  echo "ERROR: no xoxp token in keychain (service=slack-xoxp-token, account=yaas)" >&2
  exit 1
fi

# Token goes in an Authorization header fed via --config on stdin so it never
# appears in the process table (same pattern as mcp-call.sh).
RESP=$(curl -sS -X POST "https://slack.com/api/$METHOD" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "channel=$CHANNEL" \
  --data-urlencode "timestamp=$TS" \
  --data-urlencode "name=$EMOJI" \
  --config - <<CURLCFG
header = "Authorization: Bearer $TOKEN"
CURLCFG
)

unset TOKEN

if printf '%s' "$RESP" | jq -e '.ok' >/dev/null 2>&1; then
  exit 0
fi

ERR=$(printf '%s' "$RESP" | jq -r '.error // "unknown_error"')
# Idempotent no-ops: the end-state already matches what the caller wanted.
if [ "$ERR" = "already_reacted" ] || [ "$ERR" = "no_reaction" ]; then
  exit 0
fi
echo "ERROR: Slack $METHOD failed: $ERR" >&2
exit 2
