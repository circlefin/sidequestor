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
#   1. Reading adapter configuration
#   2. Optionally running Slack OAuth with PKCE and storing a rotating token pair
#   3. Running a smoke test when local Slack checking is enabled
#   4. Optionally installing the launchd jobs
#
# Idempotent: re-running it rotates the token.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT=""
OAUTH_ONLY=0
CONFIG="$SCRIPT_DIR/yaas-app-config.json"
PORT=3118
STATE_SERVER_LOG=$(mktemp)
trap 'rm -f "$STATE_SERVER_LOG"' EXIT

# ── Preflight ───────────────────────────────────────────────────────────────
for cmd in jq python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  fi
done

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found" >&2
  exit 1
fi

# ── App manifest ────────────────────────────────────────────────────────────
# Generated from yaas-app-config.json rather than kept as a second copy: the
# scope list is the thing that drifts, and a manifest pasted into Slack months
# ago is invisible from here. Whatever this prints is exactly what setup.sh will
# then ask Slack to authorize. The output is Slack's YAML manifest format.
print_manifest() {
  jq -r '
    "display_information:",
    "  name: " + .slack_app.app_name,
    "  description: " + (.slack_app.description // "Personal Slack assistant."),
    "oauth_config:",
    "  redirect_urls:",
    "    - " + .slack_app.redirect_uri,
    "  scopes:",
    "    user:",
    (.user_scopes[] | "      - " + .),
    "settings:",
    "  org_deploy_enabled: false",
    "  socket_mode_enabled: false",
    "  token_rotation_enabled: true",
    "  is_mcp_enabled: true"
  ' "$CONFIG"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest) print_manifest; exit 0 ;;
    --workspace) WORKSPACE_ROOT="${2:?--workspace requires a path}"; shift 2 ;;
    --oauth-only) OAUTH_ONLY=1; shift ;;
    *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
  esac
done
REPO_ROOT="${WORKSPACE_ROOT:-$(cd "$TRIAGE_DIR/.." && pwd)}"
export YAAS_WORKSPACE="$REPO_ROOT"
ENV_FILE="$REPO_ROOT/.env"

# Load per-install values from .env
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and configure the adapters you use." >&2
  exit 1
fi
# shellcheck source=../../.env
set -a; source "$ENV_FILE"; set +a

# Every approval needs a real quest owner. Create the permanent Inbox before any
# launchd job or dashboard can accept unlinked work.
python3 "$TRIAGE_DIR/ledger/approval-helper.py" ensure-inbox >/dev/null

SLACK_CHECKERS_ENABLED="${SIDEQUESTOR_SLACK_CHECKERS_ENABLED:-${YAAS_SLACK_CHECKERS_ENABLED:-1}}"
CHECKER_CONNECTORS="${SIDEQUESTOR_CHECKER_CONNECTORS:-${YAAS_CHECKER_CONNECTORS:-slack,email,github,jira}}"
case ",$CHECKER_CONNECTORS," in
  *,slack,*) ;;
  *) SLACK_CHECKERS_ENABLED=0 ;;
esac
# Reject an unknown or duplicated connector here, at the one interactive moment, rather than
# letting every later tick exit 2 on BadEnvKnob. The known set comes from the runtime loader
# so this check cannot drift from what tick.py enforces.
if CONNECTOR_ERROR="$(python3 - "$TRIAGE_DIR" "$CHECKER_CONNECTORS" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import tick_state
try:
    tick_state.load_checker_connectors({"YAAS_CHECKER_CONNECTORS": sys.argv[2]})
except ValueError as exc:
    print(str(exc).replace("YAAS_CHECKER_CONNECTORS", "SIDEQUESTOR_CHECKER_CONNECTORS"))
PY
)"; then
  if [ -n "$CONNECTOR_ERROR" ]; then
    echo "ERROR: $CONNECTOR_ERROR" >&2
    exit 1
  fi
else
  echo "ERROR: could not validate SIDEQUESTOR_CHECKER_CONNECTORS=$CHECKER_CONNECTORS" >&2
  exit 1
fi
case "$SLACK_CHECKERS_ENABLED" in
  0|1) ;;
  *)
    echo "ERROR: SIDEQUESTOR_SLACK_CHECKERS_ENABLED must be 0 or 1, got: $SLACK_CHECKERS_ENABLED" >&2
    exit 1
    ;;
esac

if [ "$SLACK_CHECKERS_ENABLED" = "0" ]; then
  echo "Slack Python checkers are disabled. Skipping Slack app validation and OAuth."
  echo "Scheduled workers may still use Slack through their configured agent MCP."
