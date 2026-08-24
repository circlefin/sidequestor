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
dashboard-server.py — YAAS live dashboard server.

Serves dashboard.html and a live state API. Handles manual review actions.

Usage:
    python3 dashboard-server.py [port]   (default: 8877)

Endpoints:
    GET  /                    → dashboard.html
    GET  /yaas-triage/assets/sidequestor-mark.png → dashboard logo
    GET  /api/dashboard       → live JSON snapshot of all quest state
    GET  /api/briefs          → canonical Markdown briefings
    GET  /state/<path>        → raw state file
    POST /api/review/<id>     → mark item reviewed (optionally with edits)
    POST /api/edit/<id>       → update a reviewed draft in place
    POST /api/cancel/<id>     → cancel item
    POST /api/undo/<id>       → undo reviewed/cancelled back to pending_review
    POST /api/reclaim/<id>    → recover an expired executing lease
    POST /api/quests          → create a draft-only quest from an operator prompt
    POST /api/workspace/open  → open this workspace in Cursor or the default app
"""

from __future__ import annotations  # PEP 604 unions below must not be
# evaluated at def time: this file has to import on Python < 3.10.

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

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
    override = os.environ.get("YAAS_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


SCRIPT_DIR     = Path(__file__).parent
REPO_ROOT      = _repo_root(__file__)
RUNTIME_ROOT   = Path(os.environ.get("YAAS_RUNTIME_ROOT", SCRIPT_DIR.parent.parent))
STATE_DIR      = REPO_ROOT / "state"
LOG_DIR        = REPO_ROOT / "logs"
APPROVALS_FILE = STATE_DIR / "pending-approvals.json"
sys.path.insert(0, str(RUNTIME_ROOT / "yaas-triage"))
import approval_state
import approval_store
import tick_check
from tick_state import Config, NUMERIC_KNOBS, load_environment, load_watch_manifests

# Statuses that are genuinely finished. Everything else is shown, deliberately: the
# queue used to ALLOWLIST pending_review/needs_reply, so `executing` and `reviewed`
# were invisible by construction and an approval stuck mid-execution had no surface at
# all. Default-visible means a status nobody thought about shows up instead of
# vanishing.
TERMINAL_APPROVAL_STATUSES = ("executed", "cancelled")
DASHBOARD_HTML = RUNTIME_ROOT / "dashboard.html"
DASHBOARD_LOGO = RUNTIME_ROOT / "yaas-triage" / "assets" / "sidequestor-mark.png"
PORT           = int(sys.argv[1]) if len(sys.argv) > 1 else 8877


def build_workspace_identity() -> dict:
    marker = REPO_ROOT / ".yaas" / "instance.json"
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "display_name": str(data.get("display_name") or REPO_ROOT.name),
        "path": str(REPO_ROOT),
        "instance_id": str(data.get("instance_id") or ""),
    }

# Workspace-specific hosts. No hardcoded defaults: they belong to whoever runs
# this, so they come from the gitignored repo-root .env (the same file the orchestrator
# sources). Without them, a reconstructed Slack/Jira link is simply omitted; a
# stored permalink still works, because it carries its own host.
def _dotenv(key: str, default: str = "") -> str:
    value = load_environment(REPO_ROOT).get(key)
    return str(value).strip() if value not in (None, "") else default

SLACK_HOST = _dotenv("SLACK_WORKSPACE_DOMAIN")            # e.g. acme.slack.com
JIRA_HOST  = (_dotenv("JIRA_BASE_URL").replace("https://", "")
                                      .replace("http://", "").rstrip("/"))

WORKER_TIMEOUT_S = 1800  # compatibility fallback for pre-lifecycle-record worker logs
WORKER_STATE_FILE = STATE_DIR / "triage" / "worker-current.json"
WORKER_HEARTBEAT_GRACE_S = 60
LIVE_TAIL_LINES  = 60   # panel wants the fuller transcript; pill only shows the target name
MAX_REQUEST_BODY_BYTES = 64 * 1024
QUEST_CREATE_TIMEOUT_S = 10


# ── Auth / hardening ──────────────────────────────────────────────────────────
# The dashboard exposes read APIs (quest state, draft approval text) and a POST
# that can flip an approval to "reviewed" (→ the worker then sends it). So it is
# a control plane, not a read-only viewer. Four layers, in order of what they
# stop:
#   1. Bind 127.0.0.1 (entry point) — off-host callers can't reach it at all.
#   2. Host-header allowlist — kills DNS-rebinding (a page using a hostname that
#      resolves to loopback still sends its own Host, which we reject).
#   3. HttpOnly + SameSite=Strict session cookie — kills cross-origin CSRF (the
#      browser won't attach the cookie to a cross-site request) and XSS token
#      theft (script can't read an HttpOnly cookie). Required on every /api,
#      /state and POST; the bare GET / bootstrap issues it.
#   4. CSP with a per-response script nonce — a missed HTML escape can't execute
#      injected JS, which would otherwise ride the same-origin cookie and bypass
#      1-3 entirely.
# Residual (by design): a process running as THIS user can GET / to mint a
# cookie. Defending that needs a real login, which is overkill for a personal
# localhost panel. This hardens the network + browser vectors, not same-user.
COOKIE_NAME   = "yaas_dash"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TOKEN_FILE    = STATE_DIR / "dashboard-token"

def _load_or_create_token() -> str:
    try:
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write 0600 from creation so the secret is never briefly world-readable.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok

SESSION_TOKEN = _load_or_create_token()

_CSP_JSON = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

def _csp_html(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    )


# ── Approvals file helpers (all writes use exclusive flock) ───────────────────

def _read_approvals() -> dict:
    return approval_store.read_queue()


# ── Slack deeplink + outbound-message normalization ───────────────────────────
# Shared by build_messages() (the Messages tab) and build_quest_detail()'s
# conversation list. Verbatim-only: a send is surfaced only when we can recover
# the exact body — an inline message_text on the event, or a join to an approval
# item by approval_id. Summary-only direct sends are counted but not listed.

_OUTBOUND_EVENTS = {
    "message_sent": "sent", "reply_sent": "sent", "dm_sent": "sent",
    "executed": "sent", "draft_posted": "draft", "email_replied": "email",
}
_SENT_EVENTS = {"message_sent", "reply_sent", "dm_sent", "executed"}
_MSG_ACTION_TYPES = ("slack_message", "email_reply")
# Slack channel/DM/group id: C/D/G + at least 7 alnum, and always >=1 digit.
# The digit requirement is what keeps a Jira project key (PROJ-1098) out.
_ID_RE = re.compile(r"\b([CDG](?=[A-Z0-9]*\d)[A-Z0-9]{6,})\b")

def _norm_channel(e: dict):
    """Best-effort channel id from the timeline's inconsistent keys."""
    for k in ("channel_id", "channel"):
        v = e.get(k)
        # Full-match the Slack id shape: a Jira key like PROJ-1098 also starts
        # with D and has no space, and used to be mistaken for a DM channel.
        if isinstance(v, str) and _ID_RE.fullmatch(v.strip()):
            return v.strip()
    for k in ("channel", "target", "source"):  # free-text like "self-DM D0A0…"
        v = e.get(k)
        if isinstance(v, str):
            m = _ID_RE.search(v)
            if m:
                return m.group(1)
    return None

_TS_RE = re.compile(r"\d{10}\.\d{3,}|\d{16,}")

def _slack_url(channel_id, ts=None, thread_ts=None, permalink=None,
               host=None):
    """Prefer a stored permalink; else construct a deterministic Slack deeplink.
    Host comes from SLACK_WORKSPACE_DOMAIN; a stored permalink always wins and
    carries the correct host for external Slack Connect channels."""
    if isinstance(permalink, str) and permalink.startswith("https://"):
        return permalink
    host = host or SLACK_HOST
    if not host or not channel_id or not _TS_RE.fullmatch(str(ts or "")):
        return None  # e.g. ts == "draft_saved": there is no message to point at
    url = f"https://{host}/archives/{channel_id}/p{str(ts).replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url

# ── Cross-surface deeplinks (Slack / Jira / GitHub / Gmail) ──────────────────
# A reply is not always a Slack message: the worker also posts Jira comments,
# GitHub PR comments/reviews and Gmail replies. Each of those has a canonical
# web URL we can rebuild from the identifiers the worker logs, so every outbound
# record in the UI can carry an "open in <surface>" link, not just Slack ones.
#
# Precedence: an explicitly logged URL always wins (it is what the API actually
# returned); otherwise reconstruct from ids. Kind drives the icon/label in the
# frontend, so classify by host when we only have a URL.

GMAIL_USER  = 0  # the /u/<n>/ slot for the work account in the browser profile
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_URL_FIELDS  = ("permalink", "dm_permalink", "thread_permalink", "message_url",
                "comment_url", "issue_url", "pr_url", "html_url", "ticket_url",
                "url")

def _kind_from_url(url: str) -> str:
    u = url.lower()
    if "slack.com" in u:            return "slack"
    if "atlassian.net" in u:        return "jira"
    if "github.com" in u:           return "github"
    if "mail.google.com" in u:      return "email"
    return "link"

def _first_str(e: dict, keys) -> str | None:
    for k in keys:
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, int):
            return str(v)
    return None

def _first_list_url(e: dict):
    """Some events log a list instead of a single field ("links": [...]). Take the
    first https URL in it. Deliberately narrow: a generic scan of every string
    value would happily return an attached Google Doc as if it were the reply."""
    for k in ("links", "urls", "permalinks"):
        v = e.get(k)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str) and x.startswith("https://"):
                    return x
    return None

def _jira_url(e: dict):
    """Jira issue (optionally focused on the comment we posted)."""
    raw = _first_str(e, ("jira", "jira_issue", "issue", "issue_key", "target"))
    if not raw or not JIRA_HOST:  # no JIRA_BASE_URL configured: nothing to link to
        return None
    m = next((x for x in _JIRA_KEY_RE.finditer(raw)
              if not x.group(1).startswith("DN-")), None)  # DN-### is not Jira
    if not m:
        return None
    url = f"https://{JIRA_HOST}/browse/{m.group(1)}"
    cid = _first_str(e, ("jira_comment_id", "comment_id"))
    if cid and cid.isdigit():
        url += (f"?focusedCommentId={cid}"
                "&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel"
                f"#comment-{cid}")
    return url

