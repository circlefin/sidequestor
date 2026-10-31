"""Small persisted Slack MCP read cooldown; never changes queue items or cursors."""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time

FALLBACK_SECONDS = 60
READ_TOOLS = frozenset({'slack_read_channel', 'slack_read_thread',
                        'slack_search_public_and_private'})


def workspace_root():
    value = os.environ.get('SIDEQUESTOR_WORKSPACE') or os.environ.get('YAAS_WORKSPACE')
    if not value:
        return None
    root = Path(value).expanduser()
    return root if root.is_dir() else None


def retry_seconds(headers, now):
    """Honor positive provider deadlines, with a fallback for unusable headers."""
    value = next((str(v).strip() for k, v in (headers or {}).items()
                  if str(k).lower() == 'retry-after'), '')
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return FALLBACK_SECONDS
    return seconds if math.isfinite(seconds) and seconds > 0 and math.isfinite(now + seconds) and now + seconds < 253402300799 else FALLBACK_SECONDS


def _path(root):
    return Path(root) / 'state/triage/slack-read-cooldown.json'


def read_deadline(root, now=None):
    if root is None:
        return 0.0
    now = time.time() if now is None else now
    try:
        data = json.loads(_path(root).read_text())
        deadline = float(data['retry_at'])
        if not math.isfinite(deadline) or deadline >= 253402300799:
            raise ValueError('non-finite deadline')
        return deadline if deadline > now else 0.0
    except FileNotFoundError:
        return 0.0
    except (OSError, ValueError, TypeError, KeyError):
        logging.getLogger(__name__).warning('Ignoring unreadable Slack read cooldown')
        return 0.0


def record_cooldown(root, headers, now=None):
    now = time.time() if now is None else now
    deadline = now + retry_seconds(headers, now)
    if root is None:
        return deadline
    path = _path(root)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            deadline = max(deadline, read_deadline(root, now))
            fd, temporary = tempfile.mkstemp(prefix='.slack-cooldown-', dir=path.parent)
            with os.fdopen(fd, 'w') as output:
                json.dump({'retry_at': deadline}, output)
                output.write('\n')
            os.replace(temporary, path)
            temporary = None
    except OSError:
        logging.getLogger(__name__).warning('Could not persist Slack read cooldown')
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return deadline


def detail(deadline, now=None):
    from datetime import datetime
    now = time.time() if now is None else now
    return 'slack ratelimited; read queue held; ' + json.dumps({
        'retry_after_seconds': max(0, math.ceil(deadline - now)),
        'retry_at': deadline,
        'retry_at_utc': datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
    })
