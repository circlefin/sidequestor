"""Run the unchanged dashboard server from the shadow projection."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .native import RUNTIME_ROOT, _environment
from .workspace import Workspace, workspace_lock, _read_json, _write_json


DASHBOARD_READY_TIMEOUT = 6.0
DASHBOARD_PORT_START = 8877
DASHBOARD_PORT_RELEASE_TIMEOUT = 60.0


def _dashboard_process_file(workspace: Workspace) -> Path:
    return workspace.state / "dashboard-process.json"


def read_dashboard_url(workspace: Workspace) -> str | None:
    try:
        value = (workspace.state / "dashboard-url.txt").read_text().strip()
    except OSError:
        return None
    return value or None


def read_dashboard_port(workspace: Workspace) -> int | None:
    """Return this workspace's published loopback dashboard port, if valid."""
    url = read_dashboard_url(workspace)
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    return port if port is not None and 1 <= port <= 65535 else None


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


def wait_for_dashboard_port(
    port: int, timeout: float = DASHBOARD_PORT_RELEASE_TIMEOUT,
) -> bool:
    """Wait until the dashboard can bind its previous loopback port again."""
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid dashboard port: {port}")
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                # Match dashboard-server.py so a released socket in TIME_WAIT does not
                # unnecessarily force the instance onto another port.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            return True
        except PermissionError:
            raise
        except OSError:
            if time.monotonic() >= deadline:
                return False
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


def _process_birth(pid: int) -> str:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                check=False, capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _owned_dashboard_pid(record, workspace):
    if record.get("instance_id") != workspace.instance_id:
        return None
    try:
        server_pid = int(record["server_pid"])
        controller_pid = int(record["controller_pid"])
        birth = record.get("server_birth")
        if not birth or _process_birth(server_pid) != birth:
            return None
        result = subprocess.run(["ps", "-p", str(server_pid), "-o", "command="],
                                check=False, capture_output=True, text=True, timeout=2)
        server = str(RUNTIME_ROOT / "yaas-triage/ops/dashboard-server.py")
        if result.returncode or server not in result.stdout:
            return None
        if record.get("controller_birth") and _process_birth(controller_pid) == record["controller_birth"]:
            return controller_pid
        return server_pid  # The controller died, but its verified child still owns the port.
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def stop_dashboard_process(workspace: Workspace) -> bool:
    """Stop only a foreground dashboard controller owned by this workspace."""
    process_file = _dashboard_process_file(workspace)
    try:
        record = json.loads(process_file.read_text())
        pid = int(record["controller_pid"])
    except (OSError, ValueError, KeyError, TypeError):
        process_file.unlink(missing_ok=True)
        return False
    if record.get("server_birth"):
        pid = _owned_dashboard_pid(record, workspace)
        matches = pid is not None
    else:
        matches = _dashboard_process_matches(pid, workspace)
    if pid == os.getpid() or not matches:
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


def saved_dashboard_port(workspace: Workspace, requested: int | None = None) -> int:
    """Migrate a published URL once, then retain the address across stop/start."""
    with workspace_lock(workspace, "metadata"):
        marker = workspace.yaas_dir / "instance.json"
        data = _read_json(marker)
        if data.get("instance_id") != workspace.instance_id:
            raise SystemExit("Workspace identity changed; dashboard settings were not modified.")
        from .launchd import production_status
        manifest = production_status(workspace) or {}
        arguments = manifest.get("jobs", {}).get("dashboard", {}).get("arguments", [])
        previous_port = None
        if arguments:
            try:
                candidate = int(arguments[-1])
                if 1 <= candidate <= 65535:
                    previous_port = candidate
            except (ValueError, TypeError):
                pass
        port = requested or data.get("dashboard_port") or read_dashboard_port(workspace) or previous_port or 8877
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SystemExit("Invalid saved dashboard port; run sq start --dashboard-port 8877")
        if data.get("dashboard_port") != port:
            data["dashboard_port"] = port
            _write_json(marker, data)
        return port


def serve(workspace: Workspace, port: int = 0) -> int:
    try:
        with workspace_lock(workspace, "dashboard", blocking=False):
            return _serve(workspace, port)
    except BlockingIOError:
        print(f"Dashboard is already running or starting for {workspace.root}.")
        return 0


def _serve(workspace: Workspace, port: int = 0) -> int:
    server = RUNTIME_ROOT / "yaas-triage" / "ops" / "dashboard-server.py"
    if not server.is_file():
        raise SystemExit("packaged dashboard server is not present")

    actual_port = port or saved_dashboard_port(workspace)
    if not wait_for_dashboard_port(actual_port, timeout=0):
        raise SystemExit(f"Dashboard port {actual_port} is occupied. Stop the existing instance or application; Sidequestor will not change ports.")
    url = f"http://127.0.0.1:{actual_port}"
    url_file = workspace.state / "dashboard-url.txt"
    # The server reloads .env per request; do not freeze workspace settings into
    # its inherited environment and accidentally turn them into overrides.
    environment = _environment(workspace, load_dotenv=False)
    ready_directory = tempfile.TemporaryDirectory(prefix="sidequestor-dashboard-ready-")
    ready_file = Path(ready_directory.name) / "ready"
    environment["SIDEQUESTOR_DASHBOARD_READY_FILE"] = str(ready_file)
    try:
        process = subprocess.Popen(
            [sys.executable, str(server), str(actual_port)],
            cwd=workspace.root,
            env=environment,
        )
    except BaseException:
        ready_directory.cleanup()
        raise
    process_file = _dashboard_process_file(workspace)
    process_file.write_text(json.dumps({
        "controller_pid": os.getpid(),
        "server_pid": process.pid,
        "instance_id": workspace.instance_id,
        "controller_birth": _process_birth(os.getpid()),
        "server_birth": _process_birth(process.pid),
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
            if ready_file.is_file():
                break
            time.sleep(0.01)
        else:
            process.terminate()
            return 1
        # Publish readiness only after the unchanged server has actually bound its socket.
        url_file.write_text(url + "\n")
        print(f"Sidequestor dashboard -> {url} (loopback only)", flush=True)
        return process.wait()
    finally:
        ready_directory.cleanup()
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
