import tempfile
import unittest
from pathlib import Path

from sidequestor.launchd import _production_jobs
from sidequestor.resources import sync_resources
from sidequestor.setup import provision_env
from sidequestor.workspace import init_workspace


class SetupTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