def _github_url(e: dict):
    """GitHub PR (optionally the exact comment), from repo + pr number."""
    repo = _first_str(e, ("repo", "github_repo"))
    pr   = _first_str(e, ("pr", "pr_number", "pull_request", "github_pr"))
    if not repo or "/" not in repo or not pr or not str(pr).isdigit():
        return None
    url = f"https://github.com/{repo}/pull/{pr}"
    rc = _first_str(e, ("review_comment_id",))
    ic = _first_str(e, ("github_comment_id", "issue_comment_id"))
    if rc and rc.isdigit():
        url += f"#discussion_r{rc}"
    elif ic and ic.isdigit():
        url += f"#issuecomment-{ic}"
    return url

def _gmail_url(e: dict):
    """Gmail message/thread. Prefer the id of the reply we actually sent."""
    mid = _first_str(e, ("sent_id", "gmail_sent_id", "gmail_message_id", "gmail_id",
                         "gmail_thread_id", "email_thread_id", "thread_id",
                         "message_id", "gmail_preview_id"))
    if not mid or not re.fullmatch(r"[0-9a-f]{8,}", mid):
        return None
    return f"https://mail.google.com/mail/u/{GMAIL_USER}/#all/{mid}"

def _ext_link(e: dict) -> tuple[str | None, str | None]:
    """(url, kind) for any timeline event / approval target. kind ∈
    slack | jira | github | email | link."""
    if not isinstance(e, dict):
        return None, None
    url = _first_str(e, _URL_FIELDS)
    if not url or not url.startswith("https://"):
        url = _first_list_url(e)
    if url and url.startswith("https://"):
        return url, _kind_from_url(url)
    at = e.get("action_type") or ""
    # Prefer the surface the action itself names; absent that, a channel id means
    # Slack, so try it first rather than latching onto a stray id field.
    order = (["slack", "jira", "github", "email"] if _norm_channel(e)
             else ["jira", "github", "email", "slack"])
    if "email" in at:
        order = ["email", "jira", "github", "slack"]
    elif "jira" in at:
        order = ["jira", "github", "email", "slack"]
    elif "github" in at or at == "pr_comment":
        order = ["github", "jira", "email", "slack"]
    elif "slack" in at:
        order = ["slack", "jira", "github", "email"]
    for kind in order:
        if kind == "jira":
            u = _jira_url(e)
        elif kind == "github":
            u = _github_url(e)
        elif kind == "email":
            u = _gmail_url(e)
        else:
            ch = _norm_channel(e)
            ts = _first_str(e, ("response_ts", "msg_ts", "message_ts", "thread_ts"))
            u = _slack_url(ch, ts, e.get("thread_ts"))
        if u:
            return u, kind
    return None, None

def _watch_link(w: dict, wtype: str, ch, tts) -> tuple[str | None, str | None]:
    """A watch entry points at a surface, not a single reply: a Slack thread, a
    Jira JQL set, a repo's PRs, or a Gmail query (no stable URL)."""
    if wtype == "jira":
        u = _jira_url(w)
        if u:
            return u, "jira"
        jql = w.get("jql")
        if JIRA_HOST and isinstance(jql, str) and jql.strip():
            return (f"https://{JIRA_HOST}/issues/?jql="
                    + urllib.parse.quote(jql.strip()), "jira")
        return None, None
    if wtype == "github_pr":
        u = _github_url(w)
        if u:
            return u, "github"
        repo = _first_str(w, ("repo", "github_repo"))
        return ((f"https://github.com/{repo}/pulls", "github")
                if repo and "/" in repo else (None, None))
    if wtype == "email":
        return None, None
    url = _slack_url(ch, tts, tts)
    if not url and ch:  # channel/DM watch: no thread ts, so link the channel
        url = f"https://{SLACK_HOST}/archives/{ch}" if SLACK_HOST else None
    return (url, "slack") if url else (None, None)

def _link_fields(e: dict) -> dict:
    """The two keys every record hands the frontend, plus the legacy slack_url
    (kept so older cached clients keep working)."""
    url, kind = _ext_link(e)
    return {"link_url": url, "link_kind": kind,
            "slack_url": url if kind == "slack" else None}


def _approvals_index(data: dict) -> dict:
    return {i.get("id"): i.get("message_text", "")
            for i in data.get("items", []) if i.get("id")}

def _clip(s, n):
    return s[:n] + "…" if isinstance(s, str) and len(s) > n else (s or "")

def _normalize_msg_event(e: dict, approvals_by_id: dict):
    """Outbound timeline event → compact verbatim message record, or None."""
    kind = _OUTBOUND_EVENTS.get(e.get("event"))
    if kind is None:
        return None
    body = e.get("message_text")
    if not body:
        appr_id = e.get("approval_id")
        if appr_id and appr_id in approvals_by_id:
            body = approvals_by_id[appr_id]
    if not body:
        return None  # verbatim-only: skip summary-only sends
    if e.get("event") == "executed" and e.get("action_type") == "email_reply":
        kind = "email"
    channel_id = _norm_channel(e)
    return {
        "ts":           e.get("ts"),
        "event":        e.get("event"),
        "kind":         kind,
        "channel_id":   channel_id,
        "thread_ts":    e.get("thread_ts"),
        "message_text": _clip(body, 2000),
        "note":         _clip(e.get("note"), 300),
        **_link_fields(e),
        "action_type":  e.get("action_type"),
        "awaiting_send": False,
    }

def _approval_target_link(i: dict) -> dict:
    """Link fields for an approval item. The target dict carries the surface ids
    (channel/thread for Slack, email_thread_id for Gmail, an issue key or PR for
    Jira/GitHub), so resolve against target merged with the item's action_type."""
    t = dict(i.get("target") or {})
    # An executed item also carries what the send returned (a permalink, a Jira
    # comment id, a Gmail message id) at the top level, so merge those in and the
    # history row links to the actual reply, not just the surface.
    for k in ("result_url", "permalink", "jira_comment_id", "comment_id",
              "github_comment_id", "review_comment_id", "repo", "pr",
              "gmail_message_id", "sent_id", "response_ts"):
        v = i.get(k)
        if v not in (None, ""):
            t[k] = v
    t.setdefault("action_type", i.get("action_type"))
    if isinstance(t.get("thread_ts"), str):
        t.setdefault("response_ts", t["thread_ts"])
    out = _link_fields(t)
    if out["link_url"] or i.get("action_type") in _MSG_ACTION_TYPES:
        return out
    # Legacy / loosely-logged non-message items (a Jira comment queued as a
    # remote_request) name the issue only in the prose. Fall back to the first
    # issue key found there, with the recorded comment id as the anchor.
    for field in ("context", "risk_reason", "message_text"):
        v = i.get(field)
        if not isinstance(v, str):
            continue
        # DN-### is a private local register prefix, not a real Jira project.
        mk = next((x for x in _JIRA_KEY_RE.finditer(v)
                   if not x.group(1).startswith("DN-")), None)
        if mk:
            cid = _first_str(i, ("jira_comment_id", "comment_id", "response_ts"))
            probe = {"jira": mk.group(1)}
            if cid and cid.isdigit() and len(cid) <= 9:  # a ts has a dot/17 digits
                probe["jira_comment_id"] = cid
            u = _jira_url(probe)
            if u:
                return {"link_url": u, "link_kind": "jira", "slack_url": None}
    return out

def _approval_card(i: dict) -> dict:
    """Project a pending-approval item into the shape the review card needs."""
    t = i.get("target") or {}
    now = datetime.now(timezone.utc)
    stalled = approval_state.is_stalled(i, now)
    return {
        "id":             i.get("id"),
        "quest_id":       i.get("quest_id"),
        "quest_title":    i.get("quest_title", i.get("quest_id")),
        "action_type":    i.get("action_type", "slack_message"),
        "status":         i.get("status"),
        "created_at":     i.get("created_at"),
        "asked_at":       i.get("asked_at"),
        "reviewed_at":    i.get("reviewed_at"),
        "executing_at":   i.get("executing_at"),
        "lease_expires_at": i.get("lease_expires_at"),
        "cancelled_at":   i.get("cancelled_at"),
        "sent_at":        i.get("sent_at"),
        "human_edited":   bool(i.get("human_edited")),
        "target":         t,
        "message_text":   i.get("message_text", ""),
        "context":        i.get("context", ""),
        "risk_reason":    i.get("risk_reason", ""),
        "review_note":    i.get("review_note", ""),
        "review_history": i.get("review_history", []),
        "worker_reply":   i.get("worker_reply", ""),
        "stalled":        stalled,
        "processing_error": i.get("processing_error", ""),
        "processing_error_at": i.get("processing_error_at"),
        "failed_from_status": i.get("failed_from_status"),
        "available_actions": approval_state.available_actions(i, now),
        **_approval_target_link(i),
    }

def _approval_draft_record(i: dict) -> dict:
    """A pending message-type approval, shown as an awaiting-send draft."""
    t = i.get("target") or {}
    return {
        "ts":           i.get("created_at"),
        "event":        "draft_posted",
        "kind":         "draft",
        "channel_id":   t.get("channel_id"),
        "thread_ts":    t.get("thread_ts"),
        "message_text": _clip(i.get("message_text", ""), 2000),
        "note":         "",
        **_approval_target_link(i),
        "action_type":  i.get("action_type"),
        "awaiting_send": True,
        "quest_id":     i.get("quest_id"),
        "quest_title":  i.get("quest_title", i.get("quest_id")),
    }


# ── Live run detector ──────────────────────────────────────────────────────
# run-agent.py owns the subprocess, so its atomic heartbeat record is lifecycle truth.
# Transcript parsing remains only for a run already in flight during an upgrade.

def _worker_log_lines(log_name):
    if not isinstance(log_name, str) or Path(log_name).name != log_name:
        return []
    try:
        return (LOG_DIR / log_name).read_text().splitlines()
    except OSError:
        return []


def _worker_tail(lines):
    return [l for l in lines[3:] if l.strip() and not l.startswith("===")][-LIVE_TAIL_LINES:]


def _heartbeat_age(value):
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc).timestamp() - then.timestamp()
    except (TypeError, ValueError):
        return None


