#!/usr/bin/env python3
"""Add verified, text-anchored Google Doc comments through a guarded writer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SKILL_DIR.parent.parent
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


class DriverError(RuntimeError):
    pass


class PrerequisiteError(ValueError):
    """The driver cannot start, so no write was attempted."""


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


def _driver_command():
    configured = os.environ.get("SIDEQUESTOR_GDOC_COMMENT_DRIVER")
    if configured and os.environ.get(
            "SIDEQUESTOR_GDOC_COMMENT_TEST_MODE", "").strip().lower() not in TRUE_VALUES:
        raise DriverError(
            "SIDEQUESTOR_GDOC_COMMENT_DRIVER is available only in explicit test mode")
    driver = Path(configured).expanduser() if configured else SKILL_DIR / "playwright-driver.py"
    if not driver.is_file():
        raise DriverError(f"Google Doc comment driver is not present: {driver}")
    if driver.suffix == ".py":
        return [os.environ.get("SIDEQUESTOR_PYTHON") or sys.executable, str(driver)]
    return [str(driver)]


def _require_driver_prerequisites():
    """Fail before reserving an idempotency key when the browser driver cannot import."""
    try:
        command = _driver_command()
    except DriverError as exc:
        raise PrerequisiteError(str(exc)) from exc
    if os.environ.get("SIDEQUESTOR_GDOC_COMMENT_DRIVER"):
        return
    try:
        completed = subprocess.run(
            [command[0], "-c", "import importlib.util, sys; "
             "sys.exit(0 if importlib.util.find_spec('playwright') else 3)"],
            capture_output=True, timeout=30, env=os.environ,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrerequisiteError(f"cannot check for Playwright: {exc}") from exc
    if completed.returncode:
        raise PrerequisiteError(
            "Playwright is not installed; reinstall Sidequestor with its declared dependencies")


def _run_driver(payload):
    driver_payload = {"doc_id": payload["doc_id"], "anchors": payload["anchors"]}
    completed = subprocess.run(
        _driver_command(), input=json.dumps(driver_payload), text=True,
        capture_output=True, timeout=240, env=os.environ,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise DriverError(detail[-1] if detail else f"driver exited {completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DriverError(f"driver returned invalid JSON: {exc}") from exc
    rows = result.get("results") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or not result.get("ok")
            or result.get("doc_id") != payload["doc_id"]
            or not isinstance(rows, list) or len(rows) != len(payload["anchors"])):
        raise DriverError("driver returned an unverified document or anchor result")
    comment_ids = set()
    for requested, row in zip(payload["anchors"], rows):
        if (not isinstance(row, dict) or row.get("status") != "anchored"
                or not row.get("comment_id") or row.get("anchor") != requested["text"]
                or row.get("quoted_text") != requested["text"]
                or row.get("comment") != requested["comment"]):
            raise DriverError(f"driver returned an unverified anchor result for {requested['text']!r}")
        if row["comment_id"] in comment_ids:
            raise DriverError("driver returned the same comment ID more than once")
        comment_ids.add(row["comment_id"])
    return result


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
    _require_driver_prerequisites()
    key, prior = _reserve(payload)
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
    lock_path = REPO_ROOT / "state" / "gdoc-comment-browser.lock"
    try:
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = _run_driver(payload)
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
    except DriverError as exc:
        print(f"error: Google Doc comment outcome is indeterminate: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