else
for cmd in curl openssl security open; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command for Slack OAuth not found: $cmd" >&2
    exit 1
  fi
done
MISSING=""
for var in SLACK_APP_ID SLACK_CLIENT_ID SLACK_WORKSPACE_NAME SLACK_WORKSPACE_DOMAIN; do
  [ -z "${!var:-}" ] && MISSING="$MISSING $var"
done
if [ -n "$MISSING" ]; then
  # The app has to exist before these values can exist, so an empty .env almost
  # always means "has not created the app yet" rather than "typo". Hand over the
  # manifest instead of a filename to go read.
  cat >&2 <<'BANNER'

Slack app not configured yet. You need to create one in your workspace first;
it takes about two minutes.

  1. Open https://api.slack.com/apps → Create New App → From an app manifest
  2. Pick your workspace, then paste the manifest below
  3. Create, then Install to Workspace (your admin may need to approve it)
  4. From Basic Information, copy App ID and Client ID into .env
  5. Confirm that Agents → "Slack Model Context Protocol (MCP) Server" is enabled; the manifest requests it, but workspace policy may still require a manual toggle
  6. In OAuth and Permissions → User Token Scopes, verify the requested scopes

──────────────────────── paste this ────────────────────────
BANNER
  print_manifest >&2
  cat >&2 <<BANNER
────────────────────────────────────────────────────────────

Then fill in$MISSING in $ENV_FILE and run this again.
Re-print the manifest any time with: $0 --manifest

BANNER
  exit 1
fi

# Generic values from the JSON config (scopes, redirect URI, PKCE flag, app_name)
APP_NAME=$(jq -r '.slack_app.app_name' "$CONFIG")
REDIRECT=$(jq -r '.slack_app.redirect_uri' "$CONFIG")
SCOPES=$(jq -r '.user_scopes | join(",")' "$CONFIG")

# Per-install values from .env
CLIENT_ID="$SLACK_CLIENT_ID"
APP_URL="https://api.slack.com/apps/$SLACK_APP_ID"

cat <<EOF
╔════════════════════════════════════════════════════════════════════╗
║  Sidequestor — Slack onboarding                                    ║
╠════════════════════════════════════════════════════════════════════╣
║  App:         $APP_NAME ($APP_URL)
║  Client ID:   $CLIENT_ID
║  Redirect:    $REDIRECT
║  Scopes:      $(echo "$SCOPES" | tr ',' '\n' | head -3 | tr '\n' ',' | sed 's/,$//'), ... ($(echo "$SCOPES" | tr ',' '\n' | wc -l | tr -d ' ') total)
╚════════════════════════════════════════════════════════════════════╝

