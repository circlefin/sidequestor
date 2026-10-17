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
    def test_automatic_port_scan_starts_at_8877_and_skips_occupied_ports(self) -> None:
        attempts: list[int] = []

        def make_socket(*_args):
            return _ProbeSocket(attempts, {8877, 8878})

        with patch.object(dashboard.socket, "socket", side_effect=make_socket):
            self.assertEqual(dashboard._ephemeral_port(), 8879)

        self.assertEqual(attempts, [8877, 8878, 8879])

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
