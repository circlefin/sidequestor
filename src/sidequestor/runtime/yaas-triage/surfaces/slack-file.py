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
slack-file.py — post a local file into Slack, or fetch the files attached to a message.

The Slack MCP cannot do either. This is the sanctioned path for both. Quest uploads
apply dispatch-target matching, an active quest with `allow_send: true` or a claimed
approval for this exact file, the stale-thread hold, and timeline logging. Explicit
interactive uploads are allowed outside dispatches and are not logged to a quest.

Dispatch detection reads SIDEQUESTOR_DISPATCH_TARGET, which a worker with a shell could
unset. That makes the interactive refusal a guard against mistakes, not a security
boundary: the same worker can already reach the Slack token through the keychain helper.

Usage
=====
    python3 yaas-triage/surfaces/slack-file.py send '<json>'
    python3 yaas-triage/surfaces/slack-file.py approval-spec '<json>'
    python3 yaas-triage/surfaces/slack-file.py fetch '<json>'

send / approval-spec <json> fields:
    channel_id       (required)  conversation ID (C.../D.../G...). Member IDs are refused:
                                 the approval target and the follow-up watch both key on
                                 the conversation, and an upload cannot resolve one.
    file             (required)  path to a local regular file
    filename         (optional)  name shown in Slack (default: the file's basename)
    title            (optional)  title shown in Slack (default: filename)
    initial_comment  (optional)  message posted with the file, sent verbatim and logged
    thread_ts        (optional)  parent ts to post in-thread
    quest_id         (optional)  quest to authorize and log under; required unless the
                                 reactions dispatch is running or interactive is true
    approval_id      (optional)  claimed approval being executed
    note             (optional)  short human summary for the timeline `note`
    interactive      (optional)  true for an explicitly authorized manual upload without
                                 a quest; refused inside a dispatch

approval-spec prints the {action_type, target, message_text} a reviewer must approve for
this exact upload. Merge it into the `sq approval write` payload. The target pins the
file's SHA-256, size and name, so different bytes cannot ride on an approval.

fetch <json> fields:
    channel_id + ts  the message whose files to fetch (top-level or a thread reply), or
    file_id          one file by id
    out_dir          (optional)  destination directory; default is a fresh private temp
                                 directory. Never inside the workspace state/ tree.
    max_bytes        (optional)  per-file cap, default 20 MiB
    any_type         (optional)  bool; default fetches only images and PDFs

Output (stdout): compact JSON.
    send:  {"file_id":..,"response_ts":..,"permalink":..,"channel_id":..,"logged":..}
           or {"held":true,"reason":..,"approval_id":..} when held for review
    fetch: {"files":[{"file_id","name","mimetype","size","path"}],"skipped":[{..,"reason"}]}

Exit codes:
    0  done (or held for review)
    1  bad arguments, or the send was denied by policy; nothing was sent
    2  failed; nothing was posted
    3  outcome unknown: Slack may have posted the file. Do NOT retry; check Slack first
    4  transient failure before anything was posted; safe to retry
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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
    override = (os.environ.get("SIDEQUESTOR_WORKSPACE")
                or os.environ.get("YAAS_WORKSPACE"))
    if override:
        return Path(override).expanduser().resolve()
    p = Path(start).resolve()
    for d in (p, *p.parents):
        if (d / "yaas-triage").is_dir():
            return d
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT = _repo_root(__file__)
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
from timeline_io import utc_now, quest_dir, append_timeline
from tick_state import SLACK_CONVERSATION_RE
import approval_store
import slack_files


def _load_slack_send():
    """slack-send.py's thread reader and stale limit, so both senders hold identically."""
    spec = importlib.util.spec_from_file_location("slack_send_shared", SCRIPT_DIR / "slack-send.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_slack_send = _load_slack_send()

EXIT_FOR = {
    slack_files.OK: 0,
    slack_files.BAD_ARGS: 1,
    slack_files.AUTH: 2,
    slack_files.ERROR: 2,
    slack_files.OUTCOME_UNKNOWN: 3,
    slack_files.TRANSIENT: 4,
}


class Denied(Exception):
    pass


def _fail(message, code):
    print(f"error: {message}", file=sys.stderr)
    return code


def _valid_quest_id(value):
    return bool(value) and "/" not in value and value not in (".", "..") and not any(
        ord(ch) < 32 or ord(ch) == 127 for ch in value)


# ── send: validation and approval binding ────────────────────────────────────

def _prepare(p):
    """Validate a send payload and pin the file it names. Raises Denied on bad input."""
    channel_id = str(p.get("channel_id") or "").strip()
    if not SLACK_CONVERSATION_RE.fullmatch(channel_id):
        raise Denied(f"channel_id {channel_id!r} must be a conversation id (C…/D…/G…)")
    raw = p.get("file")
    if not raw:
        raise Denied("file is required")
    path = Path(str(raw)).expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise Denied(f"cannot read file {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Denied(f"{path} is not a regular file")
    if info.st_size == 0 or info.st_size > slack_files.UPLOAD_MAX_BYTES:
        raise Denied(f"{path} is {info.st_size} bytes; Slack accepts 1 byte to 1 GiB")
    quest_id = str(p.get("quest_id") or "").strip()
    if quest_id and not _valid_quest_id(quest_id):
        raise Denied(f"invalid quest id {quest_id!r}")
    filename = str(p.get("filename") or path.name)
    return {
        "channel_id": channel_id,
        "thread_ts": str(p.get("thread_ts") or "") or None,
        "path": path,
        "filename": filename,
        # The effective title, so the approval pins what Slack will actually display.
        "title": str(p.get("title") or filename),
        "size": info.st_size,
        "sha256": slack_files.sha256_file(path),
        "comment": str(p.get("initial_comment") or ""),
        "quest_id": quest_id,
        "approval_id": str(p.get("approval_id") or ""),
        "note": str(p.get("note") or ""),
        "interactive": p.get("interactive") is True,
    }


def approval_target(job):
    target = {
        "surface": "slack",
        "action": "file_upload",
        "channel_id": job["channel_id"],
        "file": str(job["path"]),
        "filename": job["filename"],
        "title": job["title"],
        "size": job["size"],
        "sha256": job["sha256"],
    }
    if job["thread_ts"]:
        target["thread_ts"] = job["thread_ts"]
    return target


def approval_spec(job):
    return {
        "action_type": "remote_request",
        "target": approval_target(job),
        "message_text": job["comment"],
    }


def _claimed_approval(job):
    """The claimed approval for this exact upload, or None.

    Binds on the full target (destination, path, name, size, digest) and the exact
    comment, so neither different bytes nor a reworded comment can ride on it.
    """
    if not job["approval_id"] or not job["quest_id"]:
        return None
    try:
        item = next(
            (row for row in approval_store.read_queue().get("items", [])
             if isinstance(row, dict) and row.get("id") == job["approval_id"]),
            None,
        )
        if not item or item.get("quest_id") != job["quest_id"]:
            return None
        if item.get("status") != "executing" or item.get("action_type") != "remote_request":
            return None
        if item.get("target") != approval_target(job):
            return None
        if (item.get("message_text") or "") != job["comment"]:
            return None
        expiry = datetime.fromisoformat(str(item["lease_expires_at"]).replace("Z", "+00:00"))
        return item if expiry.timestamp() >= time.time() else None
    except Exception:
        # Approval-state failures must never weaken the send guard.
        return None


def _policy_reason(job):
    """Fail-closed reason to refuse this upload, or None when authorized."""
    target = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = job["quest_id"]
    if target == "reactions" and quest_id:
        return "the reactions dispatch cannot write through a quest"
    if target and target != "reactions" and target != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {target!r}"
    if job["interactive"]:
        if target:
            return "interactive uploads are unavailable inside a dispatch"
        if quest_id:
            return "interactive uploads cannot be attributed to a quest"
        if job["approval_id"]:
            return "interactive uploads cannot use a quest approval"
        return None
    if target == "reactions" and not quest_id:
        return None
    if not quest_id:
        return "quest_id is required unless interactive is true for a manual upload"
    qdir = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not qdir.is_dir():
        return (f"quest {quest_id} is not in state/quests/active; a completed or "
                f"archived quest may not send")
    try:
        meta = json.loads((qdir / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid quest policy file")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    if not meta.get("allow_send") and not _claimed_approval(job):
        return (f"quest {quest_id} has allow_send false and no claimed approval for this "
                f"exact file and destination; run approval-spec and queue it for review")
    return None


def _stale_reason(job, now=None):
    """Same hold rule as slack-send.py: force-draft, or a thread that has gone quiet."""
    if os.environ.get("YAAS_FORCE_DRAFT") == "1":
        return "force-draft mode is active (YAAS_FORCE_DRAFT=1)"
    if not job["thread_ts"]:
        return None
    now = now if now is not None else time.time()
    newest = _slack_send._thread_last_activity(job["channel_id"], job["thread_ts"])
    if newest is None:
        return "could not read the thread to confirm it is still live"
    approval = _claimed_approval(job)
    freshest = newest
    if approval and approval.get("reviewed_at"):
        try:
            reviewed = datetime.fromisoformat(
                str(approval["reviewed_at"]).replace("Z", "+00:00")).timestamp()
            freshest = max(newest, reviewed)
        except (TypeError, ValueError, OverflowError):
            pass
    age_h = (now - freshest) / 3600.0
    if age_h > _slack_send.STALE_HOURS:
        return (f"newest message in the thread is {age_h:.1f}h old "
                f"(limit {_slack_send.STALE_HOURS:.0f}h), so the conversation has moved on")
    return None


def _queue_for_review(job, reason):
    payload = {
        "quest_id": job["quest_id"],
        "quest_title": job["quest_id"],
        "source": "stale_reply_guard",
        **approval_spec(job),
        "context": job["note"] + f" [held automatically: {reason}]",
        "risk_reason": f"stale-reply guard: {reason}",
    }
    out = subprocess.run(
        ["python3", str(SCRIPT_DIR.parent / "ledger" / "approval-helper.py"), "write",
         json.dumps(payload)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise Denied(f"could not queue the held upload for review: "
                     f"{(out.stderr or out.stdout).strip()[:200]}")
    approval_id = (out.stdout or "").strip()
    if approval_id:
        return approval_id
    # Empty output means the helper deduplicated against a pending item. It matches file
    # uploads on the full target, so that item is this exact upload; report its id.
    existing = next(
        (row.get("id") for row in approval_store.read_queue().get("items", [])
         if isinstance(row, dict) and row.get("quest_id") == job["quest_id"]
         and row.get("status") == "pending_review"
         and row.get("target") == approval_target(job)
         and (row.get("message_text") or "") == job["comment"]),
        None)
    if not existing:
        raise Denied("the held upload was not queued and no matching review item exists")
    return existing


def _file_record(job, file_id=""):
    record = {"filename": job["filename"], "size": job["size"], "sha256": job["sha256"]}
    if file_id:
        record["file_id"] = file_id
    return record


def cmd_send(p):
    try:
        job = _prepare(p)
    except Denied as exc:
        return _fail(str(exc), 1)

    # Authorization first, so a denied upload has no external side effect at all.
    reason = _policy_reason(job)
    if reason:
        return _fail(f"send denied: {reason}", 1)

    held = _stale_reason(job)
    if held:
        if job["interactive"]:
            return _fail(f"send denied: {held}; an interactive upload cannot be queued "
                         f"for quest review", 1)
        try:
            approval_id = _queue_for_review(job, held)
        except Denied as exc:
            # Nothing was uploaded, so this is a plain failure, not a hold.
            return _fail(str(exc), 2)
        qdir = quest_dir(REPO_ROOT, job["quest_id"]) if job["quest_id"] else None
        if qdir:
            append_timeline(qdir, {
                "ts": utc_now(), "event": "draft_posted",
                "channel_id": job["channel_id"], "thread_ts": job["thread_ts"],
                "approval_id": approval_id, "held_reason": held,
                "message_text": job["comment"], "files": [_file_record(job)],
                "note": job["note"] + " [auto-held by the stale-reply guard]"})
        print(json.dumps({"held": True, "reason": held, "approval_id": approval_id,
                          "response_ts": "", "permalink": ""}))
        return 0

    try:
        result = slack_files.upload(
            job["path"], job["channel_id"], filename=job["filename"], title=job["title"],
            thread_ts=job["thread_ts"], initial_comment=job["comment"] or None,
            sha256=job["sha256"])
    except slack_files.SlackFileError as exc:
        return _fail(str(exc), EXIT_FOR.get(exc.code, 2))
    except OSError as exc:
        return _fail(f"cannot read {job['path']}: {exc}", 2)

    # From here the file is posted. Nothing below may turn that into a failure, or a
    # worker would retry an upload that already landed.
    logged, log_error = False, ""
    try:
        logged = _log_sent(job, result)
    except Exception as exc:
        log_error = f"{type(exc).__name__}: {exc}"
        print(f"warning: upload succeeded (file {result['file_id']}) but the timeline write "
              f"failed: {log_error}; do not retry the upload", file=sys.stderr)

    output = {**result, "channel_id": job["channel_id"], "logged": logged}
    if log_error:
        output["log_error"] = log_error
    print(json.dumps(output, ensure_ascii=False))
    return 0


def _log_sent(job, result):
    if job["quest_id"]:
        qdir = quest_dir(REPO_ROOT, job["quest_id"])
        if qdir is None:
            print(f"warning: quest '{job['quest_id']}' not found; upload succeeded but "
                  f"not logged", file=sys.stderr)
        else:
            entry = {
                "ts": utc_now(), "event": "message_sent",
                "channel_id": job["channel_id"], "message_text": job["comment"],
                "files": [_file_record(job, result["file_id"])],
                "response_ts": result["response_ts"] or (job["thread_ts"] or ""),
            }
            if job["thread_ts"]:
                entry["thread_ts"] = job["thread_ts"]
            if result["permalink"]:
                entry["permalink"] = result["permalink"]
            if job["note"]:
                entry["note"] = job["note"]
            append_timeline(qdir, entry)
            return True
    return False


def cmd_approval_spec(p):
    try:
        job = _prepare(p)
    except Denied as exc:
        return _fail(str(exc), 1)
    print(json.dumps(approval_spec(job), ensure_ascii=False, separators=(",", ":")))
    return 0


# ── fetch ─────────────────────────────────────────────────────────────────────

def _out_dir(raw):
    """Resolve the destination, refusing anywhere under the workspace state/ tree."""
    if not raw:
        return Path(tempfile.mkdtemp(prefix="sidequestor-slack-files-"))
    path = Path(str(raw)).expanduser().resolve()
    state = (REPO_ROOT / "state").resolve()
    if path == state or state in path.parents:
        raise Denied(f"out_dir {path} is inside the workspace state/ tree")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def cmd_fetch(p):
    channel_id = str(p.get("channel_id") or "").strip()
    ts = str(p.get("ts") or "").strip()
    file_id = str(p.get("file_id") or "").strip()
    if not file_id and not (channel_id and ts):
        return _fail("fetch needs channel_id + ts, or file_id", 1)
    try:
        max_bytes = int(p.get("max_bytes") or slack_files.DEFAULT_MAX_BYTES)
        if max_bytes <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _fail("max_bytes must be a positive integer", 1)
    types = () if p.get("any_type") else slack_files.DEFAULT_TYPES
    try:
        out_dir = _out_dir(p.get("out_dir"))
    except Denied as exc:
        return _fail(str(exc), 1)

    try:
        if file_id:
            infos = [slack_files.file_info(file_id)]
        else:
            infos = slack_files.message_files(channel_id, ts)
    except slack_files.SlackFileError as exc:
        return _fail(str(exc), EXIT_FOR.get(exc.code, 2))

    fetched, skipped, worst = [], [], 0
    for info in infos:
        summary = {"file_id": info.get("id", ""), "name": info.get("name", ""),
                   "mimetype": info.get("mimetype", ""), "size": info.get("size")}
        why = slack_files.skip_reason(info, max_bytes, types)
        if why:
            skipped.append({**summary, "reason": why})
            continue
        try:
            path = slack_files.download(info, out_dir, max_bytes)
        except slack_files.SlackFileError as exc:
            skipped.append({**summary, "reason": str(exc)})
            code = EXIT_FOR.get(exc.code, 2)
            worst = code if code == 2 or not worst else worst
            continue
        fetched.append({**summary, "path": str(path)})

    print(json.dumps({"out_dir": str(out_dir), "files": fetched, "skipped": skipped},
                     ensure_ascii=False))
    return worst


COMMANDS = {"send": cmd_send, "approval-spec": cmd_approval_spec, "fetch": cmd_fetch}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 1
    handler = COMMANDS.get(argv[0])
    if handler is None or len(argv) != 2:
        return _fail(f"usage: slack-file.py <{'|'.join(COMMANDS)}> '<json>'", 1)
    try:
        payload = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON argument: {exc}", 1)
    if not isinstance(payload, dict):
        return _fail("the JSON argument must be an object", 1)
    return handler(payload)


if __name__ == "__main__":
    sys.exit(main())
