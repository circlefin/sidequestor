"""Run the unchanged dashboard server from the shadow projection."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .native import RUNTIME_ROOT, _environment
from .workspace import Workspace


DASHBOARD_READY_TIMEOUT = 6.0
DASHBOARD_PORT_START = 8877


def _dashboard_process_file(workspace: Workspace) -> Path:
    return workspace.state / "dashboard-process.json"


def read_dashboard_url(workspace: Workspace) -> str | None:
    try:
        value = (workspace.state / "dashboard-url.txt").read_text().strip()
    except OSError:
        return None
    return value or None


def wait_for_dashboard_url(
    workspace: Workspace, timeout: float = DASHBOARD_READY_TIMEOUT,
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        url = read_dashboard_url(workspace)
        if url:
            return url
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _dashboard_process_matches(pid: int, workspace: Workspace) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    command = (result.stdout or "").strip()
    return result.returncode == 0 and str(workspace.root) in command and "dashboard" in command


def stop_dashboard_process(workspace: Workspace) -> bool:
    """Stop only a foreground dashboard controller owned by this workspace."""
    process_file = _dashboard_process_file(workspace)
    try:
        record = json.loads(process_file.read_text())
        pid = int(record["controller_pid"])
    except (OSError, ValueError, KeyError, TypeError):
        process_file.unlink(missing_ok=True)
        return False
    if pid == os.getpid() or not _dashboard_process_matches(pid, workspace):
        process_file.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        process_file.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process_file.unlink(missing_ok=True)
    (workspace.state / "dashboard-url.txt").unlink(missing_ok=True)
    return True


def _ephemeral_port() -> int:
    """Return the first available loopback port at or above the default."""
    for port in range(DASHBOARD_PORT_START, 65536):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except PermissionError:
            # Preserve the existing diagnostic so callers can distinguish a
            # sandbox or host policy from ordinary port occupancy.
            raise
        except OSError:
            continue
    raise OSError(f"no available dashboard port from {DASHBOARD_PORT_START} upward")


def serve(workspace: Workspace, port: int = 8877) -> int:
    server = RUNTIME_ROOT / "yaas-triage" / "ops" / "dashboard-server.py"
    if not server.is_file():
        raise SystemExit("packaged dashboard server is not present")

    actual_port = port or _ephemeral_port()
    url = f"http://127.0.0.1:{actual_port}"
    url_file = workspace.state / "dashboard-url.txt"
    environment = _environment(workspace)
    process = subprocess.Popen(
        [sys.executable, str(server), str(actual_port)],
        cwd=workspace.root,
        env=environment,
    )
    process_file = _dashboard_process_file(workspace)
    process_file.write_text(json.dumps({
        "controller_pid": os.getpid(),
        "server_pid": process.pid,
        "workspace": str(workspace.root),
    }) + "\n")
    previous_handlers = {}

    def terminate_child(signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, terminate_child)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return process.returncode
            try:
                with socket.create_connection(("127.0.0.1", actual_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.01)
        else:
            process.terminate()
            return 1
        # Publish readiness only after the unchanged server has actually bound its socket.
        url_file.write_text(url + "\n")
        print(f"Sidequestor dashboard -> {url} (loopback only)", flush=True)
        return process.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if process.poll() is None:
            process.terminate()
        try:
            url_file.unlink()
        except FileNotFoundError:
            pass
        try:
            record = json.loads(process_file.read_text())
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("controller_pid") == os.getpid():
            process_file.unlink(missing_ok=True)
