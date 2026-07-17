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

# setup.sh — one-time colleague onboarding for yaas
#
# Walks a new user through:
#   1. Reading the yaas app config
#   2. OAuth flow with PKCE (no client secret needed)
#   3. Storing the issued xoxp token in their macOS keychain
#   4. Running a smoke test
#   5. Optionally installing the launchd job
#
# Idempotent: re-running it rotates the token.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TRIAGE_DIR/.." && pwd)"
CONFIG="$SCRIPT_DIR/yaas-app-config.json"
ENV_FILE="$REPO_ROOT/.env"
PORT=3118
STATE_SERVER_LOG=$(mktemp)
trap 'rm -f "$STATE_SERVER_LOG"' EXIT

# ── Preflight ───────────────────────────────────────────────────────────────
for cmd in jq curl python3 openssl security open; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  fi
done

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found" >&2
  exit 1
fi

# Load per-install values from .env
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and fill in your Slack app values." >&2
  exit 1
fi
# shellcheck source=../../.env
set -a; source "$ENV_FILE"; set +a

for var in SLACK_APP_ID SLACK_CLIENT_ID SLACK_WORKSPACE_NAME SLACK_WORKSPACE_DOMAIN; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set in $ENV_FILE — see .env.example" >&2
    exit 1
  fi
done

# Generic values from the JSON config (scopes, redirect URI, PKCE flag, app_name)
APP_NAME=$(jq -r '.slack_app.app_name' "$CONFIG")
REDIRECT=$(jq -r '.slack_app.redirect_uri' "$CONFIG")
SCOPES=$(jq -r '.user_scopes | join(",")' "$CONFIG")

# Per-install values from .env
CLIENT_ID="$SLACK_CLIENT_ID"
APP_URL="https://api.slack.com/apps/$SLACK_APP_ID"

cat <<EOF
╔════════════════════════════════════════════════════════════════════╗
║  yaas — Slack onboarding                                           ║
╠════════════════════════════════════════════════════════════════════╣
║  App:         $APP_NAME ($APP_URL)
║  Client ID:   $CLIENT_ID
║  Redirect:    $REDIRECT
║  Scopes:      $(echo "$SCOPES" | tr ',' '\n' | head -3 | tr '\n' ',' | sed 's/,$//'), ... ($(echo "$SCOPES" | tr ',' '\n' | wc -l | tr -d ' ') total)
╚════════════════════════════════════════════════════════════════════╝

You'll be redirected to Slack in your browser to authorize this app for
your user account. Review the permissions, click "Allow", then return here.

EOF

read -p "Press Enter to start the OAuth flow, or Ctrl-C to abort..."

# ── PKCE ────────────────────────────────────────────────────────────────────
# RFC 7636 Proof Key for Code Exchange
CODE_VERIFIER=$(openssl rand -base64 64 | tr -d '=\n' | tr '/+' '_-' | head -c 64)
CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" \
  | openssl dgst -sha256 -binary \
  | openssl base64 \
  | tr '/+' '_-' \
  | tr -d '=\n')
STATE_NONCE=$(openssl rand -hex 16)

# ── Build authorize URL ─────────────────────────────────────────────────────
AUTH_URL="https://slack.com/oauth/v2/authorize"
AUTH_URL+="?client_id=$CLIENT_ID"
AUTH_URL+="&user_scope=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$SCOPES")"
AUTH_URL+="&redirect_uri=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$REDIRECT")"
AUTH_URL+="&code_challenge=$CODE_CHALLENGE"
AUTH_URL+="&code_challenge_method=S256"
AUTH_URL+="&state=$STATE_NONCE"

# ── Start local callback server ─────────────────────────────────────────────
# The server writes the received code (or error) to $STATE_SERVER_LOG and exits.
python3 - "$PORT" "$STATE_SERVER_LOG" "$STATE_NONCE" <<'PY' &
import http.server, urllib.parse, sys, socket, os, html

PORT = int(sys.argv[1])
LOG_PATH = sys.argv[2]
EXPECTED_STATE = sys.argv[3]

RESPONSE_OK = b"""<!DOCTYPE html>
<html><head><title>yaas installed</title>
<style>body{font-family:-apple-system,sans-serif;max-width:600px;margin:80px auto;padding:0 24px;color:#111;}
h1{color:#2eb67d;}code{background:#f4f4f4;padding:2px 6px;border-radius:3px;}</style>
</head><body>
<h1>yaas authorized</h1>
<p>The token has been issued and stored in your Mac Keychain.</p>
<p>You can close this tab and return to your terminal.</p>
</body></html>"""

