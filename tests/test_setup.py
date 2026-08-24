import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.launchd import _production_jobs
from sidequestor.resources import sync_resources
from sidequestor.setup import print_worker_instructions, provision_env
from sidequestor.workspace import init_workspace
from sidequestor.workspace import list_instances


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

    def test_init_does_not_create_backend_instruction_files(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            self.assertFalse((workspace.root / "CLAUDE.md").exists())
            self.assertFalse((workspace.root / "AGENTS.md").exists())

    def test_codex_provisioning_uses_supported_default_model(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
            )

            provision_env(workspace, interactive=False)

            self.assertIn("SIDEQUESTOR_CODEX_MODEL=gpt-5.6-luna", workspace.env_file.read_text())

    def test_interactive_instructions_target_selected_backend_without_writing(self):
        from contextlib import redirect_stdout
        from io import StringIO

        for agent, filename in (("codex", "AGENTS.md"), ("claude", "CLAUDE.md")):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as raw:
                workspace = init_workspace(raw)
                output = StringIO()
                with redirect_stdout(output):
                    print_worker_instructions(agent)

                self.assertIn(filename, output.getvalue())
                self.assertIn("Before acting on a triage dispatch", output.getvalue())
                self.assertIn("Engine-managed skills are installed", output.getvalue())
                self.assertFalse((workspace.root / "AGENTS.md").exists())
                self.assertFalse((workspace.root / "CLAUDE.md").exists())

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

    def test_canonical_config_home_precedes_legacy_alias(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as canonical:
            with patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": canonical}):
                workspace = init_workspace(Path(raw))
                self.assertEqual(list_instances()[0]["instance_id"], workspace.instance_id)
                self.assertTrue((Path(canonical) / "yaas" / "instances.json").is_file())


if __name__ == "__main__":
    unittest.main()
