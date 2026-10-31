from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from sidequestor import dashboard
from sidequestor.workspace import init_workspace


class _ProbeSocket:
    def __init__(self, attempts: list[int], occupied: set[int]) -> None:
        self.attempts = attempts
        self.occupied = occupied
        self.port = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, address) -> None:
        self.port = address[1]
        self.attempts.append(self.port)
        if self.port in self.occupied:
            raise OSError("address already in use")

    def getsockname(self):
        return ("127.0.0.1", self.port)

    def setsockopt(self, *_args) -> None:
        pass


class DashboardPortTest(unittest.TestCase):
    def test_fixed_port_probe_detects_real_conflict(self):
        import socket
        try:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("localhost binding forbidden by execution environment")
        with listener:
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(dashboard.wait_for_dashboard_port(port, timeout=0))
        self.assertTrue(dashboard.wait_for_dashboard_port(port, timeout=0))

    def test_owned_dashboard_without_workspace_in_controller_argv(self):
        import subprocess
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", {"YAAS_CONFIG_HOME": raw}):
            ws = init_workspace(Path(raw) / "workspace")
            record = {"instance_id": ws.instance_id, "server_pid": 12345, "controller_pid": 12346,
                      "server_birth": "server-start", "controller_birth": "controller-start"}
            response = subprocess.CompletedProcess([], 0, str(dashboard.RUNTIME_ROOT / "yaas-triage/ops/dashboard-server.py") + " 8877", "")
            with patch.object(dashboard, "_process_birth", side_effect=["server-start", "controller-start"]), \
                    patch.object(dashboard.subprocess, "run", return_value=response):
                self.assertEqual(dashboard._owned_dashboard_pid(record, ws), 12346)
            with patch.object(dashboard, "_process_birth", return_value="reused-pid"):
                self.assertIsNone(dashboard._owned_dashboard_pid(record, ws))

    def test_foreground_dashboard_from_default_workspace_can_be_stopped(self):
        import os
        import socket
        import subprocess
        import sys
        import time
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        except PermissionError:
            self.skipTest("localhost binding forbidden by execution environment")
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"YAAS_CONFIG_HOME": str(Path(raw) / "config")}):
            ws = init_workspace(Path(raw) / "workspace")
            environment = {k: v for k, v in os.environ.items() if not k.startswith(("SIDEQUESTOR_", "YAAS_"))}
            environment.update(YAAS_CONFIG_HOME=str(Path(raw) / "config"))
            with subprocess.Popen([sys.executable, "-m", "sidequestor", "dashboard", "serve", str(port)],
                                  cwd=raw, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                try:
                    deadline = time.monotonic() + 6
                    while not dashboard.read_dashboard_url(ws) and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertIsNotNone(dashboard.read_dashboard_url(ws))
                    self.assertEqual(dashboard.saved_dashboard_port(ws), 8877)
                    self.assertTrue(dashboard.stop_dashboard_process(ws))
                    process.wait(timeout=6)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=6)

    def test_duplicate_foreground_start_does_not_replace_process_record(self):
        from sidequestor.workspace import workspace_lock
        with tempfile.TemporaryDirectory() as raw, patch.dict("os.environ", {"YAAS_CONFIG_HOME": raw}):
            ws = init_workspace(Path(raw) / "workspace")
            record = ws.state / "dashboard-process.json"
            record.write_text("existing-controller")
            with workspace_lock(ws, "dashboard"), patch.object(dashboard, "_serve") as serve:
                self.assertEqual(dashboard.serve(ws), 0)
                serve.assert_not_called()
            self.assertEqual(record.read_text(), "existing-controller")

    def test_reads_the_workspace_specific_dashboard_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-dashboard-port-") as raw, \
                patch.dict("os.environ", {"YAAS_CONFIG_HOME": str(Path(raw) / "config")}):
            workspace = init_workspace(Path(raw) / "workspace")
            (workspace.state / "dashboard-url.txt").write_text("http://127.0.0.1:8878\n")
            self.assertEqual(dashboard.read_dashboard_port(workspace), 8878)

            (workspace.state / "dashboard-url.txt").write_text("http://example.com:8878\n")
            self.assertIsNone(dashboard.read_dashboard_port(workspace))

            (workspace.state / "dashboard-url.txt").write_text("http://127.0.0.1:0\n")
            self.assertIsNone(dashboard.read_dashboard_port(workspace))

    def test_waits_until_the_previous_dashboard_port_can_bind(self) -> None:
        attempts: list[int] = []
        probes = [
            _ProbeSocket(attempts, {8877}),
            _ProbeSocket(attempts, set()),
        ]
        with patch.object(dashboard.socket, "socket", side_effect=probes), \
                patch.object(dashboard.time, "sleep") as sleep:
            self.assertTrue(dashboard.wait_for_dashboard_port(8877, timeout=1))

        self.assertEqual(attempts, [8877, 8877])
        sleep.assert_called_once_with(0.05)


if __name__ == "__main__":
    unittest.main()
