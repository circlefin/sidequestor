"""Shared bounded-history logic for Telegram user-session checkers."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import result


SURFACE = os.environ.get(
    "TELEGRAM_CALL",
    str(Path(__file__).resolve().parents[1] / "surfaces" / "telegram-call.py"),
)
DEFAULT_LIMIT = 100
# Catch up incrementally so an old high-volume watch cannot repeat the same deep
# binary search forever without committing a safe prefix.
MAX_WINDOW_SECONDS = 24 * 60 * 60


class Transient(Exception):
    pass


class Misconfig(Exception):
    pass


def _python():
    return os.environ.get("SIDEQUESTOR_PYTHON") or os.environ.get("YAAS_PYTHON") or sys.executable


def _call(params):
    try:
        proc = subprocess.run(
            [_python(), SURFACE, json.dumps(params, separators=(",", ":"))],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired as exc:
        raise Transient("Telegram query timed out") from exc
    except OSError as exc:
        raise Misconfig(f"cannot launch Telegram helper with {_python()!r}: {exc}") from exc
    if proc.returncode == 4:
        raise Transient((proc.stderr or "Telegram transient failure").strip()[:250])
    if proc.returncode in (1, 3):
        raise Misconfig((proc.stderr or "Telegram credential/query is invalid").strip()[:250])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError((proc.stderr or f"telegram-call exit {proc.returncode}").strip()[:250])
    value = json.loads(proc.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        raise RuntimeError("telegram-call returned an invalid response")
    return value


def _string_list(entry, key):
    value = entry.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, (str, int)) for item in value):
        raise Misconfig(f"{key} must be a list of strings or integers")
    return [str(item) for item in value]


def lag_for(watch_type):
    try:
        value = int(Path(__file__).with_name(f"{watch_type}.lag").read_text().strip())
    except (OSError, ValueError) as exc:
        raise Misconfig(f"{watch_type}.lag is missing or invalid") from exc
    return value


def _timestamp(message):
    value = message.get("ts")
    if value is None or value == "":
        raise RuntimeError("Telegram message is missing its timestamp")
    return float(value)


def _matches(message, sender_ids, kinds, keywords, include_outgoing):
    if not include_outgoing and message.get("outgoing"):
        return False
    if sender_ids and str(message.get("sender_id", "")) not in sender_ids:
        return False
    if kinds and str(message.get("kind", "")).lower() not in kinds:
        return False
    text = str(message.get("text", ""))
    return not keywords or any(keyword in text.lower() for keyword in keywords)


def _preview(message):
    return f"{message.get('sender_id') or '?'}: [{message.get('kind', 'message')}]"


def run(entry, query=None, now=None, lag=0):
    peer = str(entry["peer"])
    try:
        since = float(entry.get("last_checked_ts") or 0)
        limit = int(entry.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise Misconfig("last_checked_ts and limit must be numeric") from exc
    if since <= 0:
        raise Misconfig("last_checked_ts must be greater than zero")
    if not 1 <= limit <= 500:
        raise Misconfig("limit must be between 1 and 500")
    sender_ids = set(_string_list(entry, "filter_sender_ids"))
    kinds = {value.lower() for value in _string_list(entry, "filter_kinds")}
    keywords = [value.lower() for value in _string_list(entry, "filter_keywords") if value]
    include_outgoing = entry.get("include_outgoing", False)
    if not isinstance(include_outgoing, bool):
        raise Misconfig("include_outgoing must be a boolean")
    # Telegram message dates have whole-second precision. Never commit a
    # fractional watermark or the later part of that second becomes invisible.
    ceiling = int(float(now if now is not None else time.time()) - max(0, lag))
    ceiling = min(ceiling, int(since) + MAX_WINDOW_SECONDS)
    if ceiling <= since:
        result.counted(0, "", advance_to=since, complete=True)
        return
    params = {"credential_id": entry.get("credential_id", "default"), "peer": peer,
              "after_ts": since, "before_ts": ceiling, "limit": limit}
    if query:
        params["query"] = query
    if entry.get("from_user"):
        params["from_user"] = str(entry["from_user"])
    page = _call(params)
    if not page.get("complete"):
        if page.get("permanent"):
            raise Misconfig(str(page.get("reason") or "Telegram window cannot be drained"))
        result.emit("hold", complete=False,
                    reason=str(page.get("reason") or "Telegram window was not drained"))
        return
    try:
        advance_to = float(page["advance_to"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("telegram-call omitted a valid advance_to") from exc
    if not since < advance_to <= ceiling:
        raise RuntimeError("telegram-call returned an unsafe advance_to")
    rows = [row for row in page["messages"] if since < _timestamp(row) <= advance_to]
    matched = sorted(
        (row for row in rows if _matches(
            row, sender_ids, kinds, keywords, include_outgoing)),
        key=lambda row: (_timestamp(row), int(row["id"])),
    )
    newest = matched[-1] if matched else None
    result.counted(len(matched), _preview(newest) if newest else "",
                   advance_to=advance_to, complete=True)


def cli(entry, query=None, lag=0):
    try:
        run(entry, query=query, lag=lag)
    except Transient as exc:
        result.ratelimited(str(exc))
    except Misconfig as exc:
        result.misconfig(str(exc))
    except Exception as exc:
        result.error(f"{type(exc).__name__}: {exc}")
