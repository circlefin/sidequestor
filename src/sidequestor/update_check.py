"""Tell the user when a newer Sidequestor release is on PyPI.

Run by the heartbeat job, in the background, at most once a day per workspace. It only
reads PyPI and writes ``state/update-status.json``; the dashboard banner and ``sq doctor``
read that file and never touch the network themselves. Upgrading stays a user action
(``sq upgrade``), because an upgrade restarts the workspace's jobs.

Kept out of ``health-monitor.py`` on purpose: that monitor promises to make no network
calls, so a stalled DNS lookup or proxy here can never delay a liveness alert.

Every failure is quiet: offline, a corporate proxy, an unexpected PyPI response, or a
version string this module cannot compare all mean "no notice", never an error surface.
Set ``SIDEQUESTOR_UPDATE_CHECK=0`` to turn the check off.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__

PYPI_URL = "https://pypi.org/pypi/sidequestor/json"
CHECK_INTERVAL_SEC = 24 * 3600
RETRY_AFTER_ERROR_SEC = 6 * 3600
FETCH_TIMEOUT_SEC = 10
# Hard ceiling for the whole run. urllib's timeout does not bound DNS resolution.
RUN_DEADLINE_SEC = 60
STATUS_NAME = "update-status.json"
_OFF = {"0", "false", "no", "off"}
_RELEASE = re.compile(r"^\d+(\.\d+)*$")


def parse_version(value: object) -> tuple[int, ...] | None:
    """A plain release version as a tuple, or None for anything else (pre-releases,
    local or dev builds), which this module never compares."""
    text = str(value or "").strip()
    if not _RELEASE.match(text) or len(text) > 64:
        return None
    parts = [int(p) for p in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: object, installed: object) -> bool:
    new, cur = parse_version(latest), parse_version(installed)
    return new is not None and cur is not None and new > cur


def status_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / "state" / STATUS_NAME


def read_status(workspace_root: Path) -> dict:
    try:
        data = json.loads(status_path(workspace_root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def available_update(status: dict, installed: str = __version__) -> dict | None:
    """The update to offer, judged against the version running now rather than the one
    recorded at check time, so a finished upgrade clears the notice immediately."""
    latest = status.get("latest")
    if not is_newer(latest, installed):
        return None
    return {"installed": installed, "latest": str(latest),
            "url": f"https://pypi.org/project/sidequestor/{latest}/",
            "command": "sq upgrade"}


def check_enabled(env: dict) -> bool:
    value = env.get("SIDEQUESTOR_UPDATE_CHECK", env.get("YAAS_UPDATE_CHECK", "1"))
    return str(value).strip().lower() not in _OFF


def check_status(workspace_root: Path, installed: str = __version__) -> dict:
    """Safe dashboard feedback, without exposing proxy URLs or raw network errors."""
    status = read_status(workspace_root)
    update = available_update(status, installed)
    return {"status": "failed" if status.get("error") else "available" if update else
            "current" if status.get("latest") else "unknown",
            "last_attempt_ts": status.get("last_attempt_ts", 0),
            "checked_utc": status.get("checked_utc"), "update": update}


def fetch_latest(url: str = PYPI_URL, timeout: float = FETCH_TIMEOUT_SEC) -> str:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": f"sidequestor/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(2_000_000))
    version = data["info"]["version"]
    if parse_version(version) is None:
        raise ValueError(f"unexpected version from PyPI: {version!r}")
    return str(version)


def notify_desktop(update: dict) -> None:
    title = "Sidequestor update available"
    subtitle = f"Version {update['latest']} is out (you have {update['installed']})"
    body = "Run `sq upgrade` in your workspace to install it."
    script = (f"display notification {json.dumps(body)} with title {json.dumps(title)} "
              f"subtitle {json.dumps(subtitle)}")
    # check=True: a failed delivery raises, so the version is not recorded as notified and
    # the next daily check tries again.
    subprocess.run(["osascript", "-e", script], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, indent=2) + "\n")
    os.replace(tmp, path)


def run_check(
    workspace_root: Path,
    env: dict,
    *,
    now: float | None = None,
    installed: str = __version__,
    fetch: Callable[[str], str] | None = None,
    notify: Callable[[dict], None] | None = None,
    force: bool = False,
) -> dict | None:
    """Refresh the cached status when it is due, and notify once per new version.

    Returns the status written, or None when nothing ran (disabled, not due, or another
    run holds the lock).
    """
    if not check_enabled(env):
        return None
    now = time.time() if now is None else now
    path = status_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{STATUS_NAME}.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        status = read_status(workspace_root)
        try:
            due = float(status.get("next_check_ts", 0)) <= now
        except (TypeError, ValueError):
            due = True
        if force:
            try:
                due = now - float(status.get("last_attempt_ts", 0)) >= 60
            except (TypeError, ValueError):
                due = True
        if not due:
            return None
        url = env.get("SIDEQUESTOR_UPDATE_CHECK_URL") or PYPI_URL
        status.update({"installed": installed,
                       "last_attempt_ts": now,
                       "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))})
        try:
            status["latest"] = (fetch or fetch_latest)(url)
            status["error"] = None
            status["next_check_ts"] = now + CHECK_INTERVAL_SEC
        except Exception as exc:
            # Keep the last known `latest`: a failed check says nothing about releases.
            status["error"] = f"{type(exc).__name__}: {exc}"[:300]
            status["next_check_ts"] = now + RETRY_AFTER_ERROR_SEC
        update = available_update(status, installed)
        if update and notify and status.get("notified_version") != update["latest"]:
            try:
                notify(update)
                status["notified_version"] = update["latest"]
            except Exception:
                pass
        _write_status(path, status)
        return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sidequestor.update_check")
    parser.add_argument("--workspace", default=os.environ.get("SIDEQUESTOR_WORKSPACE")
                        or os.environ.get("YAAS_WORKSPACE"))
    parser.add_argument("--notify", action="store_true", help="send a desktop notification")
    parser.add_argument("--force", action="store_true", help="check now (limited to once a minute)")
    args = parser.parse_args(argv)
    if not args.workspace:
        return 0
    signal.alarm(RUN_DEADLINE_SEC)
    from .native import _load_workspace_env
    from .workspace import load_workspace

    try:
        workspace = load_workspace(args.workspace)
    except SystemExit:
        return 0
    env = _load_workspace_env(dict(os.environ), workspace)
    run_check(workspace.root, env, notify=notify_desktop if args.notify else None, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
