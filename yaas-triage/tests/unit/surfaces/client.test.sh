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

# test-client.sh — the one HTTP client for Slack MCP, Slack Web and Jira.
#
# This replaced three shell scripts (mcp-call.sh, jira-call.sh, slack-react.sh) that
# each rolled their own keychain read, curl invocation, and error classification. They
# DISAGREED about what an error is:
#
#   jira-call.sh   0 ok  1 auth  2 error  3 args  4 TRANSIENT
#   mcp-call.sh    0 ok  1 auth  2 error  3 args     (no transient class)
#   slack-react.sh 0 ok  1 auth  2 error  3 args     (no transient class)
#
# So a Slack 429 came back as exit 2 "MCP error", indistinguishable from a permanent
# failure. That is precisely how rate limits got misfiled as hard errors and woke a paid
# worker every 60 seconds. One taxonomy for all three is the point of this change, not
# tidiness.
#
# The network parts are not exercised here; the CLASSIFICATION is, which is where the
# bugs were.

set -u
# Suites live in yaas-triage/tests/; SCRIPT_DIR points at yaas-triage/ so every
# reference to a helper stays exactly as it was written.
# yaas-triage/, found by walking up rather than by counting "..": these suites live at
# varying depths under tests/, and counting is the bug A1 removed from the scripts.
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d/yaas-triage"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate yaas-triage/ above $1" >&2; return 1
}
SCRIPT_DIR="$(_find_triage "$0")" || exit 1
. "$SCRIPT_DIR/tests/lib/harness.sh"

python3 - "$SCRIPT_DIR" <<'PYEOF'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("client", f"{sys.argv[1]}/surfaces/client.py")
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)

P = F = 0
def ok(m):
    global P; P += 1; print(f"  \033[32mPASS\033[0m {m}")
def bad(m):
    global F; F += 1; print(f"  \033[31mFAIL\033[0m {m}")
def eq(label, got, want):
    ok(label) if got == want else bad(f"{label} (got {got!r}, want {want!r})")

print("── one exit taxonomy, shared by all three surfaces ────────────────────────")
eq("OK",        c.OK, 0)
eq("AUTH",      c.AUTH, 1)
eq("ERROR",     c.ERROR, 2)
eq("BAD_ARGS",  c.BAD_ARGS, 3)
eq("TRANSIENT", c.TRANSIENT, 4)

print()
print("── HTTP status classification is identical everywhere ─────────────────────")
for code, want, why in [
    (200, c.OK,        "success"),
    (204, c.OK,        "success with no body"),
    (401, c.AUTH,      "unauthorised"),
    (403, c.AUTH,      "forbidden"),
    (429, c.TRANSIENT, "RATE LIMIT — the case the Slack scripts got wrong"),
    (500, c.ERROR,     "server error, not retryable by us"),
    (502, c.TRANSIENT, "bad gateway"),
    (503, c.TRANSIENT, "unavailable"),
    (504, c.TRANSIENT, "gateway timeout"),
    (404, c.ERROR,     "not found"),
    (0,   c.TRANSIENT, "no response at all"),
]:
    eq(f"HTTP {code} -> {why}", c.classify_status(code), want)

print()
print("── a rate limit in the BODY counts too, whatever the status ───────────────")
# Slack answers 200 with {"ok":false,"error":"ratelimited"} often enough that status
# alone is not sufficient. mcp-call.sh classified this as a hard error.
eq("body says ratelimited",      c.classify_body('{"ok":false,"error":"ratelimited"}'), c.TRANSIENT)
eq("body says rate_limited",     c.classify_body('{"error":"rate_limited"}'),           c.TRANSIENT)
eq("plain-text ratelimited",     c.classify_body('ratelimited'),                        c.TRANSIENT)
eq("body says invalid_auth",     c.classify_body('{"ok":false,"error":"invalid_auth"}'), c.AUTH)
eq("body says token_revoked",    c.classify_body('{"error":"token_revoked"}'),          c.AUTH)
eq("an ordinary body is fine",   c.classify_body('{"ok":true,"messages":"..."}'),       c.OK)
eq("an unrelated error is ERROR",c.classify_body('{"ok":false,"error":"channel_not_found"}'), c.ERROR)

print()
print("── network failures are transient, not hard errors ────────────────────────")
for exc, want in [
    (TimeoutError("timed out"),               c.TRANSIENT),
    (ConnectionResetError("reset by peer"),   c.TRANSIENT),
    (ConnectionRefusedError("refused"),       c.TRANSIENT),
    (OSError("dns"),                          c.TRANSIENT),
    (ValueError("nonsense"),                  c.ERROR),
]:
    eq(f"{type(exc).__name__} -> {'TRANSIENT' if want == c.TRANSIENT else 'ERROR'}",
       c.classify_exception(exc), want)

