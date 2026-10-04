from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor.cli import _cmd_credentials, _cmd_start, _cmd_stop, _dispatch
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

    def test_start_can_pin_the_dashboard_to_its_previous_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            manifest = {"jobs": {}}
            with patch("sidequestor.cli.install_production", return_value=manifest) as install, \
                    patch("sidequestor.cli.wait_for_dashboard_url", return_value=None), \
                    redirect_stdout(StringIO()):
                self.assertEqual(_cmd_start(workspace, ["--dashboard-port", "8877"]), 0)
            install.assert_called_once_with(workspace, Path(sys.executable), 8877)

    def test_start_repairs_keychain_helper_before_starting_slack_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            workspace.env_file.write_text("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=1\n")
            with patch("sidequestor.cli.run_native", return_value=0) as run_native, \
                    patch("sidequestor.cli.install_production", return_value={"jobs": {}}), \
                    patch("sidequestor.cli.wait_for_dashboard_url", return_value=None), \
                    redirect_stdout(StringIO()):
                self.assertEqual(_cmd_start(workspace), 0)
            run_native.assert_called_once_with(
                workspace,
                "yaas-triage/surfaces/slack_credentials.py",
                ["repair-keychain", "--quiet"],
            )

    def test_start_leaves_jobs_stopped_when_keychain_migration_needs_attention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            workspace.env_file.write_text("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=1\n")
            with patch("sidequestor.cli.run_native", return_value=5), \
                    patch("sidequestor.cli.install_production") as install, \
                    redirect_stdout(StringIO()):
                self.assertEqual(_cmd_start(workspace), 5)
            install.assert_not_called()

    def test_credentials_repair_stops_and_restarts_on_the_same_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            with patch("sidequestor.cli.production_status",
                       return_value={"running": True}), \
                    patch("sidequestor.cli.read_dashboard_port", return_value=9123), \
                    patch("sidequestor.cli.stop_production", return_value=True), \
                    patch("sidequestor.cli.run_native", return_value=0), \
                    patch("sidequestor.cli.wait_for_dashboard_port", return_value=True), \
                    patch("sidequestor.cli._cmd_start", return_value=0) as start, \
                    redirect_stdout(StringIO()):
                self.assertEqual(_cmd_credentials(workspace, ["repair-keychain"]), 0)
            start.assert_called_once_with(workspace, ["--dashboard-port", "9123"])

    def test_credentials_repair_failure_leaves_a_running_instance_stopped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            with patch("sidequestor.cli.production_status",
                       return_value={"running": True}), \
                    patch("sidequestor.cli.read_dashboard_port", return_value=9123), \
                    patch("sidequestor.cli.stop_production", return_value=True), \
                    patch("sidequestor.cli.run_native", return_value=2), \
                    patch("sidequestor.cli._cmd_start") as start, \
                    redirect_stdout(StringIO()):
                self.assertEqual(_cmd_credentials(workspace, ["repair-keychain"]), 2)
            start.assert_not_called()

    def test_credentials_status_is_read_only_and_does_not_stop_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            with patch("sidequestor.cli.run_native", return_value=0) as run_native, \
                    patch("sidequestor.cli.stop_production") as stop:
                self.assertEqual(_cmd_credentials(workspace, ["status"]), 0)
            run_native.assert_called_once_with(
                workspace,
                "yaas-triage/surfaces/slack_credentials.py",
                ["helper-status"],
            )
            stop.assert_not_called()

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

    def test_gdoc_comment_routes_to_the_guarded_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            payload = '{"quest_id":"review-doc"}'
            with patch("sidequestor.cli.run_native", return_value=0) as run_native:
                self.assertEqual(
                    _dispatch("gdoc-comment", [payload], str(workspace.root), None), 0)
            run_native.assert_called_once_with(
                workspace,
                "yaas-triage/skills/yaas-gdoc-anchored-comments/gdoc-comment.py",
                [payload],
            )


if __name__ == "__main__":
    unittest.main()
