import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.launchd import _production_jobs
from sidequestor.resources import sync_resources
from sidequestor.setup import provision_env
from sidequestor.workspace import init_workspace


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.config_home = tempfile.TemporaryDirectory(prefix="sidequestor-config-")
        self.config_patch = patch.dict(os.environ, {"YAAS_CONFIG_HOME": self.config_home.name})
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.config_home.cleanup()

    def test_provisioning_preserves_real_values(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=claude\n"
                "CUSTOM_VALUE=keep-me\n"
            )

            provision_env(workspace, interactive=False)
            content = workspace.env_file.read_text()

            self.assertIn("SIDEQUESTOR_AGENT=claude", content)
            self.assertIn("CUSTOM_VALUE=keep-me", content)
            self.assertIn("SIDEQUESTOR_CLAUDE_MODEL=claude-opus-4-6", content)
            self.assertIn("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0", content)

    def test_production_labels_are_instance_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(Path(raw))
            jobs = _production_jobs(workspace, Path("/usr/bin/python3"))
            self.assertTrue(jobs)
            for job in jobs.values():
                self.assertIn(workspace.instance_id, job["label"])

    def test_launchd_jobs_preserve_venv_interpreter_path(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as tools:
            workspace = init_workspace(Path(raw))
            real_python = Path("/usr/bin/python3").resolve()
            venv_python = Path(tools) / "python"
            venv_python.symlink_to(real_python)
            jobs = _production_jobs(workspace, venv_python)
            for name in ("triage", "dashboard"):
                self.assertEqual(jobs[name]["arguments"][0], str(venv_python))
            self.assertEqual(jobs["dashboard"]["arguments"][-1], "0")


if __name__ == "__main__":
    unittest.main()
