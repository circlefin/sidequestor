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

# jira-call.sh — call the Jira Cloud REST API directly, over Basic auth.
#
# The interactive Atlassian MCP (mcp__plugin_circle-mcp-atlassian_atlassian__*)
# is a Claude Code *plugin* authenticated by an interactive OAuth flow. It is not
# listed in the repo .mcp.json, so the headless dispatch (triage.sh -> claude -p)
# never receives it, and OAuth would not survive a launchd session anyway. This
# script is the headless equivalent of mcp-call.sh's Slack bridge: it hits the
# Jira REST API with a long-lived API token, so the dispatched worker can poll
# ticket status and read comments without the MCP.
#
# Usage:
#   ./jira-call.sh <METHOD> <path-with-query> [body_json]
#
# Examples:
#   # status + summary of every issue matching a label
#   ./jira-call.sh GET '/rest/api/3/search/jql?jql=labels%3Dmy-label&fields=status,summary'
#   # one issue's status
#   ./jira-call.sh GET '/rest/api/3/issue/PROJ-123?fields=status'
#   # its comments (read reviewer replies)
#   ./jira-call.sh GET '/rest/api/3/issue/PROJ-123/comment'
#
# Requires:
#   - Keychain entry: service=jira-api-token, account=yaas
#       create the token at https://id.atlassian.com/manage-profile/security/api-tokens
#       then: security add-generic-password -s jira-api-token -a yaas -w '<TOKEN>'
#   - JIRA_EMAIL and JIRA_BASE_URL env vars. Put them in the repo-root .env
#     (gitignored); triage.sh sources it with `set -a`, so they reach this script
#     even when it is invoked from a checker subprocess:
#       JIRA_EMAIL=you@example.com
#       JIRA_BASE_URL=https://your-org.atlassian.net
#   - jq, curl
#
# Output (stdout):
#   The raw JSON response body. Pipe through jq to extract fields.
#
# Exit codes:
#   0 = success, 1 = auth/token issue, 2 = HTTP/API error, 3 = bad args,
#   4 = transient/retryable (429 rate-limit, 502/503/504) — callers should treat
#       this as "skip this tick", NOT as a hard error. checkers/jira.py maps it
#       to the `ratelimited` token so triage skips instead of burning a dispatch.

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <METHOD> <path-with-query> [body_json]" >&2
  exit 3
fi

METHOD="$1"
REQ_PATH="$2"
BODY_JSON="${3:-}"

# No defaults on purpose: a hardcoded account/host would be wrong for anyone else
# and would leak an identity into this file. Both come from .env (see header).
BASE_URL="${JIRA_BASE_URL:-}"
EMAIL="${JIRA_EMAIL:-}"
if [ -z "$BASE_URL" ] || [ -z "$EMAIL" ]; then
  echo "ERROR: JIRA_BASE_URL and JIRA_EMAIL must be set (add them to the repo-root .env)" >&2
  exit 1
fi

# Path must be absolute (/rest/api/3/...). Guard against a full URL being passed,
# which would let the token leak to an arbitrary host.
case "$REQ_PATH" in
  /*) : ;;
  *) echo "ERROR: path must start with '/' (got: $REQ_PATH)" >&2; exit 3 ;;
esac

TOKEN=$(security find-generic-password -s jira-api-token -a yaas -w 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  echo "ERROR: no Jira API token in keychain (service=jira-api-token, account=yaas)" >&2
  exit 1
fi

# Basic auth = base64("email:token"). Build it locally so the token never lands
# in the process table.
AUTH_B64=$(printf '%s:%s' "$EMAIL" "$TOKEN" | base64 | tr -d '\n')
unset TOKEN

# Assemble curl args. Auth header goes via --config on stdin (not argv) so it is
# invisible to `ps`. Capture the HTTP status on the last line for error handling.
CURL_ARGS=(-sS -X "$METHOD"
  -H "Accept: application/json"
  -H "Content-Type: application/json"
  --connect-timeout 10 --max-time 25
  -w $'\n%{http_code}')
if [ -n "$BODY_JSON" ]; then
  CURL_ARGS+=(-d "$BODY_JSON")
fi

# `set -e` would abort here on any curl failure (timeout, DNS, refused) before
# the HTTP-code branches below could classify it, making transient network
# errors indistinguishable from hard errors. Capture curl's status instead.
set +e
RAW=$(curl "${CURL_ARGS[@]}" "${BASE_URL}${REQ_PATH}" --config - <<CURLCFG
header = "Authorization: Basic $AUTH_B64"
CURLCFG
)
CURL_RC=$?
set -e
unset AUTH_B64

if [ "$CURL_RC" -ne 0 ]; then
  case "$CURL_RC" in
    # 6 DNS, 7 connect refused, 28 timeout, 35 SSL, 52 empty reply,
    # 55/56 send/recv error — all retryable transport failures.
    6|7|28|35|52|55|56)
      echo "TRANSIENT: curl exit $CURL_RC (network/timeout) — retryable" >&2
      exit 4 ;;
    *)
      echo "ERROR: curl exit $CURL_RC" >&2
      exit 2 ;;
  esac
fi

HTTP_CODE=$(printf '%s' "$RAW" | tail -n1)
BODY=$(printf '%s' "$RAW" | sed '$d')

case "$HTTP_CODE" in
  401|403)
    echo "ERROR: Jira auth failed (HTTP $HTTP_CODE) — check token/email/permissions" >&2
    exit 1 ;;
  2*)
    printf '%s\n' "$BODY"
    exit 0 ;;
  429|502|503|504)
    # Transient. Distinct exit code so pre-dispatch checkers can skip the tick
    # rather than read it as dirty and burn an Opus dispatch that finds nothing.
    echo "TRANSIENT: Jira returned HTTP $HTTP_CODE — retryable" >&2
    exit 4 ;;
  000)
    # curl could not complete (timeout / DNS / connection refused).
    echo "TRANSIENT: no HTTP response (timeout or connection failure)" >&2
    exit 4 ;;
  *)
    ERR=$(printf '%s' "$BODY" | jq -r '(.errorMessages // [])[0] // (.errors | tostring) // empty' 2>/dev/null || true)
    echo "ERROR: Jira API returned HTTP $HTTP_CODE${ERR:+: $ERR}" >&2
    exit 2 ;;
esac
