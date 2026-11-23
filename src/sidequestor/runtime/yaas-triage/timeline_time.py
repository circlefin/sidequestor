"""Source-message chronology without changing the timeline's recording clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re


# These events describe observed information. Actions, failures and dispatches
# must keep the time Sidequestor did the work, even if they mention old messages.
SOURCE_TIME_EVENTS = frozenset({
    "info_received", "info_logged", "message_received", "email_received",
})


def source_event_time(value, recorded_ts=None):
    """Normalize an explicit source time to UTC, or ignore unusable evidence.

    Numeric values are Unix *seconds*, never IDs or millisecond guesses. ISO
    values must include a timezone. A source event cannot postdate its recording
    second (the logger's clock stamps whole seconds).
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", value):
            seconds = float(value)
            if not math.isfinite(seconds) or seconds <= 0:
                return None
            date = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})", value):
                return None
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None or date.utcoffset() is None:
                return None
            date = date.astimezone(timezone.utc)
            if date.timestamp() <= 0:
                return None
        if recorded_ts is not None:
            recorded = source_event_time(recorded_ts)
            if recorded is None:
                return None
            boundary = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            coarse = isinstance(recorded_ts, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", recorded_ts)
            after_recording = date >= boundary + timedelta(seconds=1) if coarse else date > boundary
            if after_recording:
                return None
        return date.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def display_timestamp(entry):
    """Only explicit, valid information-source times change display chronology."""
    recorded = entry.get("ts") or entry.get("timestamp")
    if recorded is not None and entry.get("event") in SOURCE_TIME_EVENTS:
        source = source_event_time(entry.get("source_event_ts"), recorded)
        if source is not None:
            return source
    return recorded
