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

# mcp-call.sh — call a tool on mcp.slack.com via JSON-RPC over HTTP
#
# Usage:
#   ./mcp-call.sh <tool_name> <arguments_json>
#
# Example:
#   ./mcp-call.sh slack_search_public_and_private '{"query":"to:me","limit":5}'
#
# Requires:
#   - Keychain entry: service=slack-xoxp-token, account=yaas
#   - jq, curl, python3
#
# Output (stdout):
#   The "content[0].text" field from the tool response, which for Slack tools
#   is a JSON string describing results. Callers should pipe through jq/python
#   to extract fields.
#
# Exit codes:
#   0 = success, 1 = auth/token issue, 2 = MCP error, 3 = bad args

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <tool_name> <arguments_json>" >&2
  exit 3
fi

TOOL_NAME="$1"
TOOL_ARGS="$2"

TOKEN=$(security find-generic-password -s slack-xoxp-token -a yaas -w 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  echo "ERROR: no xoxp token in keychain (service=slack-xoxp-token, account=yaas)" >&2
  exit 1
fi

REQ=$(jq -cn --arg name "$TOOL_NAME" --argjson args "$TOOL_ARGS" \
  '{jsonrpc:"2.0", id:1, method:"tools/call", params:{name:$name, arguments:$args}}')

# The Authorization header carries the long-lived xoxp token. Pass it via a
# curl config on stdin (--config -) instead of -H on the argv, so the token is
# never visible in the process table (`ps`) to other local processes/users.
RAW=$(curl -sS -X POST https://mcp.slack.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -d "$REQ" \
  --config - <<CURLCFG
header = "Authorization: Bearer $TOKEN"
CURLCFG
)

unset TOKEN

# Handle Server-Sent Events framing (some responses come as "data: <json>\n\n")
BODY=$(printf '%s' "$RAW" | awk '/^data: /{sub(/^data: /,""); print; exit} /^\{/{print; exit}')
if [ -z "$BODY" ]; then
  BODY="$RAW"
fi

# Check for MCP error
if printf '%s' "$BODY" | jq -e '.error' >/dev/null 2>&1; then
  ERR=$(printf '%s' "$BODY" | jq -r '.error.message // .error')
  echo "ERROR: MCP returned error: $ERR" >&2
  exit 2
fi

# Extract content[0].text (Slack MCP convention)
printf '%s' "$BODY" | jq -r '.result.content[0].text // empty'
