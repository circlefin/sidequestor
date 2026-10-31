"""The permanent dispatch owner for workspace work. Call under the tick lock."""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

INBOX_QUEST = "quest-inbox"
CONTEXT = """# Inbox

This permanent quest owns independent one-off work, reaction history, and workspace
health and upgrade reviews. Process only the exact watches in the dispatch.
Follow OPERATING.md for health diagnosis and system review items. Ordinary approval
items follow the quest-dispatch skill. Never complete, retire, or archive Inbox.
If work becomes ongoing, create or adopt a dedicated quest and record its ID here.
"""


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def ensure_inbox(root):
    """Restore the original directory where possible; never replace existing history."""
    quests = Path(root) / "state" / "quests"
    quests.mkdir(parents=True, exist_ok=True)
    qdir = quests / "active" / INBOX_QUEST
    qdir.parent.mkdir(parents=True, exist_ok=True)
    with open(quests / ".quest-inbox.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not qdir.exists():
            for bucket in ("archived", "completed", "retired"):
                old = quests / bucket / INBOX_QUEST
                if old.is_dir():
                    old.rename(qdir)
                    break
        qdir.mkdir(exist_ok=True)
        meta_path = qdir / "meta.json"
        meta = _recover_json(meta_path) or {}
        desired = dict(meta, id=INBOX_QUEST, title="Inbox", status="active", system_role="inbox")
        desired.setdefault("priority", "normal")
        desired.setdefault("allow_send", False)
        desired["retire_slack_threads_after_days"] = "never"
        if desired != meta:
            write_json(meta_path, desired)
        if not (qdir / "context.md").exists():
            (qdir / "context.md").write_text(CONTEXT)
        (qdir / "timeline.ndjson").touch(exist_ok=True)
        watch = qdir / "watch.json"
        doc = _recover_json(watch)
        if doc is not None and (not isinstance(doc.get("watches"), list)
                                or not all(isinstance(w, dict) for w in doc["watches"])):
            watch.rename(watch.with_name(f"watch.json.invalid.{time.time_ns()}"))
            doc = None
        if doc is None:
            write_json(qdir / "watch.json", {"watches": []})
    return qdir


def _recover_json(path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    path.rename(path.with_name(f"{path.name}.invalid.{time.time_ns()}"))
    return None


def log_inbox(root, key, entry):
    """Timeline-backed idempotency: a crash cannot split the receipt and the record."""
    qdir = ensure_inbox(root)
    path = qdir / "timeline.ndjson"
    with open(qdir / ".history.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            last_line = ""
            for line in stream:
                last_line = line
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("system_event_key") == key:
                        return
                except ValueError:
                    continue
            if last_line and not last_line.endswith("\n"):
                stream.write("\n")
            stream.write(json.dumps(dict(entry, system_event_key=key,
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n")


def try_log_inbox(root, key, entry):
    """A history failure must never turn an already completed send into a retry."""
    try:
        log_inbox(root, key, entry)
        return True
    except Exception as exc:
        print(f"warning: Inbox history unavailable ({type(exc).__name__}: {exc}); "
              "action result is unchanged, do not repeat a completed action", file=sys.stderr)
        return False
