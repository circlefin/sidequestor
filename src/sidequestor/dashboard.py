"""Run the unchanged dashboard server from the shadow projection."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .native import RUNTIME_ROOT, _environment
from .workspace import Workspace


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


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
        if process.poll() is None:
            process.terminate()
        try:
            url_file.unlink()
        except FileNotFoundError:
            pass