def _lifecycle_matches_latest(current):
    """Reject a terminal record left behind when a newer lifecycle write failed."""
    latest = LOG_DIR / "worker-latest.log"
    try:
        return not latest.exists() or latest.resolve().name == current.get("log")
    except OSError:
        return False

def build_live_run() -> dict:
    not_running = {"running": False, "stale": False, "state": "idle",
                   "targets": [], "started_at": None, "tail": []}
    try:
        current = json.loads(WORKER_STATE_FILE.read_text())
    except (OSError, ValueError):
        current = None

    if (isinstance(current, dict) and current.get("schema") == 1
            and current.get("state") in ("running", "exited")
            and _lifecycle_matches_latest(current)):
        if current["state"] == "exited":
            return {**not_running, "state": "exited", "exit": current.get("exit"),
                    "ended_at": current.get("ended_at")}
        targets = [str(t) for t in current.get("targets", []) if t]
        age = _heartbeat_age(current.get("heartbeat_at"))
        stale = age is None or age > WORKER_HEARTBEAT_GRACE_S
        return {
            "running": not stale,
            "stale": stale,
            "state": "stale" if stale else "running",
            "targets": targets,
            "started_at": current.get("started_at"),
            "tail": _worker_tail(_worker_log_lines(current.get("log"))),
        }

    # Compatibility fallback for a pre-upgrade worker with no lifecycle record.
    log_path = LOG_DIR / "worker-latest.log"
    if not log_path.exists():
        return not_running

    try:
        st = log_path.stat()
        text = log_path.read_text()
    except Exception:
        return not_running

    lines = text.splitlines()
    if not lines:
        return not_running

    # Some agent backends emit the terminal stream event but do not append the
    # token footer. Treat that event as terminal too, otherwise a completed
    # worker remains visibly "running" until the stale timeout expires.
    finished = any(l.startswith("=== Tokens") for l in lines[-3:]) or any(
        l.startswith("[turn.completed]") for l in lines[-5:]
    )
    age_s = datetime.now(timezone.utc).timestamp() - st.st_mtime
    if finished or age_s > WORKER_TIMEOUT_S:
        return not_running

    started_at = None
    targets = []
    for l in lines[:5]:
        if l.startswith("=== Worker dispatch"):
            started_at = l.removeprefix("=== Worker dispatch").removesuffix("===").strip()
        elif l.startswith("Dirty targets:"):
            targets = [t.strip() for t in l.removeprefix("Dirty targets:").split(",") if t.strip()]
        elif l.startswith("Target:"):
            targets = [t.strip() for t in l.removeprefix("Target:").split(",") if t.strip()]

    return {
        "running":    True,
        "stale":      False,
        "state":      "running",
        "targets":    targets,
        "started_at": started_at,
        "tail":       _worker_tail(lines),
    }


# ── Open-items builder ────────────────────────────────────────────────────────
# A read-only, LLM-free rollup of the OPEN LOOPS across active quests — not the
# raw watch list (most watches are passive monitors, not things awaiting action).
# Each quest is classified into one primary state from its timeline.ndjson:
#   blocked        — last event is a blocker the worker couldn't clear
#   awaiting_reply — we sent something and nothing has come back since
#   draft          — a draft is posted/queued and nothing sent after it
#   (quests whose latest activity is inbound-and-handled, or pure monitors, are
#    NOT listed — they are not open loops.)
# Plus pending approvals (needs your review) and any persisted commitments the
# worker flagged as promised-but-unfulfilled. Computed on each poll from
# timeline.ndjson + pending-approvals.json + commitments file — no tokens.
# Deterministic between state changes so the ETag/304 poll path holds.

def _open_watch_types():
    """Return open-loop types and a displayable registry error.

    Triage owns fail-closed checker behavior. The dashboard is a read-only projection, so a
    broken manifest must not take unrelated quest data or controls offline.
    """
    try:
        manifests = load_watch_manifests(RUNTIME_ROOT / "yaas-triage")
    except (OSError, ValueError) as exc:
        return set(), str(exc)
    return {
        wtype for wtype, manifest in manifests.items() if manifest["open_loop"]
    }, None
_OUT_EVENTS = {"message_sent", "reply_sent", "dm_sent", "executed"}
_IN_EVENTS  = {"info_received"}
# Routine/no-loop events that never represent an open item on their own.
_ROUTINE_EVENTS = {"created", "note", "status_change", "brief_written",
                   "weekly_recap_posted", "weekly_recap_skipped", "dry_run",
                   "schedule_changed"}
COMMITMENTS_FILE = STATE_DIR / "open-commitments.json"

def _event_link(e: dict):
    url, kind = _ext_link(e)
    return url, kind, _norm_channel(e)

def build_open_items() -> dict:
    open_watch_types, registry_error = _open_watch_types()
    active_dir = STATE_DIR / "quests" / "active"
    blocked, awaiting_reply, drafts = [], [], []

    for quest_dir in sorted(active_dir.iterdir()) if active_dir.exists() else []:
        if not quest_dir.is_dir():
            continue
        try:
            meta = json.loads((quest_dir / "meta.json").read_text())
        except Exception:
            continue
        if meta.get("status") in ("completed", "cancelled"):
            continue
        tp = quest_dir / "timeline.ndjson"
        if not tp.exists():
            continue
        try:
            evs = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        except Exception:
            continue
        evs = [e for e in evs if isinstance(e, dict)]
        if not evs:
            continue

        base = {"id": quest_dir.name, "title": meta.get("title", quest_dir.name),
                "priority": meta.get("priority", "normal")}

        # Positional scan (newest = highest index). last_* = index of newest of type.
        # nonblocked = newest event of ANY type except "blocked" — a later note/
        # recap/action means the worker recovered (matches isBlockedNow in the
        # frontend), so a block only counts if nothing came after it.
        last = {"blocked": -1, "out": -1, "in": -1, "draft": -1,
                "material": -1, "nonblocked": -1}
        for idx, e in enumerate(evs):
            ev = e.get("event")
            if ev == "blocked":            last["blocked"] = idx
            else:                          last["nonblocked"] = idx
            if ev in _OUT_EVENTS:          last["out"] = idx;   last["material"] = idx
            elif ev in _IN_EVENTS:         last["in"] = idx;    last["material"] = idx
            elif ev == "draft_posted":     last["draft"] = idx; last["material"] = idx
            elif ev not in _ROUTINE_EVENTS and ev != "blocked": last["material"] = idx

        # Blocked wins: a blocker with nothing (not even a note) logged after it.
        if last["blocked"] >= 0 and last["blocked"] > last["nonblocked"]:
            be = evs[last["blocked"]]
            url, kind, _ = _event_link(be)
            blocked.append({**base, "ts": be.get("ts"),
                            "reason": _clip(be.get("reason") or be.get("note") or "", 200),
                            "slack_url": url, "link_url": url, "link_kind": kind})
            continue
        # Draft queued and nothing sent after it → awaiting your send.
        if last["draft"] > last["out"] and last["draft"] >= last["in"]:
            de = evs[last["draft"]]
            url, kind, _ = _event_link(de)
            drafts.append({**base, "ts": de.get("ts"),
                           "reason": _clip(de.get("note") or "", 200),
                           "slack_url": url, "link_url": url, "link_kind": kind})
            continue
        # We sent something and nothing has come back since → waiting on them.
        if last["out"] >= 0 and last["out"] > last["in"]:
            oe = evs[last["out"]]
            url, kind, _ = _event_link(oe)
            awaiting_reply.append({**base, "ts": oe.get("ts"),
                                   "reason": _clip(oe.get("note") or "", 200),
                                   "slack_url": url, "link_url": url, "link_kind": kind})

    # Pending approvals — things waiting on YOUR review/decision.
    review = []
    if APPROVALS_FILE.exists():
        try:
            for i in _read_approvals().get("items", []):
                if i.get("status") in TERMINAL_APPROVAL_STATUSES:
                    continue
                if i.get("action_type") == "manual_instruction":
                    continue
                t = i.get("target") or {}
                review.append({
                    "id": i.get("id"), "quest_id": i.get("quest_id"),
                    "title": i.get("quest_title", i.get("quest_id")),
                    "status": i.get("status"),
                    "action_type": i.get("action_type", "slack_message"),
                    **_approval_target_link(i),
                })
        except Exception:
            pass

    # Commitments the worker flagged as promised-but-unfulfilled (if the worker
    # persists them; empty until that plumbing exists).
    commitments = []
    if COMMITMENTS_FILE.exists():
        try:
            data = json.loads(COMMITMENTS_FILE.read_text())
            commitments = data.get("items", []) if isinstance(data, dict) else []
        except Exception:
            pass

    return {
        "blocked":        blocked,
        "awaiting_reply": awaiting_reply,
        "drafts":         drafts,
        "review":         review,
        "commitments":    commitments,
        "registry_error": registry_error,
        "total":          len(blocked) + len(awaiting_reply) + len(drafts)
                          + len(review) + len(commitments),
    }


# A quest counts as "rate limited" if it produced a gate_watch_ratelimited event within this
# window. Rate-limiting is transient (it recovers the next tick), so this is a recency window,
# not a persisted flag. It changes the payload only at genuine transitions (a fresh ratelimit,
# or one aging past the window) — a handful of times around an actual throttle — NOT on every
# poll the way a generated_at timestamp would, so it does not defeat the ETag/304 path in the
# spirit of the note below.
RATELIMIT_WINDOW_SEC = 300


def _recent_runlog_events(event_name: str, window_sec: int) -> list:
    """Recent run-log events of one type within window_sec of now, newest first. Tail-reads the
    run-log (bounded by _RUNLOG_TAIL_BYTES) so it stays cheap on a large log.

    Note the log is append-ordered by tick COMPLETION, but each line's `ts` is its logical start,
    so timestamps jitter out of order by up to a tick (~60s). We therefore scan the whole tail
    and filter by ts (`continue`), NOT break on the first out-of-window line — a break could drop
    a still-in-window event that happens to sit just after a slightly-older-ts neighbour. The
    tail-byte cap keeps this cheap regardless."""
    runlog = STATE_DIR / "run-log.ndjson"
    if not runlog.exists():
        return []
    now = datetime.now(timezone.utc)
    out = []
    try:
        size = runlog.stat().st_size
        with open(runlog, "rb") as f:
            if size > _RUNLOG_TAIL_BYTES:
                f.seek(size - _RUNLOG_TAIL_BYTES)
                f.readline()
            raw_lines = f.read().decode(errors="replace").splitlines()
        for raw in reversed(raw_lines):
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if not isinstance(e, dict) or e.get("event") != event_name:
                continue
            try:
                t = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - t).total_seconds() > window_sec:
                continue
            out.append(e)
    except Exception:
        return []
    return out