RESPONSE_ERR = lambda msg: f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system;max-width:600px;margin:80px auto;padding:0 24px;">
<h1 style="color:#e01e5a;">yaas install failed</h1>
<p>{msg}</p>
<p>Check your terminal for details.</p>
</body></html>""".encode()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/callback":
            self.send_response(404); self.end_headers(); return
        q = urllib.parse.parse_qs(u.query)
        code = q.get("code", [""])[0]
        state = q.get("state", [""])[0]
        err = q.get("error", [""])[0]
        if err:
            msg = "error=" + html.escape(err)  # escape: err is reflected into HTML
            with open(LOG_PATH, "w") as f: f.write(f"ERROR {msg}\n")
            self.send_response(400); self.send_header("Content-Type","text/html"); self.end_headers()
            self.wfile.write(RESPONSE_ERR(msg))
            return
        if state != EXPECTED_STATE:
            with open(LOG_PATH, "w") as f: f.write("ERROR state mismatch\n")
            self.send_response(400); self.send_header("Content-Type","text/html"); self.end_headers()
            self.wfile.write(RESPONSE_ERR("State mismatch — possible CSRF. Re-run setup.sh."))
            return
        with open(LOG_PATH, "w") as f: f.write(f"CODE {code}\n")
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(RESPONSE_OK)

# Bind with SO_REUSEADDR
class Srv(http.server.HTTPServer):
    allow_reuse_address = True

try:
    srv = Srv(("127.0.0.1", PORT), H)
except OSError as e:
    with open(LOG_PATH, "w") as f: f.write(f"ERROR port bind: {e}\n")
    sys.exit(1)

srv.timeout = 300  # 5 min total window
srv.handle_request()  # serve exactly one request then exit
PY

SERVER_PID=$!
sleep 0.5  # let server bind

echo "Opening browser to Slack authorize page..."
echo "If it doesn't open automatically, visit:"
echo "  $AUTH_URL"
echo
open "$AUTH_URL"

# Wait for the server to finish (max ~5 min via its own timeout)
wait "$SERVER_PID" 2>/dev/null || true

# ── Inspect server log ──────────────────────────────────────────────────────
if [ ! -s "$STATE_SERVER_LOG" ]; then
  echo "ERROR: no callback received. The server timed out or the browser flow was interrupted." >&2
  exit 1
fi

RESULT=$(cat "$STATE_SERVER_LOG")
if [[ "$RESULT" == ERROR* ]]; then
  echo "OAuth flow failed: ${RESULT#ERROR }" >&2
  exit 1
fi

CODE=$(printf '%s' "$RESULT" | awk '/^CODE/ {print $2}')
if [ -z "$CODE" ]; then
  echo "ERROR: no code in callback: $RESULT" >&2
  exit 1
fi

echo "✓ Authorization code received"

# ── Exchange code for token ─────────────────────────────────────────────────
echo "Exchanging code for user OAuth token..."
# Pass the fields via a curl config on stdin (--config -) rather than -d on the
# argv, so the single-use auth code and PKCE verifier never appear in `ps`.
EXCHANGE=$(curl -sS -X POST https://slack.com/api/oauth.v2.access --config - <<CURLCFG
data = "client_id=$CLIENT_ID"
data = "code=$CODE"
data = "code_verifier=$CODE_VERIFIER"
data = "redirect_uri=$REDIRECT"
CURLCFG
)

OK=$(printf '%s' "$EXCHANGE" | jq -r '.ok')
if [ "$OK" != "true" ]; then
  ERROR=$(printf '%s' "$EXCHANGE" | jq -r '.error // "unknown"')
  echo "ERROR: token exchange failed: $ERROR" >&2
  echo "Full response (secrets redacted):" >&2
  printf '%s' "$EXCHANGE" | jq 'del(.access_token, .authed_user.access_token, .authed_user.refresh_token)' >&2
  exit 1
fi

TOKEN=$(printf '%s' "$EXCHANGE" | jq -r '.authed_user.access_token')
TEAM=$(printf '%s' "$EXCHANGE" | jq -r '.team.name // .team.id')
USER_ID=$(printf '%s' "$EXCHANGE" | jq -r '.authed_user.id')

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "ERROR: no user token in response. Response had keys:" >&2
  printf '%s' "$EXCHANGE" | jq 'keys' >&2
  exit 1
fi

echo "✓ Token issued for user $USER_ID in workspace $TEAM (length ${#TOKEN})"

# ── Store in keychain ───────────────────────────────────────────────────────
# Note: `security add-generic-password -w "$TOKEN"` puts the token on this
# process's argv for the ~ms the `security` binary runs, briefly visible via
# `ps` to other local users. The macOS `security` CLI has no argv-free way to
# pass a generic-password value (no stdin mode), so this one-time, setup-moment
# exposure is accepted. The recurring token use (mcp-call.sh) is argv-free.
security delete-generic-password -s slack-xoxp-token -a yaas > /dev/null 2>&1 || true
security add-generic-password \
  -s slack-xoxp-token \
  -a yaas \
  -w "$TOKEN" \
  -D "Slack user OAuth token for yaas bash triage" \
  -j "Issued $(date -u +%Y-%m-%dT%H:%M:%SZ) for user $USER_ID in $TEAM via setup.sh"

unset TOKEN
unset EXCHANGE
unset CODE
unset CODE_VERIFIER

echo "✓ Token stored in macOS Keychain (service=slack-xoxp-token, account=yaas)"
echo

# ── Quick connectivity check ────────────────────────────────────────────────
echo "Testing Slack MCP connectivity..."
echo
TEST_RESULT=$("$TRIAGE_DIR/mcp-call.sh" slack_read_user_profile '{"user_id":"me"}' 2>&1 || true)
if printf '%s' "$TEST_RESULT" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); sys.exit(0 if d else 1)" 2>/dev/null; then
  echo "✓ Slack MCP connection OK."
else
  echo "⚠️  Slack MCP check returned unexpected output. Token may need rotation."
  echo "   Output: $(printf '%s' "$TEST_RESULT" | head -c 200)"
  echo "   You can proceed — run triage.sh manually to test further."
fi

# ── Offer launchd install ───────────────────────────────────────────────────
echo
read -p "Install the launchd job to run triage.sh every 60 seconds? [y/N]: " INSTALL_LAUNCHD
if [[ "$INSTALL_LAUNCHD" =~ ^[Yy] ]]; then
  if [ -x "$SCRIPT_DIR/install-launchd.sh" ]; then
    "$SCRIPT_DIR/install-launchd.sh"
  else
    echo "install-launchd.sh not found yet — skipping. Run it later when available."
  fi
else
  echo "Skipped. You can run triage.sh manually any time:"
  echo "  $TRIAGE_DIR/triage.sh"
fi

# ── Offer launchd install for the dashboard ─────────────────────────────────
echo
read -p "Install the launchd job to keep the dashboard running continuously? [y/N]: " INSTALL_DASHBOARD
if [[ "$INSTALL_DASHBOARD" =~ ^[Yy] ]]; then
  if [ -x "$SCRIPT_DIR/install-launchd-dashboard.sh" ]; then
    "$SCRIPT_DIR/install-launchd-dashboard.sh"
  else
    echo "install-launchd-dashboard.sh not found yet — skipping. Run it later when available."
  fi
else
  echo "Skipped. You can start the dashboard manually any time:"
  echo "  $TRIAGE_DIR/dashboard.sh"
fi

# ── Offer daily yaas-v2 auto-sync ───────────────────────────────────────────
# For people who just want to run the latest YAAS and not build/extend it
# themselves. Wires up a second git-dir (.git-yaas-v2) tracking the canonical
# template read-only, then flips settings.json -> sync.yaas_v2_auto_pull so
# triage.sh pulls it once a day. Never touches files that are already
# customized — see sync-yaas-v2.sh.
echo
read -p "Opt into daily auto-sync from the public yaas-v2 template? [y/N]: " SYNC_V2
if [[ "$SYNC_V2" =~ ^[Yy] ]]; then
  if [ -x "$SCRIPT_DIR/init-yaas-v2-tracking.sh" ]; then
    "$SCRIPT_DIR/init-yaas-v2-tracking.sh"
    SETTINGS_FILE="$REPO_ROOT/settings.json"
    if [ ! -f "$SETTINGS_FILE" ]; then
      cp "$REPO_ROOT/settings.json.example" "$SETTINGS_FILE"
    fi
    python3 - "$SETTINGS_FILE" <<'PYEOF'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
d.setdefault("sync", {})["yaas_v2_auto_pull"] = True
json.dump(d, open(path, "w"), indent=2)
PYEOF
    echo "✓ settings.json: sync.yaas_v2_auto_pull = true"
    echo "  triage.sh will check for yaas-v2 updates once every 24h."
    echo "  Result of each attempt: state/yaas-v2-sync-status.json"
  else
    echo "init-yaas-v2-tracking.sh not found yet — skipping."
  fi
else
  echo "Skipped. You're on your own for pulling yaas-v2 updates — a normal"
  echo "'git pull' works fine if you haven't diverged from the template."
fi

echo
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  yaas setup complete                                               ║"
echo "╠════════════════════════════════════════════════════════════════════╣"
echo "║  Manual run:     $TRIAGE_DIR/triage.sh"
echo "║  Dry run:        DRY_RUN=1 $TRIAGE_DIR/triage.sh"
echo "║  Dashboard:      $TRIAGE_DIR/dashboard.sh  (http://localhost:8877)"
echo "║  Rotate token:   rerun setup.sh"
echo "║  Revoke:         Settings → Manage apps in your Slack workspace    ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
