from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor.cli import _cmd_start, _cmd_stop, _dispatch
from sidequestor.workspace import init_workspace


class CliLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_home = tempfile.TemporaryDirectory(prefix="sidequestor-cli-config-")
        self.config_patch = patch.dict("os.environ", {"YAAS_CONFIG_HOME": self.config_home.name})
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.config_home.cleanup()

    def test_start_reports_the_ready_dashboard_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            manifest = {"jobs": {
                "triage": {"label": "triage"},
                "heartbeat": {"label": "heartbeat"},
                "dashboard": {"label": "dashboard"},
            }}
            output = StringIO()
            with patch("sidequestor.cli.install_production", return_value=manifest), \
                    patch("sidequestor.cli.wait_for_dashboard_url", return_value="http://127.0.0.1:43123"), \
                    redirect_stdout(output):
                self.assertEqual(_cmd_start(workspace), 0)
            self.assertIn("dashboard: http://127.0.0.1:43123", output.getvalue())

    def test_stop_also_stops_a_foreground_dashboard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            output = StringIO()
            with patch("sidequestor.launchd.stop_production", return_value=False), \
                    patch("sidequestor.cli.stop_dashboard_process", return_value=True), \
                    redirect_stdout(output):
                self.assertEqual(_cmd_stop(workspace), 0)
            self.assertIn(f"stopped Sidequestor instance {workspace.instance_id}", output.getvalue())

    def test_tick_syncs_resources_when_engine_version_drifted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            with patch("sidequestor.cli.current_engine_version", return_value="0.1.0.dev0"), \
                    patch("sidequestor.cli.sync_resources") as sync, \
                    patch("sidequestor.cli.run_native", return_value=0) as run_native:
                self.assertEqual(_dispatch("tick", [], str(workspace.root), None), 0)
            sync.assert_called_once_with(workspace)
            run_native.assert_called_once_with(workspace, "yaas-triage/tick.py", [])

    def test_tick_continues_when_drift_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            with patch("sidequestor.cli.current_engine_version", return_value="0.1.0.dev0"), \
                    patch("sidequestor.cli.sync_resources", side_effect=RuntimeError("boom")), \
                    patch("sidequestor.cli.run_native", return_value=0) as run_native:
                self.assertEqual(_dispatch("tick", [], str(workspace.root), None), 0)
            run_native.assert_called_once_with(workspace, "yaas-triage/tick.py", [])


if __name__ == "__main__":
    unittest.main()