print()
print("── SSE framing is unwrapped (mcp.slack.com answers either way) ────────────")
eq("data: framed",     c.unwrap_sse('data: {"jsonrpc":"2.0","id":1}\n\n'), '{"jsonrpc":"2.0","id":1}')
eq("plain json",       c.unwrap_sse('{"jsonrpc":"2.0","id":1}'),           '{"jsonrpc":"2.0","id":1}')
eq("event: then data", c.unwrap_sse('event: message\ndata: {"a":1}\n\n'),  '{"a":1}')
eq("unrecognised is returned as-is", c.unwrap_sse("garbage"), "garbage")

print()
print("── the MCP envelope is unwrapped to the text callers expect ───────────────")
env = '{"result":{"content":[{"type":"text","text":"hello"}]}}'
eq("content[0].text",              c.extract_mcp_text(env), "hello")
eq("no content -> empty",          c.extract_mcp_text('{"result":{}}'), "")
eq("malformed -> empty",           c.extract_mcp_text("not json"), "")

print()
print("── an MCP protocol error is surfaced, not swallowed ───────────────────────")
err = '{"error":{"code":-32000,"message":"tool not found"}}'
eq("error detected", c.mcp_error(err), "tool not found")
eq("no error",       c.mcp_error('{"result":{}}'), None)
# The one that matters: a rate limit reported through the MCP error channel must be
# TRANSIENT, not a hard error.
eq("a rate limit in the MCP error channel is transient",
   c.classify_body('{"error":{"message":"ratelimited"}}'), c.TRANSIENT)

print()
print("── idempotent Slack reactions are success, not failure ────────────────────")
eq("already_reacted on add",  c.classify_body('{"ok":false,"error":"already_reacted"}', idempotent=True), c.OK)
eq("no_reaction on remove",   c.classify_body('{"ok":false,"error":"no_reaction"}',     idempotent=True), c.OK)
eq("...but only when asked",  c.classify_body('{"ok":false,"error":"already_reacted"}'), c.ERROR)

print()
print("── the jira path guard: never let a token reach another host ──────────────")
eq("absolute path allowed", c.check_jira_path("/rest/api/3/myself"), None)
for bad_path in ("https://evil.example.com/x", "rest/api/3/myself", "//evil.example.com/x"):
    if c.check_jira_path(bad_path) is None:
        bad(f"path guard allowed {bad_path!r}")
    else:
        ok(f"path guard rejects {bad_path!r}")

print()
print(f"  ── module: {P} passed, {F} failed")
sys.exit(1 if F else 0)
PYEOF
MODRC=$?
[ "$MODRC" -eq 0 ] || FAIL=$((FAIL+1))

echo
echo "── the CLI validates arguments before touching the network ────────────────"
run() { python3 "$SCRIPT_DIR/surfaces/client.py" "$@" >/dev/null 2>&1; echo $?; }
eq "no args"                  "$(run)"                          "3"
eq "unknown surface"          "$(run nope a b)"                 "3"
eq "mcp with no args json"    "$(run mcp slack_read_channel)"   "3"
eq "mcp with bad json"        "$(run mcp tool 'not json')"      "3"
eq "jira with no path"        "$(run jira GET)"                 "3"
eq "jira with a full URL"     "$(run jira GET https://evil.example.com/x)" "3"
eq "react with wrong action"  "$(run slack-react sideways C1 1.1 x)" "3"
eq "react with too few args"  "$(run slack-react add C1)"       "3"

echo
echo "── the documented shims still exist and still work ────────────────────────"
# CLAUDE.md and several skills tell the worker to call these by name. Renaming them
# would silently break every one of those instructions.
for shim in mcp-call.sh jira-call.sh slack-react.sh; do
  if [ -x "$SCRIPT_DIR/surfaces/$shim" ]; then ok "$shim still present and executable"
  else bad "$shim is missing — worker instructions reference it by name"; fi
done
eq "mcp-call.sh forwards bad args"  "$(bash "$SCRIPT_DIR/surfaces/mcp-call.sh" 2>/dev/null; echo $?)" "3"
eq "jira-call.sh forwards bad args" "$(bash "$SCRIPT_DIR/surfaces/jira-call.sh" 2>/dev/null; echo $?)" "3"

echo
echo "────────────────────────────────────────────────────────────────────────────"
echo "client: $PASS shell checks passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