# ── Config builder ─────────────────────────────────────────────────────────────
# The knobs that shape a tick, read the same way the orchestrator reads them: env first, then
# REPO_ROOT/.env, then the built-in default. `set` marks whether the value is overridden
# anywhere (so the UI can show "default" vs a live override). Grouped for a readable Config tab.
# Deterministic between state changes (it only moves when .env / env changes), so it does not
# defeat the ETag/304 path.
def build_config() -> dict:
    resolved = Config(str(RUNTIME_ROOT / "yaas-triage"))

    def knob(key, default, desc):
        raw = resolved.env.get(key)
        return {"key": key, "value": raw if raw not in (None, "") else str(default),
                "default": str(default), "set": raw not in (None, ""), "desc": desc}

    groups = [
        {"title": "Adapters", "items": [
            knob("YAAS_SLACK_CHECKERS_ENABLED", 1,
                 "Free local slack_* checkers and the reaction sweep. Set to 0 when Slack is available only through a paid worker's MCP."),
        ]},
        {"title": "Concurrency", "items": [
            knob("YAAS_TRIAGE_MAX_PARALLEL", NUMERIC_KNOBS["YAAS_TRIAGE_MAX_PARALLEL"],
                 "Quests checked at once = peak simultaneous Slack calls. Low on purpose: "
                 "burst concurrency is what trips Slack's rate limiter."),
        ]},
        {"title": "Dispatch limits", "items": [
            knob("YAAS_MAX_DISPATCH_FANOUT", 4, "Max paid worker dispatches per tick."),
            knob("YAAS_TICK_DISPATCH_BUDGET", NUMERIC_KNOBS["YAAS_TICK_DISPATCH_BUDGET"], "Wall-seconds the dispatch phase may spend per tick."),
            knob("YAAS_MIN_DISPATCH_SLICE", NUMERIC_KNOBS["YAAS_MIN_DISPATCH_SLICE"], "A target needs at least this many seconds of budget or it's deferred."),
            knob("YAAS_MAX_TARGET_DISPATCH_PER_HOUR", 25, "Per-target hourly breaker: skip a target dispatched more than this in the last hour."),
        ]},
        {"title": "Spend caps", "items": [
            knob("YAAS_MAX_SPEND_1H", 40, "Dollar tripwire over a rolling hour (Claude backend)."),
            knob("YAAS_MAX_SPEND_24H", 250, "Dollar backstop over a rolling day."),
            knob("YAAS_MAX_DISPATCH_6H", 250, "Dispatch-count cap over 6h (covers Codex/Cursor, which report no cost)."),
        ]},
        {"title": "Promotion & backoff", "items": [
            knob("YAAS_UNACKED_PROMOTE", 3, "Dispatches with no progress before a watch starts backing off (5m doubling to 24h; it never stops retrying)."),
            knob("YAAS_CHECKER_ERROR_PROMOTE", 6, "Consecutive checker errors before a watch is held as misconfig."),
        ]},
        {"title": "Timing", "items": [
            knob("YAAS_STALE_REPLY_HOURS", 24, "Replies older than this are drafted, not sent (stale-reply guard)."),
            knob("YAAS_RETIRE_DEFAULT_DAYS", 14, "Default age at which a stale slack_thread watch is retired."),
            knob("YAAS_RETIRE_EPHEMERAL_HOURS", 168, "Lifetime of a watch marked ephemeral (a DM reply-catcher). Unmarked watches never expire."),
        ]},
        {"title": "Backend", "items": [
            knob("YAAS_AGENT", "codex", "Which agent backend runs the worker dispatch (claude / codex / cursor)."),
        ]},
    ]
    return {"orchestrator": "tick.py", "worker_timeout_sec": 1800, "groups": groups}


# ── Dashboard payload builder ─────────────────────────────────────────────────
# NOTE: the payload must be deterministic between state changes — the ETag/304
# path in the handler hashes the serialized body, so any always-changing field
# (e.g. a "generated_at" timestamp) would silently defeat conditional polling.
# live_run is exempt from that determinism goal by nature (it mirrors a log
# file that grows while a worker runs) — that's a genuine state change, so a
# non-304 response on every poll during a live run is correct, not a bug.

_BRIEF_TYPES = ("morning", "evening", "weekly", "monthly")


def build_briefs(limit: int = 30) -> list:
    """Return newest canonical briefings with their full Markdown content."""
    briefs = []
    briefs_dir = STATE_DIR / "briefs"
    if not briefs_dir.exists():
        return briefs

    candidates = []
    for brief_path in briefs_dir.glob("*.md"):
        try:
            file_stat = brief_path.stat()
        except OSError:
            continue
        created_at = getattr(file_stat, "st_birthtime", file_stat.st_mtime)
        candidates.append((created_at, file_stat.st_mtime_ns, brief_path.name,
                           brief_path, file_stat))

    newest_first = sorted(candidates, key=lambda item: item[:3], reverse=True)
    for created_at, _, _, brief_path, file_stat in newest_first:
        if len(briefs) >= limit:
            break
        try:
            markdown = brief_path.read_text()
        except OSError:
            continue
        words = set(re.findall(r"[a-z0-9]+", brief_path.stem.lower()))
        brief_type = next((kind for kind in _BRIEF_TYPES if kind in words), "brief")
        title = next(
            (line[2:].strip() for line in markdown.splitlines() if line.startswith("# ")),
            brief_path.name,
        )
        # macOS exposes birth time; other platforms fall back to mtime. The filename is
        # deliberately not a schema, so `at` always comes from filesystem metadata.
        at = datetime.fromtimestamp(created_at, timezone.utc).astimezone()
        briefs.append({
            "file": brief_path.name,
            "type": brief_type,
            "title": title,
            "markdown": markdown,
            "at": at.isoformat(),
            "ts": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(),
        })
    return briefs

