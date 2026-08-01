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
    GET  /api/dashboard       → live JSON snapshot of all quest state
    GET  /state/<path>        → raw state file
    POST /api/review/<id>     → mark item reviewed (optionally with edits)
    POST /api/cancel/<id>     → cancel item
"""

import fcntl
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
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR     = Path(__file__).parent
REPO_ROOT      = SCRIPT_DIR.parent
STATE_DIR      = REPO_ROOT / "state"
LOG_DIR        = REPO_ROOT / "logs"
APPROVALS_FILE = STATE_DIR / "pending-approvals.json"
DASHBOARD_HTML = REPO_ROOT / "dashboard.html"
PORT           = int(sys.argv[1]) if len(sys.argv) > 1 else 8877

# Workspace-specific hosts. No hardcoded defaults: they belong to whoever runs
# this, so they come from the gitignored repo-root .env (the same file triage.sh
# sources). Without them, a reconstructed Slack/Jira link is simply omitted; a
# stored permalink still works, because it carries its own host.
def _dotenv(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    if v:
        return v.strip()
    try:
        for line in (REPO_ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default

SLACK_HOST = _dotenv("SLACK_WORKSPACE_DOMAIN")            # e.g. acme.slack.com
JIRA_HOST  = (_dotenv("JIRA_BASE_URL").replace("https://", "")
                                      .replace("http://", "").rstrip("/"))

WORKER_TIMEOUT_S = 1800  # mirrors WORKER_TIMEOUT in triage.sh — a log older than
                         # this with no footer means the worker was killed, not running
LIVE_TAIL_LINES  = 60   # panel wants the fuller transcript; pill only shows the target name


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
    if not APPROVALS_FILE.exists():
        return {"version": 1, "items": []}
    with open(APPROVALS_FILE) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


_CONFLICT = object()  # sentinel: item exists but wrong status for this transition

def _update_approval(approval_id: str, updates: dict, from_status=("pending_review", "needs_reply")) -> dict | None | object:
    """Read-modify-write with exclusive lock.
    Returns the updated item, None if not found, or _CONFLICT if status mismatch.
    `from_status` is a str or a collection of allowed current statuses — the
    review queue surfaces both `pending_review` and `needs_reply` items, so the
    reviewer must be able to act on either (a needs_reply item is one the worker
    hasn't revised yet; the reviewer's explicit action still wins)."""
    allowed = {from_status} if isinstance(from_status, str) else set(from_status)
    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.load(f)
            item = next((i for i in data.get("items", []) if i.get("id") == approval_id), None)
            if item is None:
                return None
            if item.get("status") not in allowed:
                return _CONFLICT
            item.update(updates)
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            return item
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
    return {
        "id":             i.get("id"),
        "quest_id":       i.get("quest_id"),
        "quest_title":    i.get("quest_title", i.get("quest_id")),
        "action_type":    i.get("action_type", "slack_message"),
        "status":         i.get("status"),
        "created_at":     i.get("created_at"),
        "target":         t,
        "message_text":   i.get("message_text", ""),
        "context":        i.get("context", ""),
        "risk_reason":    i.get("risk_reason", ""),
        "review_note":    i.get("review_note", ""),
        "review_history": i.get("review_history", []),
        "worker_reply":   i.get("worker_reply", ""),
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
# triage.sh writes logs/worker-latest.log at dispatch time: a header line with
# the dirty targets, then a live-appended human-readable tool-call transcript
# (tee'd through format-stream.py as the worker runs), ending with a
# "=== Tokens: ..." footer once the worker exits. So "still running" = the
# footer hasn't been written yet and the file is fresh enough that it isn't
# just a killed/crashed run that never got to write one.

def build_live_run() -> dict:
    log_path = LOG_DIR / "worker-latest.log"
    not_running = {"running": False, "targets": [], "started_at": None, "tail": []}
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

    finished = any(l.startswith("=== Tokens") for l in lines[-3:])
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

    body = [l for l in lines[3:] if l.strip() and not l.startswith("===")]
    return {
        "running":    True,
        "targets":    targets,
        "started_at": started_at,
        "tail":       body[-LIVE_TAIL_LINES:],
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

_OPEN_WATCH_TYPES = {"slack_thread", "slack_channel", "slack_dm", "slack_mention", "email"}
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
                if i.get("status") not in ("pending_review", "needs_reply"):
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
        "total":          len(blocked) + len(awaiting_reply) + len(drafts)
                          + len(review) + len(commitments),
    }


# ── Dashboard payload builder ─────────────────────────────────────────────────
# NOTE: the payload must be deterministic between state changes — the ETag/304
# path in the handler hashes the serialized body, so any always-changing field
# (e.g. a "generated_at" timestamp) would silently defeat conditional polling.
# live_run is exempt from that determinism goal by nature (it mirrors a log
# file that grows while a worker runs) — that's a genuine state change, so a
# non-304 response on every poll during a live run is correct, not a bug.

def build_dashboard() -> dict:
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
        last_seen_ts = None   # newest non-blocked event of ANY type (incl. notes):
                              # a later note means the worker recovered after a block
        timeline_path = quest_dir / "timeline.ndjson"
        if timeline_path.exists():
            lines = [l for l in timeline_path.read_text().splitlines() if l.strip()]
            for raw in reversed(lines[-20:]):
                try:
                    e = json.loads(raw)
                    ev = e.get("event", "")
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
            "id":           quest_dir.name,
            "title":        meta.get("title", quest_dir.name),
            "status":       meta.get("status", "active"),
            "priority":     meta.get("priority", "normal"),
            "allow_send":   meta.get("allow_send", False),
            "last_action":  last_action,
            "last_blocked": last_blocked,
            "last_seen_ts": last_seen_ts,
        })

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
                              if i.get("status") in ("pending_review", "needs_reply")]
        except Exception:
            pass

    # Daily briefs: markdown files in state/briefs/, named <date>_<hhmm>_<type>.md
    # (e.g. 2026-06-05_0830_morning.md). Newest first, full markdown included so
    # the dashboard renders them client-side with clickable links.
    briefs = []
    briefs_dir = STATE_DIR / "briefs"
    if briefs_dir.exists():
        for bf in sorted(briefs_dir.glob("*.md"), reverse=True)[:30]:
            try:
                md = bf.read_text()
            except Exception:
                continue
            parts = bf.stem.split("_")
            btype = parts[2] if len(parts) >= 3 else (parts[-1] if parts else "")
            title = ""
            for line in md.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            briefs.append({
                "file":     bf.name,
                "type":     btype,
                "title":    title or bf.name,
                "markdown": md,
                "ts":       datetime.fromtimestamp(bf.stat().st_mtime, timezone.utc).isoformat(),
            })

    return {
        "triage":         triage_state,
        "live_run":       build_live_run(),
        "quests":         quests,
        "recent_activity": recent_activity,
        "pending_review": pending_review,
        "briefs":         briefs,
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
    pending    = [i for i in appr_data.get("items", [])
                  if i.get("status") in ("pending_review", "needs_reply")]

    needs_you, other_actions = [], []
    for i in pending:
        (needs_you if i.get("action_type", "slack_message") in _MSG_ACTION_TYPES
         else other_actions).append(_approval_card(i))

    recent, summary_only = [], 0
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
            if e.get("event") not in _OUTBOUND_EVENTS:
                continue
            rec = _normalize_msg_event(e, appr_by_id)
            if rec is None:
                if e.get("event") in _SENT_EVENTS:
                    summary_only += 1
                continue
            rec["quest_id"], rec["quest_title"] = quest_dir.name, title
            recent.append(rec)

    for i in pending:  # awaiting-send drafts (message types only)
        if i.get("action_type", "slack_message") in _MSG_ACTION_TYPES:
            recent.append(_approval_draft_record(i))

    recent.sort(key=lambda r: r.get("ts") or "", reverse=True)
    recent = recent[:40]

    return {
        "needs_you":          needs_you,
        "other_actions":      other_actions,
        "recent_messages":    recent,
        "summary_only_count": summary_only,
    }


# ── Quest detail builder (lazy-fetched by the drawer) ────────────────────────

_TIMELINE_KEYS = ("ts", "event", "note", "reason", "permalink", "by", "channel",
                  "channel_id", "thread_ts", "response_ts", "msg_ts", "message_ts",
                  "approval_id", "draft_id", "action_type", "to", "subject",
                  "message_text")
_TIMELINE_CAP  = 500

def build_quest_detail(quest_id: str) -> dict | None:
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

    # conversation: verbatim-only outbound messages for THIS quest (newest-first)
    # + this quest's pending/reviewed message-type approvals as awaiting-send
    # drafts. Same recoverable-body filter as build_messages(); the full timeline
    # above still lists every event (including summary-only ones).
    appr_data  = _read_approvals() if APPROVALS_FILE.exists() else {"items": []}
    appr_by_id = _approvals_index(appr_data)
    conversation = []
    for raw in reversed(lines[-_TIMELINE_CAP:]):
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if not isinstance(e, dict) or e.get("event") not in _OUTBOUND_EVENTS:
            continue
        rec = _normalize_msg_event(e, appr_by_id)
        if rec:
            conversation.append(rec)
    for i in appr_data.get("items", []):
        if (i.get("quest_id") == quest_id
                and i.get("status") in ("pending_review", "needs_reply", "reviewed")
                and i.get("action_type", "slack_message") in _MSG_ACTION_TYPES):
            conversation.append(_approval_draft_record(i))
    conversation.sort(key=lambda r: r.get("ts") or "", reverse=True)

    # ── Per-quest open items: the durable "what is THIS quest still waiting on"
    #    summary. Open threads come from watch.json (each watch = a thread/inbox
    #    the bot is actively tracking, with its own reason + link); blocked +
    #    review + commitments mirror the aggregate build_open_items categories,
    #    scoped to this quest. Small per quest, so listing every watch here is
    #    the right granularity (unlike the noisy 100+ aggregate view).
    def _as_float(x):
        try:    return float(x)
        except (TypeError, ValueError): return 0.0

    open_threads, scheduled = [], []
    for e in watches:
        wtype = e.get("type")
        if wtype in _OPEN_WATCH_TYPES:
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
        if i.get("quest_id") == quest_id and i.get("status") in ("pending_review", "needs_reply")
    ]

    commitments = []
    if COMMITMENTS_FILE.exists():
        try:
            cdata = json.loads(COMMITMENTS_FILE.read_text())
            commitments = [c for c in (cdata.get("items", []) if isinstance(cdata, dict) else [])
                           if c.get("quest_id") == quest_id]
        except Exception:
            pass

    open_items = {
        "blocked":       blocked_now,
        "threads":       open_threads,
        "threads_total": threads_total,
        "scheduled":     scheduled,
        "review":        review,
        "commitments":   commitments,
    }

    return {
        "id":             quest_id,
        "meta":           meta,
        "context_md":     context_md,
        "watches":        watches,
        "timeline":       timeline,
        "timeline_total": total,
        "conversation":   conversation,
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
                {**i, **_approval_target_link(i)}
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

    def _serve_dashboard(self):
        """Serve the HTML shell: inject a per-response CSP nonce into the single
        inline <script>, and (re)issue the session cookie. This is the only
        unauthenticated route — it hands the browser the cookie that every other
        route then requires."""
        try:
            html = DASHBOARD_HTML.read_text()
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

        # Bootstrap route: unauthenticated, issues the session cookie.
        if path in ("/", "/index.html"):
            self._serve_dashboard()
            return

        # Everything else (state + APIs) requires the cookie.
        if not self._authed():
            self.send_error(403, "forbidden")
            return

        if path == "/api/dashboard":
            self._send_json_etag(build_dashboard)
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

        path   = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        parts = [p for p in path.strip("/").split("/") if p]
        # Expected: ["api", "review" | "revise" | "cancel", "<id>"]
        if len(parts) == 3 and parts[0] == "api" and parts[1] in ("review", "revise", "cancel"):
            self._handle_review(parts[2], parts[1], payload)
            return

        # Manual quest dispatch: ["api", "prompt", "<quest_id>"]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "prompt":
            self._handle_prompt(urllib.parse.unquote(parts[2]), payload)
            return

        self.send_error(404)

    def _handle_prompt(self, quest_id: str, payload: dict):
        """Fire a dashboard-initiated worker run against one quest with a
        free-text instruction. Spawns manual-dispatch.sh detached; it shares
        triage's lock (busy → exit 75) and streams into worker-latest.log, which
        the dashboard's live panel already renders. Returns immediately — the
        run is watched live, not awaited here."""
        instruction = (payload.get("instruction") or "").strip()
        if not instruction:
            self._send_json({"error": "instruction is required"}, 400)
            return
        if len(instruction) > 4000:
            self._send_json({"error": "instruction too long (max 4000 chars)"}, 400)
            return
        # Guard obvious bad ids here too; manual-dispatch.sh re-validates against
        # the actual folder and exits 2 if the quest is unknown.
        if "/" in quest_id or quest_id in ("", ".", ".."):
            self._send_json({"error": "invalid quest id"}, 400)
            return
        if not (STATE_DIR / "quests" / "active" / quest_id).is_dir():
            self._send_json({"error": "quest not found"}, 404)
            return

        # If a worker is already running (triage tick or a prior manual run), the
        # shared lock would bounce us — report busy instead of silently queueing.
        if build_live_run().get("running"):
            self._send_json({"error": "a worker is already running — try again shortly", "busy": True}, 409)
            return

        script = SCRIPT_DIR / "manual-dispatch.sh"
        try:
            # Detached: don't hold the HTTP connection open for a multi-minute
            # Opus run. Progress is observed via /api/dashboard live_run.
            subprocess.Popen(
                ["bash", str(script), quest_id, instruction],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, cwd=str(REPO_ROOT),
            )
        except Exception as e:
            self._send_json({"error": f"failed to launch dispatch: {e}"}, 500)
            return
        self._send_json({"ok": True, "quest_id": quest_id, "launched": True})

    def _handle_review(self, approval_id: str, action: str, payload: dict):
        if not APPROVALS_FILE.exists():
            self._send_json({"error": "pending-approvals.json not found"}, 404)
            return

        now = datetime.now(timezone.utc).isoformat()

        if action == "revise":
            # Explicit "Submit for revision": send the draft back to the worker
            # for another pass, no matter what. Reuses the needs_reply loop the
            # '?' heuristic below relies on, so the worker revises + re-surfaces
            # it for another review round (repeatable across turns). Requires a
            # note so the worker has something to act on.
            note = payload.get("review_note", "").strip()
            if not note:
                self._send_json({"error": "revision requires an instruction note"}, 400)
                return
            updates: dict = {"status": "needs_reply", "review_note": note,
                             "asked_at": now}
            if "message_text" in payload:
                updates["message_text"] = payload["message_text"]
                updates["human_edited"] = True
        elif action == "review":
            note = payload.get("review_note", "").strip()
            edited = "message_text" in payload
            # A note that is a question ('?') with no accompanying message edit
            # is a query to the bot, NOT an approval. Do not mark it reviewed
            # (which would send it and hide it) — set needs_reply so it stays in
            # the queue and the worker answers + re-surfaces it next triage.
            # (The "Submit for revision" button above is the explicit form of this.)
            if note and "?" in note and not edited:
                updates = {"status": "needs_reply", "review_note": note,
                           "asked_at": now}
            else:
                updates = {"status": "reviewed", "reviewed_at": now}
                if edited:
                    updates["message_text"] = payload["message_text"]
                    updates["human_edited"] = True
                if note:
                    updates["review_note"] = note
        else:
            updates = {"status": "cancelled", "cancelled_at": now}

        try:
            item = _update_approval(approval_id, updates)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        if item is None:
            self._send_json({"error": "approval not found"}, 404)
            return
        if item is _CONFLICT:
            self._send_json({"error": "already reviewed, executing, executed, or cancelled"}, 409)
            return

        self._send_json({"ok": True, "status": item["status"]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    # Loopback only. Off-host callers never reach the socket, regardless of any
    # host firewall state.
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"YAAS dashboard → http://localhost:{PORT} (loopback only)", flush=True)
        httpd.serve_forever()
