import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.cli import _workspace
from sidequestor.dashboard import saved_dashboard_port
from sidequestor.workspace import default_workspace, init_workspace, list_instances, set_default_workspace


class DefaultWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": str(self.root / "config")})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_old_sole_registry_migrates_from_unrelated_directory(self):
        workspace = init_workspace(self.root / "workspace")
        registry = self.root / "config/yaas/instances.json"
        rows = json.loads(registry.read_text())
        rows[0].pop("default")
        registry.write_text(json.dumps(rows))
        with patch("sidequestor.cli._workspace_from_environment", return_value=None), \
                patch("pathlib.Path.cwd", return_value=self.root):
            self.assertEqual(_workspace(None, None), workspace)
        self.assertTrue(list_instances()[0]["default"])

    def test_default_wins_over_cwd_and_explicit_path_overrides_default(self):
        first = init_workspace(self.root / "first")
        second = init_workspace(self.root / "second")
        with patch("sidequestor.cli._workspace_from_environment", return_value=None), \
                patch("pathlib.Path.cwd", return_value=second.root):
            self.assertEqual(_workspace(None, None), first)
            self.assertEqual(_workspace(str(second.root), None), second)

    def test_multiple_old_workspaces_need_selection(self):
        first = init_workspace(self.root / "first")
        second = init_workspace(self.root / "second")
        registry = self.root / "config/yaas/instances.json"
        rows = list_instances()
        for row in rows:
            row.pop("default", None)
        registry.write_text(json.dumps(rows))
        with self.assertRaisesRegex(SystemExit, "select-workspace"):
            default_workspace()
        set_default_workspace(second)
        self.assertEqual(default_workspace(), second)
        self.assertNotEqual(default_workspace(), first)

    def test_deleted_default_is_not_replaced_by_another_workspace(self):
        first = init_workspace(self.root / "first")
        init_workspace(self.root / "second")
        (first.yaas_dir / "instance.json").unlink()
        with self.assertRaisesRegex(SystemExit, "unavailable"):
            default_workspace()

    def test_port_migration_is_sticky_even_after_url_removed(self):
        ws = init_workspace(self.root / "workspace")
        marker = ws.yaas_dir / "instance.json"
        metadata = json.loads(marker.read_text())
        metadata.pop("dashboard_port", None)
        marker.write_text(json.dumps(metadata))
        url = ws.state / "dashboard-url.txt"
        url.write_text("http://127.0.0.1:8879")
        self.assertEqual(saved_dashboard_port(ws), 8879)
        url.unlink()
        self.assertEqual(saved_dashboard_port(ws), 8879)
        self.assertEqual(saved_dashboard_port(ws, 8877), 8877)

    def test_stopped_install_migrates_explicit_port_from_manifest(self):
        ws = init_workspace(self.root / "workspace")
        marker = ws.yaas_dir / "instance.json"
        metadata = json.loads(marker.read_text())
        metadata.pop("dashboard_port", None)
        marker.write_text(json.dumps(metadata))
        manifest = ws.yaas_dir / "launchd/production.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"backend": "production", "instance_id": ws.instance_id, "workspace": str(ws.root),
                                        "jobs": {"dashboard": {"arguments": ["dashboard", "serve", "8878"]}}}))
        self.assertEqual(saved_dashboard_port(ws), 8878)

    def test_new_default_uses_8877(self):
        self.assertEqual(saved_dashboard_port(init_workspace(self.root / "workspace")), 8877)