def build_dashboard(include_briefs: bool = False) -> dict:
    """The full quest-state snapshot.

    `include_briefs` is OFF by default and the default is the one that matters:
    build_control() calls this on every 2s dashboard poll, and briefings are a
    read-on-demand surface served by /api/briefs, so building them here means
    re-reading every file in state/briefs/ (~114ms and 75KB of JSON on a
    150-file archive) only for build_control() to drop the key. Only
    /api/dashboard, whose payload contract still carries them, opts in.
    """
    active_dir = STATE_DIR / "quests" / "active"
    quests         = []
    recent_activity = []

    for quest_dir in sorted(active_dir.iterdir()) if active_dir.exists() else []:
        if not quest_dir.is_dir():
            continue
        try:
            meta = json.loads((quest_dir / "meta.json").read_text())
        except Exception:
            continue
        last_action  = None
        last_blocked = None
        # True once ANY event other than `created` exists, i.e. a worker has actually
        # picked this quest up. Distinct from last_action, which skips note/blocked and
        # so cannot tell "never ran" from "ran but only left a note".
        has_run      = False
        last_seen_ts = None   # newest non-blocked event of ANY type (incl. notes):
                              # a later note means the worker recovered after a block
        timeline_path = quest_dir / "timeline.ndjson"
        if timeline_path.exists():
            lines = [l for l in timeline_path.read_text().splitlines() if l.strip()]
            for raw in reversed(lines[-20:]):
                try:
                    e = json.loads(raw)
                    ev = e.get("event", "")
                    if ev and ev != "created":
                        has_run = True
                    if ev == "blocked" and last_blocked is None:
                        last_blocked = {"ts": e.get("ts"), "reason": (e.get("reason") or e.get("note") or "")[:80]}
                    if ev != "blocked" and last_seen_ts is None:
                        last_seen_ts = e.get("ts")
                    # Surface the last meaningful action — skip blocked/note/created noise
                    if last_action is None and ev not in ("created", "blocked", "note"):
                        last_action = {
                            "ts":    e.get("ts"),
                            "event": ev,
                            "note":  (e.get("note") or "")[:80],
                        }
                    if last_action and last_blocked and last_seen_ts:
                        break
                except Exception:
                    continue
            _SKIP = {"created", "blocked", "note"}
            count = 0
            for raw in reversed(lines[-40:]):
                if count >= 5:
                    break
                try:
                    e = json.loads(raw)
                    if e.get("event") not in _SKIP:
                        recent_activity.append({
                            "ts":          e.get("ts"),
                            "quest_id":    quest_dir.name,
                            "quest_title": meta.get("title", quest_dir.name),
                            "event":       e.get("event"),
                            "note":        (e.get("note") or "")[:80],
                        })
                        count += 1
                except Exception:
                    continue

        quests.append({
            "id":            quest_dir.name,
            "title":         meta.get("title", quest_dir.name),
            "status":        meta.get("status", "active"),
            "priority":      meta.get("priority", "normal"),
            "allow_send":    meta.get("allow_send", False),
            "last_action":   last_action,
            "last_blocked":  last_blocked,
            "last_seen_ts":  last_seen_ts,
            "has_run":       has_run,
            "requires_initial_run": meta.get("requires_initial_run") is True,
            "backoff_count": 0,     # filled in below
            "backoff_watches": [],  # filled in below
            "ratelimited": False,   # filled in below (transient, from run-log)
            "ratelimited_count": 0,
            "ratelimited_watches": [],
        })

    # Annotate quests with their backing-off watches from unacked-counts.json.
    #
    # These used to be labelled "misconfig", which was the wrong word twice over: a watch past
    # the promote threshold is not necessarily misconfigured (a network outage gets you there
    # just as fast), and the label implied a dead end when the watch is in fact still retrying.
    # Carry the detail — how long the wait is now and the worker's last error — because "backing
    # off" alone tells a reader nothing they can act on.
    unacked_path = STATE_DIR / "triage" / "unacked-counts.json"
    if unacked_path.exists():
        try:
            promote = int(os.environ.get("YAAS_UNACKED_PROMOTE", "3"))
            uc = json.loads(unacked_path.read_text())
            backoff_by_quest: dict[str, list] = {}
            for key, entry in uc.items():
                qid, _, wid = key.partition("|")
                if not isinstance(entry, dict) or entry.get("count", 0) < promote:
                    continue
                if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                        and str(entry.get("type", "")).startswith("slack_")):
                    continue
                backoff_by_quest.setdefault(qid, []).append({
                    "watch_id":      wid,
                    "type":          entry.get("type", ""),
                    "count":         entry.get("count", 0),
                    "backoff_sec":   entry.get("backoff_sec", 0),
                    "next_retry_ts": entry.get("next_retry_ts", "0"),
                    "last_error":    entry.get("last_error", ""),
                    "last_status":   entry.get("last_status", ""),
                })
            for q in quests:
                wl = backoff_by_quest.get(q["id"], [])
                q["backoff_watches"] = wl
                q["backoff_count"]   = len(wl)
        except Exception:
            pass

    # …and with CHECKER backoff, the other, entirely separate source.
    #
    # The header's "checker backoff" tile counts both kinds (tick.py counts every BACKOFF
    # verdict), but only the unacked kind above was attributable to a quest — so a watch
    # backing off because its CHECKER keeps erroring showed up as a number at the top of the
    # dashboard with no way to find out which quest owned it. That is the more common of the
    # two in practice: an expired credential or a revoked permission lands here, not above.
    #
    # checker-health.json is keyed by watch_id alone, so the owning quest has to be recovered
    # by scanning each quest's watch.json for that id.
    health_path = STATE_DIR / "triage" / "checker-health.json"
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text())
            owner, wtype_of = {}, {}
            for q in quests:
                try:
                    wj = json.loads((active_dir / q["id"] / "watch.json").read_text())
                except Exception:
                    continue
                for w in wj.get("watches", []):
                    if isinstance(w, dict) and w.get("watch_id"):
                        owner[w["watch_id"]] = q["id"]
                        wtype_of[w["watch_id"]] = w.get("type", "")
            now = time.time()
            extra: dict[str, list] = {}
            for wid, rec in (health or {}).items():
                if not isinstance(rec, dict):
                    continue
                qid = owner.get(wid)
                if not qid:
                    continue  # orphan: the watch or its quest is gone
                if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                        and str(wtype_of.get(wid, "")).startswith("slack_")):
                    continue
                if tick_check.is_due(rec, now):
                    remaining = 0
                else:
                    remaining = float(rec.get("next_retry_ts") or 0) - now
                if remaining <= 0:
                    continue  # not currently waiting
                extra.setdefault(qid, []).append({
                    "watch_id":      wid,
                    "type":          wtype_of.get(wid, ""),
                    "count":         rec.get("consecutive_errors", 0),
                    "backoff_sec":   int(remaining),
                    "next_retry_ts": rec.get("next_retry_ts", "0"),
                    "last_error":    rec.get("last_error", ""),
                    "last_status":   "checker error",
                    "source":        "checker",
                })
            for q in quests:
                wl = extra.get(q["id"], [])
                if wl:
                    q["backoff_watches"] = list(q.get("backoff_watches") or []) + wl
                    q["backoff_count"]   = len(q["backoff_watches"])
        except Exception:
            pass

    # Annotate quests with recent rate-limiting (transient; from the run-log, not a state file).
    rl_by_quest: dict[str, dict] = {}
    for e in _recent_runlog_events("gate_watch_ratelimited", RATELIMIT_WINDOW_SEC):
        if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                and str(e.get("type", "")).startswith("slack_")):
            continue
        qid = e.get("quest")
        if qid and e.get("watch_id"):
            rl_by_quest.setdefault(qid, {})[e["watch_id"]] = {
                "watch_id": e["watch_id"],
                "type": e.get("type", "unknown"),
                "reason": e.get("reason", ""),
            }
    for q in quests:
        wl = list(rl_by_quest.get(q["id"], {}).values())
        q["ratelimited"] = bool(wl)
        q["ratelimited_count"] = len(wl)
        q["ratelimited_watches"] = wl

    recent_activity.sort(key=lambda x: x.get("ts") or "", reverse=True)
    recent_activity = recent_activity[:20]

    triage_state = {}
    triage_path  = STATE_DIR / "triage" / "last-run.json"
    if triage_path.exists():
        try:
            triage_state = json.loads(triage_path.read_text())
        except Exception:
            pass

    pending_review = []
    if APPROVALS_FILE.exists():
        try:
            data = _read_approvals()
            pending_review = [i for i in data.get("items", [])
                              if i.get("status") not in TERMINAL_APPROVAL_STATUSES
                              and i.get("action_type") != "manual_instruction"]
        except Exception:
            pass

    return {
        "workspace":      build_workspace_identity(),
        "triage":         triage_state,
        "live_run":       build_live_run(),
        "quests":         quests,
        "recent_activity": recent_activity,
        "pending_review": pending_review,
        "briefs":         build_briefs() if include_briefs else [],
        "config":         build_config(),
    }


# ── Messages tab builder ─────────────────────────────────────────────────────
# Deterministic between state changes (no timestamps) so the ETag/304 poll path
# in the handler holds. Left pane = the review queue split into message-shaped
# items (needs_you) and everything else (other_actions). Right rail =
# recent_messages: verbatim-only sends across all active quests, plus pending
# message-type approvals shown as awaiting-send drafts.

def build_messages() -> dict:
    active_dir = STATE_DIR / "quests" / "active"

    appr_data  = _read_approvals() if APPROVALS_FILE.exists() else {"items": []}
    appr_by_id = _approvals_index(appr_data)
    pending = [i for i in appr_data.get("items", [])
               if i.get("status") not in TERMINAL_APPROVAL_STATUSES]
    needs_you, other_actions = [], []
    queued_items = []
    for i in pending:
        status = i.get("status")
        card = _approval_card(i)
        stalled = card.get("stalled", False)
        if status in ("pending_review", "needs_reply") or stalled:
            (needs_you if i.get("action_type", "slack_message") in _MSG_ACTION_TYPES
             else other_actions).append(card)
            continue
        if status in ("reviewed", "executing"):
            # Keep the same rich card shape as pending review. The control UI
            # must show destination, risk, revision context and legal actions
            # after an operator approves an item, not make it disappear into a
            # thinner "queued" representation until the worker picks it up.
            queued_items.append(card)
            continue
        # Fail visible when the state machine gains a new non-terminal status.
        # Unknown work may not have a legal dashboard action yet, but silently
        # dropping it from both live lists is worse than surfacing it under
        # Attention with its raw status and context.
        other_actions.append(card)

    # Recent activity: all timeline events (not just outbound) across active quests.
    # Skips "created" (no display value) and empty note/info events.
    _SKIP_EVENTS = {"created"}
    recent = []
    for quest_dir in sorted(active_dir.iterdir()) if active_dir.exists() else []:
        if not quest_dir.is_dir():
            continue
        try:
            meta = json.loads((quest_dir / "meta.json").read_text())
        except Exception:
            continue
        title = meta.get("title", quest_dir.name)
        tp = quest_dir / "timeline.ndjson"
        if not tp.exists():
            continue
        try:
            lines = [l for l in tp.read_text().splitlines() if l.strip()]
        except Exception:
            continue
        for raw in reversed(lines[-60:]):
            try:
                e = json.loads(raw)
            except Exception:
                continue
            ev = e.get("event")
            if not ev or ev in _SKIP_EVENTS:
                continue
            # For outbound events, try to recover message body from approval index
            body = e.get("message_text") or ""
            if ev in _OUTBOUND_EVENTS and not body:
                appr_id = e.get("approval_id")
                if appr_id and appr_id in appr_by_id:
                    body = appr_by_id[appr_id]
            note = e.get("note") or ""
            # Skip non-outbound events that carry no displayable content
            if ev not in _OUTBOUND_EVENTS and not body and not note:
                continue
            channel_id = _norm_channel(e)
            rec = {
                "ts":           e.get("ts"),
                "event":        ev,
                "kind":         _OUTBOUND_EVENTS.get(ev),
                "channel_id":   channel_id,
                "thread_ts":    e.get("thread_ts"),
                "message_text": _clip(body, 2000) if body else "",
                "note":         _clip(note, 300),
                **_link_fields(e),
                "action_type":  e.get("action_type"),
                "awaiting_send": False,
                "quest_id":     quest_dir.name,
                "quest_title":  title,
            }
            recent.append(rec)

    for i in pending:  # awaiting-send drafts (message types only)
        if i.get("action_type", "slack_message") in _MSG_ACTION_TYPES:
            recent.append(_approval_draft_record(i))

    recent.sort(key=lambda r: r.get("ts") or "", reverse=True)
    recent = recent[:40]

    return {
        "needs_you":       needs_you,
        "other_actions":   other_actions,
        "recent_activity": recent,
        "queued_items":    queued_items,
    }


# ── Quest control snapshot ───────────────────────────────────────────────────
# This is intentionally additive. V1 continues to consume its focused payloads,
# while v2 receives a stable view model that can gain richer worker-provided
# provenance later without teaching the browser about raw timeline variants.

_EVENT_ACTIONS = {
    "message_sent": "sent message",
    "reply_sent": "sent reply",
    "dm_sent": "sent direct message",
    "email_replied": "replied to email",
    "draft_posted": "created draft",
    "executed": "executed approved action",
    "info_received": "received update",
    "blocked": "hit blocker",
    "status_change": "changed quest status",
}

def _control_activity(rec: dict) -> dict:
    event = rec.get("event") or "note"
    kind = rec.get("kind")
    if kind in ("sent", "draft", "email") or event in _OUTBOUND_EVENTS:
        actor = "agent"
    elif event == "info_received":
        actor = "external"
    else:
        actor = "system"
    risk = "external_send" if event in _SENT_EVENTS or kind in ("sent", "email") else "normal"
    target = rec.get("channel_id") or rec.get("quest_title") or rec.get("quest_id") or ""
    return {
        "id": f"{rec.get('quest_id', '')}:{rec.get('ts', '')}:{event}",
        "timestamp": rec.get("ts"),
        "actor": actor,
        "event": event,
        "action": _EVENT_ACTIONS.get(event, event.replace("_", " ")),
        "outcome": "awaiting review" if rec.get("awaiting_send") else "recorded",
        "risk": risk,
        "target": target,
        "detail": rec.get("message_text") or rec.get("note") or "",
        "quest_id": rec.get("quest_id"),
        "quest_title": rec.get("quest_title"),
        "link_url": rec.get("link_url") or rec.get("slack_url"),
        "link_kind": rec.get("link_kind"),
    }

