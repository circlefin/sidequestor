from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.launchd import (
    LaunchdLifecycleError,
    _alive_processes,
    _bootout_job,
    install as install_shadow,
    install_production,
    production_status,
    stop_production,
    uninstall_production,
)
from sidequestor.workspace import init_workspace, rekey_workspace


class ProductionLaunchdLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-launchd-")
        self.root = Path(self.temp.name)
        self.config_patch = patch.dict(os.environ, {"YAAS_CONFIG_HOME": str(self.root / "config")})
        self.config_patch.start()
        self.workspace = init_workspace(self.root / "workspace")
        self.launch_agents = self.root / "LaunchAgents"
        self.root_patch = patch("sidequestor.launchd._production_root", return_value=self.launch_agents)
        self.run_patch = patch("sidequestor.launchd.subprocess.run")
        self.root_patch.start()
        self.run = self.run_patch.start()
        self.run.side_effect = self._launchctl_absent

    @staticmethod
    def _launchctl_absent(command, **kwargs):
        if command[1] == "print":
            return subprocess.CompletedProcess(command, 1, "", "service not found")
        return subprocess.CompletedProcess(command, 0, "", "")

    def tearDown(self) -> None:
        self.run_patch.stop()
        self.root_patch.stop()
        self.config_patch.stop()
        self.temp.cleanup()

    def test_install_stop_restart_and_uninstall_are_instance_bound(self) -> None:
        python = self.root / "venv" / "bin" / "python"
        manifest = install_production(self.workspace, python)

        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["instance_id"], self.workspace.instance_id)
        self.assertTrue(manifest["running"])
        self.assertEqual(set(manifest["jobs"]), {"triage", "heartbeat", "dashboard"})
        self.assertEqual(manifest["jobs"]["dashboard"]["arguments"][-1], "0")
        self.assertEqual(manifest["jobs"]["triage"]["arguments"][0], str(python))
        self.assertTrue(all(Path(job["plist"]).is_file() for job in manifest["jobs"].values()))

        self.assertTrue(stop_production(self.workspace))
        self.assertFalse(production_status(self.workspace)["running"])
        self.assertFalse((self.workspace.state / "dashboard-url.txt").exists())

        restarted = install_production(self.workspace, python)
        self.assertTrue(restarted["running"])
        self.assertTrue(uninstall_production(self.workspace))
        self.assertIsNone(production_status(self.workspace))
        self.assertFalse(any(self.launch_agents.glob("*.plist")))

        calls = [call.args[0] for call in self.run.call_args_list]
        self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in calls))
        self.assertTrue(any(command[:2] == ["launchctl", "bootout"] for command in calls))

    def test_shadow_install_never_changes_launchd_state(self) -> None:
        self.run.reset_mock()

        manifest = install_shadow(self.workspace, self.root / "venv" / "bin" / "python")

        self.assertEqual(manifest["backend"], "workspace-shadow")
        self.run.assert_not_called()

    def test_status_rejects_a_manifest_copied_from_another_workspace(self) -> None:
        manifest_path = self.workspace.yaas_dir / "launchd" / "production.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({
            "schema": 2,
            "backend": "production",
            "instance_id": self.workspace.instance_id,
            "workspace": str(self.root / "different-workspace"),
            "jobs": {},
        }) + "\n")
        self.assertIsNone(production_status(self.workspace))

    def test_status_accepts_a_legacy_manifest_for_the_same_workspace(self) -> None:
        manifest_path = self.workspace.yaas_dir / "launchd" / "production.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({
            "schema": 1,
            "backend": "production",
            "workspace": str(self.workspace.root),
            "jobs": {},
        }) + "\n")
        self.assertIsNotNone(production_status(self.workspace))

    def test_stop_fails_and_preserves_running_manifest_when_a_job_remains_loaded(self) -> None:
        install_production(self.workspace, self.root / "venv" / "bin" / "python")

        def dashboard_stays_loaded(command, **kwargs):
            if command[1] == "print" and command[-1].endswith(".dashboard"):
                return subprocess.CompletedProcess(command, 0, "loaded", "")
            return self._launchctl_absent(command, **kwargs)

        self.run.side_effect = dashboard_stays_loaded
        with self.assertRaises(LaunchdLifecycleError):
            stop_production(self.workspace)
        self.assertTrue(production_status(self.workspace)["running"])

    def test_bootout_drains_the_original_process_tree_before_returning(self) -> None:
        with patch("sidequestor.launchd._service_root_pid", return_value=101), \
                patch("sidequestor.launchd._process_tree", return_value={101, 102}) as tree, \
                patch("sidequestor.launchd._drain_processes") as drain:
            _bootout_job("501", "com.sidequestor.test")

        tree.assert_called_once_with(101)
        drain.assert_called_once_with({101, 102}, "com.sidequestor.test")

    def test_permission_error_does_not_treat_a_process_as_gone(self) -> None:
        with patch("sidequestor.launchd.os.kill", side_effect=PermissionError):
            self.assertEqual(_alive_processes({101}), {101})

    def test_rekey_refuses_to_orphan_installed_launchd_labels(self) -> None:
        install_production(self.workspace, self.root / "venv" / "bin" / "python")
        with self.assertRaisesRegex(SystemExit, "uninstall Sidequestor launchd jobs"):
            rekey_workspace(self.workspace.root)


if __name__ == "__main__":
    unittest.main()