Confirm that Agents → "Slack Model Context Protocol (MCP) Server" is enabled. The manifest
requests it; if Slack did not apply that setting, enable it here:
  $APP_URL/app-assistant

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
<html><head><title>Sidequestor installed</title>
<style>body{font-family:-apple-system,sans-serif;max-width:600px;margin:80px auto;padding:0 24px;color:#111;}
h1{color:#2eb67d;}code{background:#f4f4f4;padding:2px 6px;border-radius:3px;}</style>
</head><body>
<h1>Sidequestor authorized</h1>
<p>The token has been issued and stored in your Mac Keychain.</p>
<p>You can close this tab and return to your terminal.</p>
</body></html>"""

RESPONSE_ERR = lambda msg: f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system;max-width:600px;margin:80px auto;padding:0 24px;">
<h1 style="color:#e01e5a;">Sidequestor install failed</h1>
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

TEAM=$(printf '%s' "$EXCHANGE" | jq -r '.team.name // .team.id')
USER_ID=$(printf '%s' "$EXCHANGE" | jq -r '.authed_user.id')

if [ -z "$USER_ID" ] || [ "$USER_ID" = "null" ]; then
  echo "ERROR: no user ID in OAuth response. Response had keys:" >&2
  printf '%s' "$EXCHANGE" | jq 'del(.access_token, .refresh_token, .authed_user.access_token, .authed_user.refresh_token) | keys' >&2
  exit 1
fi

echo "✓ Rotating credentials issued for user $USER_ID in workspace $TEAM"

# ── Store in keychain ───────────────────────────────────────────────────────
# The complete OAuth response travels over stdin. Neither token appears in a
# subprocess argument, and the credential module validates the replacement pair
# before changing Keychain.
if ! printf '%s' "$EXCHANGE" \
  | python3 "$TRIAGE_DIR/surfaces/slack_credentials.py" install "$CLIENT_ID" >/dev/null; then
  echo "ERROR: rotating Slack credentials were not installed" >&2
  exit 1
fi

unset EXCHANGE
unset CODE
unset CODE_VERIFIER

echo "✓ Rotating credential bundle stored in macOS Keychain"
echo

# ── Quick connectivity check ────────────────────────────────────────────────
echo "Testing Slack MCP connectivity..."
echo
TEST_RESULT=$("$TRIAGE_DIR/surfaces/mcp-call.sh" slack_read_user_profile \
  "{\"user_id\":\"$USER_ID\"}" 2>&1 || true)
if printf '%s' "$TEST_RESULT" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); sys.exit(0 if d else 1)" 2>/dev/null; then
  echo "✓ Slack MCP connection OK."
  # Keep the prior token available for rollback until the new bundle proves it
  # can read Slack. Deletion itself carries no secret in argv.
  security delete-generic-password -s slack-xoxp-token -a yaas >/dev/null 2>&1 || true
else
  echo "⚠️  Slack MCP check returned unexpected output. Token may need rotation."
  echo "   Output: $(printf '%s' "$TEST_RESULT" | head -c 200)"
  echo "   You can proceed — run 'python3 yaas-triage/tick.py' manually to test further."
fi
fi

# The package orchestrator owns launchd installation. This mode performs only
# the OAuth and credential work against the selected workspace.
if [ "$OAUTH_ONLY" = "1" ]; then
  echo "OAuth setup complete. Launchd installation is managed by 'sq start'."
  exit 0
fi

# Launchd installation is owned by the package CLI. The legacy installer scripts
# create fixed com.yaas.* labels and can race an instance-scoped Sidequestor job.
echo
echo "Launchd installation is managed by 'sq start' for this workspace."
echo "Run 'sq start' after setup to install triage, heartbeat, and dashboard together."

# ── Offer daily yaas-v2 auto-sync ───────────────────────────────────────────
# For people who just want to run the latest YAAS and not build/extend it
# themselves. Wires up a second git-dir (.git-yaas-v2) tracking the canonical
# template read-only, then flips settings.json -> sync.yaas_v2_auto_pull so
# the orchestrator pulls it once a day. Never touches files that are already
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
    echo "  the orchestrator will check for yaas-v2 updates once every 24h."
    echo "  Result of each attempt: state/yaas-v2-sync-status.json"
  else
    echo "init-yaas-v2-tracking.sh not found yet — skipping."
  fi
else
  echo "Skipped. You're on your own for pulling yaas-v2 updates — a normal"
  echo "'git pull' works fine if you haven't diverged from the template."
fi

# ── Verify the install ──────────────────────────────────────────────────────
# Prove the code is actually correct on THIS machine before you trust it. doctor.sh
# checks the environment; run-all.sh runs the unit/behaviour suites + the end-to-end
# differential goldens against tick.py (throwaway fixtures, no network, no real state).
# Warn-only: setup's configuration and any selected OAuth, keychain, or launchd work is already done above, so a
# test failure is surfaced loudly but does not undo the install.
echo
read -p "Run the verification suite now to confirm the install is correct? [Y/n]: " RUN_TESTS
if [[ ! "$RUN_TESTS" =~ ^[Nn] ]]; then
  echo
  echo "── doctor.sh (environment) ──"
  bash "$TRIAGE_DIR/ops/doctor.sh" || echo "  ⚠ doctor reported issues (see above)."
  echo
  echo "── run-all.sh (code correctness) ──"
  if bash "$TRIAGE_DIR/tests/run-all.sh"; then
    echo "✓ All tests passed — the install is verified."
  else
    echo "⚠ Some tests failed (see above). The install steps already completed remain intact,"
    echo "  but something in this checkout or environment is off — fix it before relying on YAAS."
  fi
else
  echo "Skipped. Verify any time with:"
  echo "  bash $TRIAGE_DIR/ops/doctor.sh && bash $TRIAGE_DIR/tests/run-all.sh"
fi

echo
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  Sidequestor setup complete                                        ║"
echo "╠════════════════════════════════════════════════════════════════════╣"
echo "║  Manual run:     python3 $TRIAGE_DIR/tick.py"
echo "║  Dry run:        DRY_RUN=1 python3 $TRIAGE_DIR/tick.py"
echo "║  Dashboard:      $TRIAGE_DIR/ops/dashboard-start.sh  (http://localhost:8877)"
if [ "$SLACK_CHECKERS_ENABLED" = "1" ]; then
  echo "║  Token refresh:  automatic (manual: slack_credentials.py refresh-now)"
  echo "║  Revoke:         Settings → Manage apps in your Slack workspace    ║"
else
  echo "║  Slack adapter:  local Python checking disabled in .env            ║"
fi
echo "╚════════════════════════════════════════════════════════════════════╝"