def _retry_wait_text(next_retry_ts, now: float | None = None) -> str:
    """Return a short, decision-oriented description of a scheduled retry."""
    try:
        remaining = max(0, float(next_retry_ts) - (time.time() if now is None else now))
    except (TypeError, ValueError):
        return "soon"
    if remaining < 60:
        return "in under a minute"
    if remaining < 3600:
        return f"in about {int(remaining // 60)}m"
    if remaining < 86400:
        return f"in about {int(remaining // 3600)}h"
    return f"in about {int(remaining // 86400)}d"


def _quest_health_detail(quest: dict, now: float | None = None) -> str:
    """Explain whether a quest's watch state needs action or will self-recover."""
    parts = []
    blocked = quest.get("last_blocked") or {}
    if blocked:
        parts.append(f"Blocked: {blocked.get('reason') or 'no reason was recorded'}.")

    backoffs = list(quest.get("backoff_watches") or [])
    if backoffs:
        watch = backoffs[0]
        watch_type = watch.get("type") or "unknown watch"
        reason = watch.get("last_error") or watch.get("last_status") or "no progress"
        retry = _retry_wait_text(watch.get("next_retry_ts"), now)
        suffix = f" {len(backoffs) - 1} other watch(es) are also backing off." if len(backoffs) > 1 else ""
        parts.append(
            f"{watch_type} is backing off after {reason}. "
            f"Automatic retries continue; next attempt {retry}.{suffix}"
        )

    ratelimited = list(quest.get("ratelimited_watches") or [])
    if ratelimited:
        watch = ratelimited[0]
        watch_type = watch.get("type") or "watch"
        reason = watch.get("reason") or "the upstream service limited requests"
        parts.append(f"{watch_type} was rate limited ({reason}). The loop will retry on a later tick.")

    if not backoffs and not ratelimited:
        parts.append("No automatic retry is scheduled, so this needs your intervention.")
    return " ".join(parts)


def build_control() -> dict:
    dashboard = build_dashboard()
    messages = build_messages()
    attention = []

    for card in messages["needs_you"]:
        attention.append({
            "id": f"approval:{card.get('id')}", "kind": "review",
            "priority": "high", "label": "Review agent action",
            "detail": (card.get("processing_error") or card.get("risk_reason")
                       or card.get("message_text", "")),
            "quest_id": card.get("quest_id"), "quest_title": card.get("quest_title"),
            "approval": card,
        })
    for card in messages["other_actions"]:
        attention.append({
            "id": f"approval:{card.get('id')}", "kind": "review",
            "priority": "high", "label": "Review agent action",
            "detail": (card.get("processing_error") or card.get("risk_reason")
                       or card.get("message_text", "")),
            "quest_id": card.get("quest_id"), "quest_title": card.get("quest_title"),
            "approval": card,
        })
    for quest in dashboard["quests"]:
        if quest.get("status") == "blocked" or quest.get("backoff_count") or quest.get("ratelimited"):
            attention.append({
                "id": f"quest:{quest['id']}:health", "kind": "quest_health",
                "priority": "high", "label": "Quest needs attention",
                "detail": _quest_health_detail(quest),
                "quest_id": quest["id"], "quest_title": quest["title"],
            })

    return {
        "api_version": 1,
        "build": build_build_info(),
        "workspace": dashboard["workspace"],
        "capabilities": {
            "control_snapshot": True,
            "normalized_activity": True,
            "structured_live_progress": False,
            "activity_pagination": False,
        },
        "triage": dashboard["triage"],
        "live": dashboard["live_run"],
        "quests": dashboard["quests"],
        "attention": attention,
        "queued": messages["queued_items"],
        "activity": [_control_activity(rec) for rec in messages["recent_activity"]],
        "history": build_history(),
        "reaction_emojis": build_reaction_emojis(),
    }


def build_reaction_emojis() -> dict:
    """Expose only the resolved emoji names, never the surrounding environment."""
    from reaction_config import EMOJI_SETTINGS, load_reaction_emojis

    env = {}
    for var, default in EMOJI_SETTINGS.values():
        env[var] = _dotenv(var, default)
        canonical = var.replace("YAAS_", "SIDEQUESTOR_", 1)
        override = _dotenv(canonical, "")
        if override:
            env[canonical] = override
    try:
        return {"roles": load_reaction_emojis(env), "error": None}
    except ValueError as exc:
        return {"roles": {}, "error": str(exc)}


def build_build_info() -> dict:
    """Return package build identity without requiring package imports."""
    try:
        from sidequestor.build_info import build_info
        info = build_info()
    except Exception:
        info = {}
    commit_full = os.environ.get("SIDEQUESTOR_COMMIT") or info.get("commit_full", "")
    return {
        "version": os.environ.get("SIDEQUESTOR_VERSION") or info.get("version", "unknown"),
        "commit": commit_full[:7],
        "commit_full": commit_full,
        "ref": os.environ.get("SIDEQUESTOR_REF") or info.get("ref", ""),
        "source": info.get("source", "unknown"),
        "engine": info.get("engine", "unknown"),
    }


# ── Quest detail builder (lazy-fetched by the drawer) ────────────────────────

_TIMELINE_KEYS = ("ts", "event", "note", "reason", "permalink", "by", "channel",
                  "channel_id", "thread_ts", "response_ts", "msg_ts", "message_ts",
                  "approval_id", "draft_id", "action_type", "to", "subject",
                  "message_text")
_TIMELINE_CAP  = 500

