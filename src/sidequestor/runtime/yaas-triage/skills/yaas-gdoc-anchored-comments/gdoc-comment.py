#!/usr/bin/env python3
"""Add verified, text-anchored Google Doc comments through a guarded writer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent
SURFACES_DIR = RUNTIME_DIR / "surfaces"
sys.path.insert(0, str(RUNTIME_DIR))
sys.path.insert(0, str(SURFACES_DIR))

import approval_store
from timeline_io import append_timeline, quest_dir, utc_now


MAX_ANCHORS = 1
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _repo_root(start):
    override = os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    path = Path(start).resolve()
    for directory in (path, *path.parents):
        if (directory / "yaas-triage").is_dir():
            return directory
    raise SystemExit(f"cannot locate repo root above {start} (no ancestor has yaas-triage/)")


REPO_ROOT = _repo_root(__file__)


class CommentWriteError(RuntimeError):
    pass


class PrerequisiteError(ValueError):
    """Preflight failed, so no write was attempted."""


def _enabled():
    raw = os.environ.get("SIDEQUESTOR_GDOC_COMMENTS_ENABLED", "1").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ValueError("SIDEQUESTOR_GDOC_COMMENTS_ENABLED must be 1 or 0")


def _valid_quest_id(value):
    return bool(value) and "/" not in value and value not in (".", "..") and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def _validated(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    quest_id = str(payload.get("quest_id") or "").strip()
    if not _valid_quest_id(quest_id):
        raise ValueError("quest_id is required and must be a valid active quest id")
    doc_id = str(payload.get("doc_id") or "").strip()
    if not DOC_ID_RE.fullmatch(doc_id):
        raise ValueError("doc_id is required and may contain only letters, digits, '_' and '-'")
    anchors = payload.get("anchors")
    if not isinstance(anchors, list) or len(anchors) != MAX_ANCHORS:
        raise ValueError("anchors must contain exactly 1 item per idempotent write")
    normalized = []
    seen = set()
    for index, item in enumerate(anchors, 1):
        if not isinstance(item, dict):
            raise ValueError(f"anchor {index} must be an object")
        text = str(item.get("text") or "").strip()
        comment = str(item.get("comment") or "").strip()
        if not text or not comment:
            raise ValueError(f"anchor {index} requires non-empty text and comment")
        if len(comment.encode("utf-8")) > 2048:
            raise ValueError(f"anchor {index} comment must not exceed 2048 UTF-8 bytes")
        if text in seen:
            raise ValueError(f"duplicate anchor text is not allowed: {text!r}")
        seen.add(text)
        normalized.append({"text": text, "comment": comment})
    value = dict(payload)
    value.update({"quest_id": quest_id, "doc_id": doc_id, "anchors": normalized})
    return value


def _request_fingerprint(payload):
    body = {
        "quest_id": payload["quest_id"],
        "doc_id": payload["doc_id"],
        "anchors": payload["anchors"],
    }
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def approval_target(payload):
    return {
        "surface": "google_docs",
        "action": "anchor_comments",
        "doc_id": payload["doc_id"],
        "request_sha256": _request_fingerprint(payload),
    }


def approval_message(payload):
    return "\n".join(
        f'Comment on "{item["text"]}": {item["comment"]}' for item in payload["anchors"])


def approval_spec(payload):
    return {
        "action_type": "remote_request",
        "target": approval_target(payload),
        "message_text": approval_message(payload),
    }


def _claimed_approval(payload):
    approval_id = str(payload.get("approval_id") or "")
    if not approval_id:
        return False
    try:
        item = next(
            (row for row in approval_store.read_queue().get("items", [])
             if isinstance(row, dict) and row.get("id") == approval_id),
            None,
        )
        if not item or item.get("quest_id") != payload["quest_id"]:
            return False
        if item.get("status") != "executing" or item.get("action_type") != "remote_request":
            return False
        if item.get("target") != approval_target(payload):
            return False
        if item.get("message_text") != approval_message(payload):
            return False
        expiry = datetime.fromisoformat(str(item["lease_expires_at"]).replace("Z", "+00:00"))
        return expiry.timestamp() >= time.time()
    except Exception:
        return False


def write_policy_reason(payload):
    if not _enabled():
        return "anchored Google Doc comments are disabled by SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0"
    target = os.environ.get("SIDEQUESTOR_DISPATCH_TARGET", "").strip()
    quest_id = payload["quest_id"]
    if target and target != quest_id:
        return f"quest_id {quest_id!r} does not match dispatch target {target!r}"
    active = REPO_ROOT / "state" / "quests" / "active" / quest_id
    if not active.is_dir():
        return f"quest {quest_id} is not in state/quests/active; it may not comment on a Google Doc"
    try:
        meta = json.loads((active / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("invalid meta.json")
    except Exception as exc:
        return f"cannot read quest policy for {quest_id}: {exc}"
    if not meta.get("allow_send") and not _claimed_approval(payload):
        return (f"quest {quest_id} has allow_send false and no claimed Google Doc approval "
                "for this exact document and comment set")
    if not str(payload.get("idempotency_key") or "").strip():
        return "idempotency_key is required for every Google Doc comment write"
    return None


def _gws(*args):
    binary = os.environ.get("GWS_BIN") or "gws"
    if not shutil.which(binary):
        raise PrerequisiteError(f"gws CLI is not installed: {binary}")
    try:
        completed = subprocess.run(
            [binary, *args], text=True, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gws request failed: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"gws exited {completed.returncode}")
    start = completed.stdout.find("{")
    if start < 0:
        raise RuntimeError("gws returned no JSON object")
    try:
        return json.loads(completed.stdout[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("gws returned invalid JSON") from exc


def _utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


def _tabs(tabs):
    for tab in tabs:
        yield tab
        yield from _tabs(tab.get("childTabs") or [])


def _paragraphs(content):
    for item in content:
        if "paragraph" in item:
            yield item["paragraph"]
        table = item.get("table") or {}
        for row in table.get("tableRows") or []:
            for cell in row.get("tableCells") or []:
                yield from _paragraphs(cell.get("content") or [])
        toc = item.get("tableOfContents") or {}
        yield from _paragraphs(toc.get("content") or [])


def _text_spans(paragraph):
    """Join only adjacent text runs, never text across an inline object or index gap."""
    start = end = None
    content = ""
    for element in paragraph.get("elements") or []:
        run = element.get("textRun") or {}
        value = run.get("content")
        left, right = element.get("startIndex"), element.get("endIndex")
        if (not isinstance(value, str) or not isinstance(left, int)
                or not isinstance(right, int) or right - left != _utf16_length(value)):
            if content:
                yield start, content
            start = end = None
            content = ""
            continue
        if content and left != end:
            yield start, content
            start = None
            content = ""
        if start is None:
            start = left
        content += value
        end = right
    if content:
        yield start, content


def _resolve_range(document, text):
    matches = []
    for tab in _tabs(document.get("tabs") or []):
        tab_id = (tab.get("tabProperties") or {}).get("tabId")
        document_tab = tab.get("documentTab") or {}
        if not tab_id:
            continue
        segments = [(None, document_tab.get("body") or {})]
        for kind in ("headers", "footers", "footnotes"):
            segments.extend((document_tab.get(kind) or {}).items())
        for segment_id, segment in segments:
            for paragraph in _paragraphs(segment.get("content") or []):
                for base, content in _text_spans(paragraph):
                    offset = 0
                    while (found := content.find(text, offset)) >= 0:
                        start = base + _utf16_length(content[:found])
                        match = {
                            "startIndex": start,
                            "endIndex": start + _utf16_length(text),
                            "tabId": tab_id,
                        }
                        if segment_id is not None:
                            match["segmentId"] = segment_id
                        matches.append(match)
                        offset = found + 1
    if not matches:
        raise ValueError(f"anchor text was not found: {text!r}")
    if len(matches) != 1:
        raise ValueError(f"anchor text is ambiguous ({len(matches)} matches): {text!r}")
    return matches[0]


def _preflight(payload):
    doc_id = payload["doc_id"]
    try:
        file = _gws("drive", "files", "get", "--params", json.dumps({
            "fileId": doc_id, "fields": "capabilities(canComment)",
            "supportsAllDrives": True,
        }))
        if not (file.get("capabilities") or {}).get("canComment"):
            raise PrerequisiteError("the authenticated Google account cannot comment on this document")
        document = _gws("docs", "documents", "get", "--params", json.dumps({
            "documentId": doc_id,
            "includeTabsContent": True,
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
        }))
    except RuntimeError as exc:
        raise PrerequisiteError(str(exc)) from exc
    if document.get("documentId") != doc_id:
        raise PrerequisiteError("Docs API returned a different document")
    revision_id = document.get("revisionId")
    if revision_id is not None and (not isinstance(revision_id, str) or not revision_id.strip()):
        raise PrerequisiteError("Docs API returned an invalid revisionId")
    return {
        "range": _resolve_range(document, payload["anchors"][0]["text"]),
        "revision_id": revision_id,
    }


def _insert_comment(payload, prepared):
    requested = payload["anchors"][0]
    body = {"requests": [{"insertComment": {
        "content": requested["comment"], "range": prepared["range"],
    }}]}
    if prepared["revision_id"]:
        body["writeControl"] = {"requiredRevisionId": prepared["revision_id"]}
    try:
        result = _gws(
            "docs", "documents", "batchUpdate", "--params",
            json.dumps({"documentId": payload["doc_id"]}), "--json", json.dumps(body),
        )
    except RuntimeError as exc:
        raise CommentWriteError(str(exc)) from exc
    replies = result.get("replies") or []
    thread = ((replies[0].get("insertComment") or {}).get("commentThread") or {}) if replies else {}
    post = thread.get("headPost") or {}
    if (result.get("documentId") != payload["doc_id"]
            or result.get("commentUpdateState") != "ALL_SAVED"
            or not thread.get("commentId") or not thread.get("anchorId")
            or post.get("content") != requested["comment"]
            or thread.get("plainTextQuote") != requested["text"]):
        raise CommentWriteError("Docs API did not verify the saved comment and exact anchor")
    return {"ok": True, "doc_id": payload["doc_id"], "results": [{
        "anchor": requested["text"], "comment": requested["comment"],
        "comment_id": thread["commentId"], "quoted_text": thread["plainTextQuote"],
        "status": "anchored",
    }]}


def _ledger_path():
    state = REPO_ROOT / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state / "gdoc-comment-idempotency.json"


def _read_records(handle):
    handle.seek(0)
    raw = handle.read().strip()
    return json.loads(raw) if raw else {}


def _write_records(handle, records):
    handle.seek(0)
    handle.truncate()
    json.dump(records, handle, ensure_ascii=False)
    handle.flush()
    os.fsync(handle.fileno())


def _reserve(payload):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        return None, None
    fingerprint = _request_fingerprint(payload)
    path = _ledger_path()
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        records = _read_records(handle)
        if key in records:
            record = records[key]
            if record.get("fingerprint") != fingerprint:
                raise RuntimeError(
                    f"idempotency key {key!r} belongs to a different Google Doc comment write")
            if record.get("status") == "complete":
                return key, record
            raise RuntimeError(
                f"Google Doc comment write {key!r} has an indeterminate prior attempt; "
                "inspect the document before retrying")
        records[key] = {
            "status": "started", "ts": utc_now(), "fingerprint": fingerprint,
            "doc_id": payload["doc_id"],
        }
        _write_records(handle, records)
    return key, None


def _existing_attempt(payload):
    """Find a prior key before any network preflight, so replays work offline."""
    path = REPO_ROOT / "state" / "gdoc-comment-idempotency.json"
    if not path.exists():
        return None
    key = str(payload["idempotency_key"]).strip()
    with open(path, "r", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        record = _read_records(handle).get(key)
    if not record:
        return None
    if record.get("fingerprint") != _request_fingerprint(payload):
        raise RuntimeError(
            f"idempotency key {key!r} belongs to a different Google Doc comment write")
    if record.get("status") != "complete":
        raise RuntimeError(
            f"Google Doc comment write {key!r} has an indeterminate prior attempt; "
            "inspect the document before retrying")
    return record


def _finish(key, payload, result, *, status):
    if not key:
        return
    path = _ledger_path()
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        records = _read_records(handle)
        record = records[key]
        record.update({"status": status, "updated_at": utc_now()})
        if result is not None:
            record["result"] = result
        _write_records(handle, records)


def _timeline_has_key(directory, key):
    timeline = directory / "timeline.ndjson"
    try:
        for line in timeline.read_text().splitlines():
            try:
                if json.loads(line).get("idempotency_key") == key:
                    return True
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return False


def _log_result(payload, result, key):
    directory = quest_dir(REPO_ROOT, payload["quest_id"])
    if not directory:
        raise RuntimeError(f"quest disappeared before logging: {payload['quest_id']}")
    if key and _timeline_has_key(directory, key):
        return False
    entry = {
        "ts": utc_now(), "event": "gdoc_comments_added", "surface": "google_docs",
        "action": "anchor_comments", "doc_id": payload["doc_id"],
        "comment_ids": [row["comment_id"] for row in result["results"]],
        "message_text": approval_message(payload), "idempotency_key": key or "",
        "result": result,
    }
    if payload.get("approval_id"):
        entry["approval_id"] = payload["approval_id"]
    if payload.get("note"):
        entry["note"] = str(payload["note"])
    append_timeline(directory, entry)
    return True


def execute(payload):
    lock_path = REPO_ROOT / "state" / "gdoc-comment-write.lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key = str(payload["idempotency_key"]).strip()
        prior = _existing_attempt(payload)
        if prior:
            result = dict(prior["result"])
            try:
                logged = _log_result(payload, result, key)
            except Exception as exc:
                raise RuntimeError(
                    "Google Doc comments completed but timeline logging failed; "
                    "retry this exact idempotency key to finish logging") from exc
            result["idempotent_replay"] = True
            result["logged"] = logged
            return result
        prepared = _preflight(payload)
        key, _ = _reserve(payload)
        try:
            result = _insert_comment(payload, prepared)
        except Exception:
            _finish(key, payload, None, status="indeterminate")
            raise
    try:
        _finish(key, payload, result, status="complete")
    except Exception:
        try:
            _finish(key, payload, None, status="indeterminate")
        except Exception:
            pass
        raise
    try:
        logged = _log_result(payload, result, key)
    except Exception as exc:
        raise RuntimeError(
            "Google Doc comments completed but timeline logging failed; "
            "retry this exact idempotency key to finish logging") from exc
    result["idempotent_replay"] = False
    result["logged"] = logged
    return result


def _parse_payload(value):
    return _validated(json.loads(value))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "approval-spec":
            payload = _parse_payload(args[1])
            print(json.dumps(approval_spec(payload), ensure_ascii=False, separators=(",", ":")))
            return 0
        if len(args) != 1:
            raise ValueError("usage: sq gdoc-comment [approval-spec] '<payload_json>'")
        payload = _parse_payload(args[0])
        reason = write_policy_reason(payload)
        if reason:
            raise ValueError(f"write denied: {reason}")
        result = execute(payload)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except CommentWriteError as exc:
        print(f"error: Google Doc comment outcome is indeterminate: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
