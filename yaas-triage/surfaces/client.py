#!/usr/bin/env python3
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

"""
client.py — the one HTTP client: Slack MCP, Slack Web API, and Jira REST.

Replaces mcp-call.sh (86), jira-call.sh (175) and slack-react.sh (83), which each rolled
their own Keychain read, curl invocation and error classification.

They disagreed about what an error IS:

    jira-call.sh    0 ok  1 auth  2 error  3 args  4 TRANSIENT
    mcp-call.sh     0 ok  1 auth  2 error  3 args     (no transient class)
    slack-react.sh  0 ok  1 auth  2 error  3 args     (no transient class)

So a Slack 429 surfaced as exit 2, indistinguishable from a permanent failure. That is
how rate limits were misfiled as hard errors, and before the backoff landed a hard error
woke a paid worker every 60 seconds. Roughly 1,380 lifetime occurrences of exactly this
are visible in triage.log. Giving all three surfaces ONE taxonomy is the point of this
file; sharing the plumbing is a side benefit.

Usage:
  client.py mcp <tool_name> <arguments_json>
  client.py jira <METHOD> <path-with-query> [body_json]
  client.py slack-react <add|remove> <channel_id> <message_ts> <emoji>

Exit codes, identical across all three:
  0 success   1 auth   2 error   3 bad args   4 transient (retry, do not dispatch)

Secrets: tokens are read from the macOS Keychain and passed as in-process headers, so
unlike the shell versions they never exist in an argv the process table can show. The
curl `--config -` dance the shell scripts needed is not required here.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OK, AUTH, ERROR, BAD_ARGS, TRANSIENT = 0, 1, 2, 3, 4

TIMEOUT = 30
SLACK_MCP_URL = "https://mcp.slack.com/mcp"
SLACK_WEB_URL = "https://slack.com/api"

# Body-level markers. Slack frequently answers HTTP 200 with a failure in the payload,
# so status alone is not enough.
TRANSIENT_MARKERS = ("ratelimited", "rate_limited", "rate limit", "service_unavailable",
                     "internal_error", "timeout", "temporarily")
AUTH_MARKERS = ("invalid_auth", "not_authed", "token_revoked", "token_expired",
                "account_inactive", "missing_scope", "unauthorized", "unauthenticated")
IDEMPOTENT_OK = ("already_reacted", "no_reaction")


# ── classification (pure, and therefore the part worth unit testing) ──────────

def _repo_root(start):
    """The repo root is the nearest ancestor directory that contains yaas-triage/.

    NOT counted as `parent.parent`: that is correct only while every script sits directly
    in yaas-triage/, and silently resolves to yaas-triage/ itself once a script moves into
    a subdirectory, producing a parallel state/ tree nothing reads. NOT keyed on CLAUDE.md
    (a fresh clone has only CLAUDE.example.md) and NOT on .git (two git dirs here, none in
    fixtures). Ambient $REPO_ROOT is deliberately ignored: a stale value pointing at another
    checkout would pass any marker check and silently redirect writes. Test fixtures copy
    the whole tree, so the walk-up finds the fixture on its own.

    Kept byte-identical across every file that needs it; tests/behaviour/repo-root.test.sh
    asserts that, because a shared module would need sys.path handling whose own path is
    depth-dependent, which is the bug being fixed.
    """
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


def classify_status(code):
    """HTTP status to exit code. One table for every surface."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return TRANSIENT
    if code == 0:
        return TRANSIENT               # no response at all: DNS, refused, timeout
    if 200 <= code < 300:
        return OK
    if code in (401, 403):
        return AUTH
    if code == 429 or code in (502, 503, 504):
        return TRANSIENT
    return ERROR


def classify_body(body, idempotent=False):
    """Inspect a response body for failures the status line did not express.

    `idempotent=True` is for reactions.add / reactions.remove, where "already in the
    desired state" is the outcome the caller wanted, not a failure.
    """
    if not body:
        return OK
    text = body if isinstance(body, str) else str(body)
    err = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if data.get("ok") is True:
                return OK
            raw = data.get("error")
            if isinstance(raw, dict):
                err = str(raw.get("message", ""))
            elif raw is not None:
                err = str(raw)
    except Exception:
        err = text                      # plain-text error, e.g. a bare "ratelimited"
    if not err:
        return OK
    low = err.lower()
    if idempotent and any(m in low for m in IDEMPOTENT_OK):
        return OK
    if any(m in low for m in TRANSIENT_MARKERS):
        return TRANSIENT
    if any(m in low for m in AUTH_MARKERS):
        return AUTH
    return ERROR


def classify_exception(exc):
    """A transport failure is retryable; a programming error is not."""
    if isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError, OSError)):
        return TRANSIENT
    return ERROR


def unwrap_sse(raw):
    """mcp.slack.com answers as either plain JSON or `data: <json>` SSE framing."""
    if not raw:
        return raw
    for line in raw.splitlines():
        if line.startswith("data: "):
            return line[6:].strip()
    stripped = raw.strip()
    return stripped if stripped.startswith("{") else raw


def extract_mcp_text(body):
    """Slack MCP convention: the payload lives at result.content[0].text."""
    try:
        return json.loads(body)["result"]["content"][0]["text"]
    except Exception:
        return ""


def mcp_error(body):
    """The JSON-RPC error message, or None."""
    try:
        err = json.loads(body).get("error")
    except Exception:
        return None
    if err is None:
        return None
    return str(err.get("message", err)) if isinstance(err, dict) else str(err)