def build_quest_detail(quest_id: str) -> dict | None:
    open_watch_types, registry_error = _open_watch_types()
    active_dir = STATE_DIR / "quests" / "active"
    quest_dir  = active_dir / quest_id
    # Prevent path traversal outside the active quests dir
    try:
        quest_dir.resolve().relative_to(active_dir.resolve())
    except ValueError:
        return None
    if not quest_dir.is_dir():
        return None

    meta = {}
    try:
        meta = json.loads((quest_dir / "meta.json").read_text())
    except Exception:
        pass

    context_md = ""
    try:
        context_md = (quest_dir / "context.md").read_text()
    except Exception:
        pass

    watches = []
    try:
        w = json.loads((quest_dir / "watch.json").read_text())
        if isinstance(w, dict) and isinstance(w.get("watches"), list):
            watches = [e for e in w["watches"] if isinstance(e, dict)]
    except Exception:
        pass

    timeline, total, lines = [], 0, []
    timeline_path = quest_dir / "timeline.ndjson"
    if timeline_path.exists():
        try:
            lines = [l for l in timeline_path.read_text().splitlines() if l.strip()]
            total = len(lines)
            for raw in reversed(lines[-_TIMELINE_CAP:]):
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                entry = {k: e[k] for k in _TIMELINE_KEYS if k in e}
                # Resolve the link from the FULL event (ids live outside
                # _TIMELINE_KEYS) so Jira/GitHub/Gmail replies link out too.
                entry.update(_link_fields(e))
                for k in ("note", "reason"):
                    if isinstance(entry.get(k), str) and len(entry[k]) > 300:
                        entry[k] = entry[k][:300] + "…"
                if isinstance(entry.get("message_text"), str) and len(entry["message_text"]) > 2000:
                    entry["message_text"] = entry["message_text"][:2000] + "…"
                timeline.append(entry)
        except Exception:
            pass

    appr_data  = _read_approvals() if APPROVALS_FILE.exists() else {"items": []}

    # ── Per-quest open items: the durable "what is THIS quest still waiting on"
    #    summary. Open threads come from watch.json (each watch = a thread/inbox
    #    the bot is actively tracking, with its own reason + link); blocked +
    #    review + commitments mirror the aggregate build_open_items categories,
    #    scoped to this quest. Small per quest, so listing every watch here is
    #    the right granularity (unlike the noisy 100+ aggregate view).
    def _as_float(x):
        try:    return float(x)
        except (TypeError, ValueError): return 0.0

    def _expires_ts(e):
        """When an ephemeral watch dies, or None if it never does / isn't datable yet.

        Mirrors housekeep.retire_ephemeral rather than importing it: this server is
        read-only over live state and must not pull in the module that DELETES watches.
        The window is read the same way housekeep reads it, so the two agree.
        """
        if e.get("ephemeral") is not True:
            return None
        created = _as_float(e.get("created_ts"))
        if created <= 0 or created != created:      # unstamped, or NaN
            return None
        try:
            hours = int(str(_dotenv("YAAS_RETIRE_EPHEMERAL_HOURS", "168")).strip() or 168)
        except ValueError:
            hours = 168
        return created + hours * 3600 if hours > 0 else None

    open_threads, scheduled = [], []
    for e in watches:
        wtype = e.get("type")
        if wtype in open_watch_types:
            ch, tts = e.get("channel_id"), e.get("thread_ts")
            url, kind = _watch_link(e, wtype, ch, tts)
            open_threads.append({
                "type":       wtype,
                "reason":     _clip(e.get("reason", ""), 2000),
                "read_only":  e.get("watch_mode") == "read_only",
                "slack_url":  url if kind == "slack" else None,
                "link_url":   url,
                "link_kind":  kind,
                "query":      e.get("query") if wtype == "email" else None,
                "last_checked_ts": e.get("last_checked_ts"),
                # A reply-catcher and a standing subscription look identical in this list
                # otherwise, which is how two of them ran for 3 and 12 days past their
                # purpose without anyone noticing. expires_ts is None for an unmarked
                # (permanent) watch, and None while created_ts is still unstamped —
                # housekeep backfills that on its next run, so "soon" rather than "never".
                "ephemeral":  e.get("ephemeral") is True,
                "expires_ts": _expires_ts(e),
            })
        elif wtype == "schedule":
            # A scheduled follow-up = something the bot will do / GM promised at
            # a future time. These are the durable "promised, not done" items.
            scheduled.append({
                "reason":       _clip(e.get("reason", ""), 2000),
                "next_fire_ts": e.get("next_fire_ts"),
                "cron":         e.get("cron"),
            })
    # watch.json only ever appends, so a busy quest accumulates many long-since-
    # answered threads. Surface the most recently touched ones (they are the ones
    # still likely live) and report how many older ones are hidden.
    open_threads.sort(key=lambda t: _as_float(t.get("last_checked_ts")), reverse=True)
    threads_total = len(open_threads)
    open_threads = open_threads[:15]
    scheduled.sort(key=lambda s: _as_float(s.get("next_fire_ts")))

    # Blocked: last blocked event with nothing logged after it (matches the
    # aggregate builder's recovery semantics).
    blocked_now = None
    li_block = li_other = -1
    for idx, raw in enumerate(lines):
        try:
            ev = json.loads(raw).get("event")
        except Exception:
            continue
        if ev == "blocked": li_block = idx
        else:              li_other = idx
    if li_block >= 0 and li_block > li_other:
        try:
            be = json.loads(lines[li_block])
            blocked_now = {"ts": be.get("ts"),
                           "reason": _clip(be.get("reason") or be.get("note") or "", 2000)}
        except Exception:
            blocked_now = None

    review = [
        {"id": i.get("id"), "status": i.get("status"),
         "action_type": i.get("action_type", "slack_message"),
         "message_text": _clip(i.get("message_text", ""), 2000)}
        for i in appr_data.get("items", [])
        if i.get("quest_id") == quest_id
        and i.get("status") not in TERMINAL_APPROVAL_STATUSES
        and i.get("action_type") != "manual_instruction"
    ]

    commitments = []
    if COMMITMENTS_FILE.exists():
        try:
            cdata = json.loads(COMMITMENTS_FILE.read_text())
            commitments = [c for c in (cdata.get("items", []) if isinstance(cdata, dict) else [])
                           if c.get("quest_id") == quest_id]
        except Exception:
            pass

    # Backing-off watches: dispatched N times with no progress, now retried on a decaying
    # schedule (never parked, never abandoned). Carries the wait and the worker's last error.
    backoff_watches = []
    unacked_path = STATE_DIR / "triage" / "unacked-counts.json"
    if unacked_path.exists():
        try:
            uc = json.loads(unacked_path.read_text())
            promote = int(os.environ.get("YAAS_UNACKED_PROMOTE", "3"))
            for key, entry in uc.items():
                qid, _, wid = key.partition("|")
                if qid == quest_id and entry.get("count", 0) >= promote:
                    if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                            and str(entry.get("type", "")).startswith("slack_")):
                        continue
                    backoff_watches.append({
                        "watch_id":      wid,
                        "type":          entry.get("type", "unknown"),
                        "count":         entry.get("count"),
                        "last_status":   entry.get("last_status"),
                        "last_utc":      entry.get("last_utc"),
                        "backoff_sec":   entry.get("backoff_sec", 0),
                        "next_retry_ts": entry.get("next_retry_ts", "0"),
                        "last_error":    entry.get("last_error", ""),
                        "source":        "dispatch",
                    })
        except Exception:
            pass

    # The other backoff source: the watch's CHECKER keeps erroring (expired credential, revoked
    # permission, changed upstream shape). Keyed by watch_id alone, so it is matched against this
    # quest's own watch.json. Without this the quest page showed nothing while the header counted
    # it, which is how a 27-error github_pr watch stayed anonymous for a day.
    try:
        health = json.loads((STATE_DIR / "triage" / "checker-health.json").read_text())
        wj = json.loads((STATE_DIR / "quests" / "active" / quest_id / "watch.json").read_text())
        mine = {w.get("watch_id"): w.get("type", "") for w in wj.get("watches", [])
                if isinstance(w, dict)}
        now = time.time()
        for wid, rec in (health or {}).items():
            if wid not in mine or not isinstance(rec, dict):
                continue
            if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                    and str(mine.get(wid, "")).startswith("slack_")):
                continue
            if tick_check.is_due(rec, now):
                remaining = 0
            else:
                remaining = float(rec.get("next_retry_ts") or 0) - now
            if remaining <= 0:
                continue
            backoff_watches.append({
                "watch_id":      wid,
                "type":          mine.get(wid, "unknown"),
                "count":         rec.get("consecutive_errors", 0),
                "last_status":   "checker error",
                "last_utc":      rec.get("last_error_utc"),
                "backoff_sec":   int(remaining),
                "next_retry_ts": rec.get("next_retry_ts", "0"),
                "last_error":    rec.get("last_error", ""),
                "source":        "checker",
            })
    except Exception:
        pass

    # Rate-limited watches: transient, from recent run-log events (not a state file).
    ratelimited_watches = []
    _rl_seen = set()
    for e in _recent_runlog_events("gate_watch_ratelimited", RATELIMIT_WINDOW_SEC):
        if (_dotenv("YAAS_SLACK_CHECKERS_ENABLED", "1") == "0"
                and str(e.get("type", "")).startswith("slack_")):
            continue
        if e.get("quest") == quest_id and e.get("watch_id") and e["watch_id"] not in _rl_seen:
            _rl_seen.add(e["watch_id"])
            ratelimited_watches.append({
                "watch_id":  e.get("watch_id"),
                "type":      e.get("type", "unknown"),
                "last_utc":  e.get("ts"),
                "reason":    e.get("reason"),
            })

    open_items = {
        "blocked":           blocked_now,
        "backoff_watches": backoff_watches,
        "ratelimited_watches": ratelimited_watches,
        "threads":           open_threads,
        "threads_total":     threads_total,
        "scheduled":         scheduled,
        "review":            review,
        "commitments":       commitments,
        "registry_error":    registry_error,
    }

    return {
        "id":             quest_id,
        "meta":           meta,
        "context_md":     context_md,
        "watches":        watches,
        "timeline":       timeline,
        "timeline_total": total,
        "open_items":     open_items,
    }


# ── History builder (approvals audit trail + dispatch log) ────────────────────

_LIFECYCLE_TS = ("created_at", "reviewed_at", "executing_at", "sent_at", "cancelled_at")
_RUNLOG_TAIL_BYTES = 256 * 1024

def build_history() -> dict:
    approvals = []
    if APPROVALS_FILE.exists():
        try:
            data = _read_approvals()
            approvals = [
                {**i, **_approval_card(i)}
                for i in data.get("items", [])
                if isinstance(i, dict) and i.get("status") != "pending_review"
            ]
            approvals.sort(
                key=lambda i: max((i.get(k) or "" for k in _LIFECYCLE_TS), default=""),
                reverse=True,
            )
            approvals = approvals[:50]
        except Exception:
            approvals = []

    runs = []
    runlog = STATE_DIR / "run-log.ndjson"
    if runlog.exists():
        try:
            size = runlog.stat().st_size
            with open(runlog, "rb") as f:
                if size > _RUNLOG_TAIL_BYTES:
                    f.seek(size - _RUNLOG_TAIL_BYTES)
                    f.readline()  # drop partial first line
                raw_lines = f.read().decode(errors="replace").splitlines()
            for raw in reversed(raw_lines):
                if len(runs) >= 40:
                    break
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                ev = e.get("event", "")
                if ev == "gate_dispatch_tokens":
                    runs.append({
                        "ts": e.get("ts"), "kind": "dispatch",
                        "targets":  e.get("targets"),
                        "cost_usd": e.get("cost_usd"),
                        "wall_sec": e.get("wall_sec"),
                        "ok":       e.get("exit") == 0,
                    })
                elif ev == "gate_dispatch_failure":
                    runs.append({
                        "ts": e.get("ts"), "kind": "failure",
                        "targets": e.get("targets"), "exit_code": e.get("exit_code"),
                    })
                elif ev == "gate_skip_locked":
                    runs.append({"ts": e.get("ts"), "kind": "skip_locked",
                                 "holder_pid": e.get("holder_pid")})
                elif ev == "worker_stopped":
                    runs.append({"ts": e.get("ts"), "kind": "worker_stopped",
                                 "reason": e.get("reason"), "status": e.get("status")})
        except Exception:
            runs = []

    return {"approvals": approvals, "runs": runs}


# ── HTTP handler ──────────────────────────────────────────────────────────────

