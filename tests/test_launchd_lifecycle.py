from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.launchd import (
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

    def test_rekey_refuses_to_orphan_installed_launchd_labels(self) -> None:
        install_production(self.workspace, self.root / "venv" / "bin" / "python")
        with self.assertRaisesRegex(SystemExit, "uninstall Sidequestor launchd jobs"):
            rekey_workspace(self.workspace.root)


if __name__ == "__main__":
    unittest.main()
