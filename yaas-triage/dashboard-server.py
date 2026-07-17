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
import secrets
import socketserver
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

WORKER_TIMEOUT_S = 900   # mirrors WORKER_TIMEOUT in triage.sh — a log older than
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

def _update_approval(approval_id: str, updates: dict, from_status: str = "pending_review") -> dict | None | object:
    """Read-modify-write with exclusive lock.
    Returns the updated item, None if not found, or _CONFLICT if status mismatch."""
    with open(APPROVALS_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.load(f)
            item = next((i for i in data.get("items", []) if i.get("id") == approval_id), None)
            if item is None:
                return None
            if item.get("status") != from_status:
                return _CONFLICT
            item.update(updates)
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            return item
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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


# ── Quest detail builder (lazy-fetched by the drawer) ────────────────────────

_TIMELINE_KEYS = ("ts", "event", "note", "reason", "permalink", "by", "channel")
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

    timeline, total = [], 0
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
                for k in ("note", "reason"):
                    if isinstance(entry.get(k), str) and len(entry[k]) > 300:
                        entry[k] = entry[k][:300] + "…"
                timeline.append(entry)
        except Exception:
            pass

    return {
        "id":             quest_id,
        "meta":           meta,
        "context_md":     context_md,
        "watches":        watches,
        "timeline":       timeline,
        "timeline_total": total,
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
                i for i in data.get("items", [])
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

        self.send_error(404)

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