def _ensure_approval_watch(item: dict) -> bool:
    """Re-arm the `approval` watch for a non-terminal item.

    The watch is one-shot: a worker consumes and acks it once the item is
    acted on. Any transition that puts the item back in play (undo, reclaim,
    or a second review after an undo) therefore leaves it with no watch at
    all, so triage cannot see it and it sits at `reviewed` forever while the
    dashboard cheerfully reports it queued. add-watch.py dedups on
    approval_id, so calling this on every transition is free when a watch is
    already there.

    Never raises: losing the transition because re-arming failed would be
    worse than an unwatched item, which the stranded-watch sweep still
    catches. Returns whether a watch is known to be in place.
    """
    approval_id = item.get("id") or ""
    if item.get("status") in TERMINAL_APPROVAL_STATUSES or not approval_id:
        return False
    try:
        cp = subprocess.run(
            ["python3", str(SCRIPT_DIR.parent / "ledger" / "approval-helper.py"),
             "arm", approval_id],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=20,
        )
    except Exception as exc:
        print(f"warn:rearm_watch_failed:{approval_id}:{exc}", file=sys.stderr, flush=True)
        return False
    if cp.returncode == 0:
        return True
    reason = (cp.stderr or cp.stdout or "add-watch failed").strip()[:200]
    print(f"warn:rearm_watch_failed:{approval_id}:{reason}", file=sys.stderr, flush=True)
    return False


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default stdout access log

    # ── auth / header helpers ────────────────────────────────────────────────
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _authed(self) -> bool:
        tok = ""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                tok = v
                break
        # constant-time compare so a wrong cookie can't be timed out char-by-char
        return hmac.compare_digest(tok, SESSION_TOKEN)

    def _sec_headers(self, csp: str):
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._sec_headers(_CSP_JSON)
        self.end_headers()
        self.wfile.write(body)

    def _send_json_etag(self, builder):
        """Serve builder()'s JSON with an md5 ETag; 304 on If-None-Match hit.
        builder must return a deterministic payload between state changes."""
        try:
            body = json.dumps(builder()).encode()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self._sec_headers(_CSP_JSON)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self._sec_headers(_CSP_JSON)
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self, dashboard_path: Path = DASHBOARD_HTML):
        """Serve the HTML shell: inject a per-response CSP nonce into the single
        inline <script>, and (re)issue the session cookie. This is the only
        unauthenticated route — it hands the browser the cookie that every other
        route then requires."""
        try:
            html = dashboard_path.read_text()
        except FileNotFoundError:
            self.send_error(404)
            return
        nonce = secrets.token_urlsafe(16)
        html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/",
        )
        self._sec_headers(_csp_html(nonce))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str = "text/html"):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if not self._host_ok():
            self.send_error(403, "forbidden host")
            return

        # Bootstrap route: unauthenticated and responsible for issuing the
        # session cookie used by every state and control endpoint.
        if path in ("/", "/index.html", "/dashboard.html"):
            self._serve_dashboard()
            return

        if path == "/yaas-triage/assets/sidequestor-mark.png":
            try:
                body = DASHBOARD_LOGO.read_bytes()
            except OSError:
                self.send_error(404, "dashboard logo not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Everything else (state + APIs) requires the cookie.
        if not self._authed():
            self.send_error(403, "forbidden")
            return

        if path == "/api/dashboard":
            self._send_json_etag(lambda: build_dashboard(include_briefs=True))
            return

        if path == "/api/control":
            self._send_json_etag(build_control)
            return

        if path == "/api/briefs":
            self._send_json_etag(lambda: {"briefs": build_briefs()})
            return

        if path == "/api/messages":
            self._send_json_etag(build_messages)
            return

        if path == "/api/history":
            self._send_json_etag(build_history)
            return

        if path.startswith("/api/quest/"):
            quest_id = urllib.parse.unquote(path[len("/api/quest/"):])
            try:
                detail = build_quest_detail(quest_id)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
                return
            if detail is None:
                self._send_json({"error": "quest not found"}, 404)
            else:
                self._send_json(detail)
            return

        if path.startswith("/state/"):
            rel       = path[len("/state/"):]
            file_path = STATE_DIR / rel
            # Prevent path traversal outside state/
            try:
                file_path.resolve().relative_to(STATE_DIR.resolve())
            except ValueError:
                self.send_error(403)
                return
            self._send_file(file_path, "application/json")
            return

        self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            self.send_error(403, "forbidden host")
            return
        if not self._authed():
            self.send_error(403, "forbidden")
            return

        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json({"error": "invalid Content-Length"}, 400)
            return
        if length < 0:
            self._send_json({"error": "invalid Content-Length"}, 400)
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_json({"error": "request body too large"}, 413)
            return
        raw    = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "request body must be valid JSON"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "request body must be a JSON object"}, 400)
            return

        if path == "/api/workspace/open":
            self._handle_open_workspace()
            return

        parts = [p for p in path.strip("/").split("/") if p]
        # Expected: ["api", "<action>", "<id>"]
        if len(parts) == 3 and parts[0] == "api" and parts[1] in approval_state.HTTP_ACTIONS:
            self._handle_review(parts[2], parts[1], payload)
            return

        # Manual quest dispatch: ["api", "prompt", "<quest_id>"]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "prompt":
            self._handle_prompt(urllib.parse.unquote(parts[2]), payload)
            return

        if path == "/api/quests":
            self._handle_create_quest(payload)
            return

        self.send_error(404)

    @staticmethod
    def _approval_conflict_message(action: str) -> str:
        return {
            "undo": "only reviewed or cancelled items can be undone",
            "reclaim": "only executing items with an expired lease can be reclaimed",
            "edit": "item is no longer in reviewed state",
        }.get(action, "already reviewed, executing, executed, or cancelled")

    def _handle_open_workspace(self):
        """Open the server's own workspace, never a browser-supplied path."""
        preferred = os.environ.get("SIDEQUESTOR_IDE_APP", "Cursor").strip()
        attempts = []
        if preferred:
            attempts.append((preferred, ["open", "-a", preferred, str(REPO_ROOT)]))
        attempts.append(("default opener", ["open", str(REPO_ROOT)]))
        errors = []
        for opened_with, command in attempts:
            try:
                result = subprocess.run(
                    command,
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
                continue
            if result.returncode == 0:
                self._send_json({
                    "ok": True,
                    "path": str(REPO_ROOT),
                    "opened_with": opened_with,
                })
                return
            detail = (result.stderr or result.stdout or "open failed").strip()
            errors.append(detail[:200])
        self._send_json({
            "error": "could not open workspace in Cursor or the default application",
            "detail": "; ".join(errors),
        }, 500)

    def _handle_prompt(self, quest_id: str, payload: dict):
        """Durably queue one operator-authorized instruction for a quest."""
        instruction = (payload.get("instruction") or "").strip()
        if not instruction:
            self._send_json({"error": "instruction is required"}, 400)
            return
        if len(instruction) > 4000:
            self._send_json({"error": "instruction too long (max 4000 chars)"}, 400)
            return
        # Guard obvious bad ids here too; the helper validates the payload while
        # this endpoint owns the active-quest requirement.
        if "/" in quest_id or quest_id in ("", ".", ".."):
            self._send_json({"error": "invalid quest id"}, 400)
            return
        if not (STATE_DIR / "quests" / "active" / quest_id).is_dir():
            self._send_json({"error": "quest not found"}, 404)
            return

        quest_dir = STATE_DIR / "quests" / "active" / quest_id
        title = quest_id
        try:
            title = json.loads((quest_dir / "meta.json").read_text()).get("title", quest_id)
        except Exception:
            pass

        helper = SCRIPT_DIR.parent / "ledger" / "approval-helper.py"
        request = {
            "quest_id": quest_id,
            "quest_title": title,
            "instruction": instruction,
            "context": "Submitted directly through the quest dashboard.",
        }
        try:
            cp = subprocess.run(
                ["python3", str(helper), "enqueue-instruction", json.dumps(request)],
                capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT),
            )
        except Exception as e:
            self._send_json({"error": f"failed to queue instruction: {e}"}, 500)
            return
        try:
            result = json.loads((cp.stdout or "").strip())
        except Exception:
            result = {}
        if cp.returncode != 0 or not result.get("queued"):
            self._send_json({
                "error": "instruction could not be queued",
                "approval_id": result.get("approval_id"),
                "detail": (cp.stderr or "").strip()[:300],
            }, 500)
            return
        self._send_json({"ok": True, "queued": True, "quest_id": quest_id,
                         "approval_id": result["approval_id"]}, 202)

    def _handle_create_quest(self, payload: dict):
        """Create a real quest through the canonical scaffolder, never by hand."""
        fields = {name: payload.get(name) for name in ("prompt", "title", "priority")}
        for name, value in fields.items():
            if value is not None and not isinstance(value, str):
                self._send_json({"error": f"{name} must be a string"}, 400)
                return
        prompt = (fields["prompt"] or "").strip()
        title = (fields["title"] or "").strip()
        priority = (fields["priority"] or "normal").strip()
        if not prompt:
            self._send_json({"error": "prompt is required"}, 400)
            return
        if len(prompt) > 4000:
            self._send_json({"error": "prompt too long (max 4000 chars)"}, 400)
            return
        if len(title) > 120:
            self._send_json({"error": "title too long (max 120 chars)"}, 400)
            return
        if priority not in ("high", "normal", "low"):
            self._send_json({"error": "priority must be high, normal, or low"}, 400)
            return

        # The watch is intentionally slightly after the helper's watermark. A
        # one-shot due timestamp of "now" would be written before last_checked_ts
        # and therefore be considered already spent by the schedule checker.
        initial_run_ts = str(time.time() + QUEST_CREATE_TIMEOUT_S + 5)
        if not title:
            first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
            title = first_line[:80] or "Operator request"
        spec = {
            "title": title,
            "priority": priority,
            "allow_send": False,
            "requires_initial_run": True,
            "context": (
                "## Operator request\n\n"
                f"{prompt}\n\n"
                "## Operating mode\n\n"
                "Work on this request after the initial schedule fires. Draft first; "
                "do not send anything externally without approval."
            ),
            "note": "Created from Quest Control",
            "watches": [{
                "type": "schedule",
                "next_fire_ts": initial_run_ts,
                "reason": "Run the operator's initial request once, then wait for follow-up instructions.",
            }],
        }
        helper = SCRIPT_DIR.parent / "skills" / "yaas-quest-creation" / "new-quest.py"
        try:
            cp = subprocess.run(
                [sys.executable, str(helper), json.dumps(spec)],
                capture_output=True, text=True, timeout=QUEST_CREATE_TIMEOUT_S, cwd=str(REPO_ROOT),
            )
        except Exception as e:
            self._send_json({"error": f"failed to create quest: {e}"}, 500)
            return
        match = re.search(r"^✓ Created ([a-z0-9-]+)$", cp.stdout or "", re.MULTILINE)
        if cp.returncode != 0 or not match:
            self._send_json({
                "error": "quest could not be created",
                "detail": ((cp.stderr or cp.stdout) or "").strip()[:500],
            }, 500)
            return
        self._send_json({
            "ok": True,
            "quest_id": match.group(1),
            "allow_send": False,
            "initial_run_at": initial_run_ts,
        }, 201)

    def _handle_review(self, approval_id: str, action: str, payload: dict):
        if not APPROVALS_FILE.exists():
            self._send_json({"error": "pending-approvals.json not found"}, 404)
            return

        try:
            item = approval_store.mutate_item(
                approval_id,
                lambda current: approval_state.apply_transition(
                    current, action, payload, datetime.now(timezone.utc)
                ),
            )
        except approval_state.InvalidPayload as e:
            msg = str(e)
            code = 400
            if msg.startswith("error:"):
                code = 500
            self._send_json({"error": msg}, code)
            return
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        if item is approval_store.NOT_FOUND:
            self._send_json({"error": "approval not found"}, 404)
            return
        if item is approval_state.ILLEGAL:
            self._send_json({"error": self._approval_conflict_message(action)}, 409)
            return

        rearmed = _ensure_approval_watch(item)
        self._send_json({"ok": True, "status": item["status"], "watch_armed": rearmed})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    # Loopback only. Off-host callers never reach the socket, regardless of any
    # host firewall state.
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Sidequestor dashboard -> http://localhost:{PORT} (loopback only)", flush=True)
        httpd.serve_forever()