def check_jira_path(path):
    """Return an error string if this path is not safe to send a token to.

    A full URL here would ship Basic-auth credentials to an arbitrary host, and `//host`
    is protocol-relative and does the same.
    """
    if not path or not path.startswith("/") or path.startswith("//"):
        return f"path must be an absolute path beginning with '/' (got: {path!r})"
    return None


# ── plumbing ─────────────────────────────────────────────────────────────────

def keychain(service, account="yaas"):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def request(url, method="GET", headers=None, body=None, timeout=TIMEOUT):
    """Returns (status, text). Raises only for transport failures."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # An HTTP error is a real response; its body usually says what went wrong.
        return e.code, e.read().decode("utf-8", "replace")


def fail(msg, code):
    print(f"ERROR: {msg}", file=sys.stderr)
    return code


def env_from_dotenv(name):
    """Read one var from the repo .env without sourcing the whole file.


    the original shell orchestrator exports these before a headless dispatch, but an interactive call gets a
    fresh shell that has not, which used to make a working setup look unconfigured.
    """
    val = os.environ.get(name)
    if val:
        return val.strip().strip('"').strip("'")
    env_file = _repo_root(__file__) / ".env"
    try:
        for line in env_file.read_text().splitlines():
            m = re.match(rf"^\s*{re.escape(name)}=(.*)$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ── surfaces ─────────────────────────────────────────────────────────────────

def cmd_mcp(argv):
    if len(argv) < 2:
        return fail("usage: client.py mcp <tool_name> <arguments_json>", BAD_ARGS)
    tool, raw_args = argv[0], argv[1]
    try:
        args = json.loads(raw_args)
    except Exception as exc:
        return fail(f"arguments_json is not valid JSON: {exc}", BAD_ARGS)

    token = keychain("slack-xoxp-token")
    if not token:
        return fail("no xoxp token in keychain (service=slack-xoxp-token, account=yaas)", AUTH)

    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    try:
        status, raw = request(SLACK_MCP_URL, "POST", {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Authorization": f"Bearer {token}",
        }, payload)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", classify_exception(exc))

    body = unwrap_sse(raw)
    verdict = classify_status(status)
    if verdict == OK:
        verdict = classify_body(body)
    if verdict != OK:
        detail = mcp_error(body) or body[:200]
        return fail(f"slack mcp {tool} (HTTP {status}): {detail}", verdict)

    err = mcp_error(body)
    if err:
        return fail(f"slack mcp returned error: {err}", classify_body(body) or ERROR)

    sys.stdout.write(extract_mcp_text(body))
    return OK


def cmd_jira(argv):
    if len(argv) < 2:
        return fail("usage: client.py jira <METHOD> <path> [body_json]", BAD_ARGS)
    method, path = argv[0].upper(), argv[1]
    body = argv[2] if len(argv) > 2 else None

    bad = check_jira_path(path)
    if bad:
        return fail(bad, BAD_ARGS)

    base = env_from_dotenv("JIRA_BASE_URL")
    email = env_from_dotenv("JIRA_EMAIL")
    if not base or not email:
        return fail("JIRA_BASE_URL and JIRA_EMAIL must be set (add them to the repo .env)", AUTH)
    token = keychain("jira-api-token")
    if not token:
        return fail("no Jira API token in keychain (service=jira-api-token, account=yaas)", AUTH)

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    try:
        status, text = request(base.rstrip("/") + path, method, {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        }, body)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", classify_exception(exc))

    verdict = classify_status(status)
    if verdict == OK:
        sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
        return OK
    detail = ""
    try:
        data = json.loads(text)
        detail = (data.get("errorMessages") or [""])[0] or str(data.get("errors", ""))
    except Exception:
        detail = text[:200]
    return fail(f"jira {method} {path} (HTTP {status}): {detail}", verdict)


def cmd_slack_react(argv):
    if len(argv) != 4:
        return fail("usage: client.py slack-react <add|remove> <channel_id> <ts> <emoji>", BAD_ARGS)
    action, channel, ts, emoji = argv
    if action not in ("add", "remove"):
        return fail(f"action must be 'add' or 'remove', got {action!r}", BAD_ARGS)

    token = keychain("slack-xoxp-token")
    if not token:
        return fail("no xoxp token in keychain (service=slack-xoxp-token, account=yaas)", AUTH)

    form = urllib.parse.urlencode({"channel": channel, "timestamp": ts, "name": emoji})
    try:
        status, text = request(f"{SLACK_WEB_URL}/reactions.{action}", "POST", {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {token}",
        }, form)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", classify_exception(exc))

    verdict = classify_status(status)
    if verdict == OK:
        # already_reacted / no_reaction mean the end-state the caller wanted already
        # holds, which is all a lifecycle swap cares about.
        verdict = classify_body(text, idempotent=True)
    if verdict != OK:
        return fail(f"slack reactions.{action}: {text[:200]}", verdict)
    return OK


SURFACES = {"mcp": cmd_mcp, "jira": cmd_jira, "slack-react": cmd_slack_react}


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return BAD_ARGS
    handler = SURFACES.get(sys.argv[1])
    if handler is None:
        return fail(f"unknown surface {sys.argv[1]!r}; expected one of {', '.join(SURFACES)}",
                    BAD_ARGS)
    return handler(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
