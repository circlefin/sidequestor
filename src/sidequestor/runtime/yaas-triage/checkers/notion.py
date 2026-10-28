"""Polling-only Notion page/block/comment-tree watches.

notion_page covers root properties/content, nested blocks and their comments.
notion_data_source enrolls all current/new rows with the same coverage.
notion_comments covers comments in the selected block/page tree only.
Subpages default to depth 0. include_subpage_content and
include_subpage_comments independently enable traversal up to subpage_depth.
See notion_tree.py for resumable traversal, limits and acknowledgement-safe
local delivery cursors; these cursors are NOT Notion edit timestamps.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import result

SURFACE = os.environ.get("NOTION_CALL", str(Path(__file__).resolve().parents[1] / "surfaces/notion-call.py"))


class Misconfig(Exception):
    pass


class Transient(Exception):
    retry_after = 60


class ResourceUnavailable(Misconfig):
    unavailable_resource = True


def resource_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Misconfig("Notion resource must be a UUID, not a URL") from exc


def call(method, path, body=None, timeout=35):
    args = [sys.executable, SURFACE, method, path]
    if body is not None:
        args.append(json.dumps(body, separators=(",", ":")))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        failure = Transient("Notion request timed out")
        failure.local_timeout = True
        raise failure from exc
    except OSError as exc:
        raise Misconfig("cannot launch Notion bridge") from exc
    if proc.returncode == 4:
        import re
        message = proc.stderr.strip()[:250] or "Notion transient failure"
        match = re.search(r"retry_after=(\d+)", message)
        exc = Transient(message)
        exc.retry_after = int(match[1]) if match else 60
        raise exc
    if proc.returncode == 5:
        raise ResourceUnavailable(proc.stderr.strip()[:250] or "Notion resource/query unavailable")
    if proc.returncode in (1, 3):
        raise Misconfig(proc.stderr.strip()[:250] or "Notion credential/query invalid")
    if proc.returncode:
        raise RuntimeError("Notion bridge failed")
    data = json.loads(proc.stdout)
    if not isinstance(data, dict) or data.get("object") == "error":
        raise RuntimeError("invalid Notion response")
    return data


def run(entry, mode, now=None, store_dir=None):
    import notion_tree
    try:
        return notion_tree.run(entry, mode, api=call, now=now, root=store_dir)
    except notion_tree.ConfigError as exc:
        raise Misconfig(str(exc)) from exc


def cli(mode):
    try:
        entry = json.loads(sys.argv[1])
        if not isinstance(entry, dict):
            raise Misconfig("watch must be an object")
        run(entry, mode)
    except Transient as exc:
        result.ratelimited(str(exc))
    except (Misconfig, IndexError, json.JSONDecodeError) as exc:
        result.misconfig(str(exc))
    except Exception as exc:
        result.error(f"{type(exc).__name__}: invalid Notion response/check")
