import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.launchd import _production_jobs
from sidequestor.resources import ENGINE_VERSION, current_engine_version, sync_resources
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

            content = workspace.env_file.read_text()
            self.assertIn("SIDEQUESTOR_CODEX_MODEL=gpt-5.6-luna", content)
            self.assertIn("SIDEQUESTOR_CODEX_EFFORT=high", content)

    def test_interactive_instructions_target_selected_backend_without_writing(self):
        from contextlib import redirect_stdout
        from io import StringIO

        for agent, filename in (("codex", "AGENTS.md"), ("claude", "CLAUDE.md"), ("cursor", "AGENTS.md")):
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

    def test_sync_resources_creates_workspace_and_claude_skill_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)

            destination = sync_resources(workspace)

            managed_skill = workspace.root / "skills" / "yaas-ops"
            self.assertTrue(managed_skill.is_symlink())
            self.assertEqual(os.readlink(managed_skill), "../.yaas/engine/current/skills/yaas-ops")
            claude_skill = workspace.root / ".claude" / "skills" / "yaas-ops"
            self.assertTrue(claude_skill.is_symlink())
            self.assertEqual(os.readlink(claude_skill), "../../.yaas/engine/current/skills/yaas-ops")
            prompt = workspace.root / ".codex" / "prompts" / "new-quest.md"
            self.assertTrue(prompt.is_file())
            self.assertIn("sq new-quest '<spec_json>'", prompt.read_text())
            self.assertEqual(current_engine_version(workspace), ENGINE_VERSION)
            self.assertTrue((destination / "skills" / "yaas-ops" / "SKILL.md").is_file())

    def test_sync_resources_is_idempotent_and_prunes_stale_skill_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            stale = workspace.root / "skills" / "yaas-stale"
            stale.symlink_to("../.yaas/engine/current/skills/yaas-stale", target_is_directory=True)
            wrong = workspace.root / ".claude" / "skills" / "yaas-ops"
            wrong.unlink()
            wrong.symlink_to("../../broken-target", target_is_directory=True)

            first = sync_resources(workspace)
            second = sync_resources(workspace)

            self.assertEqual(first, second)
            self.assertFalse(stale.exists())
            self.assertEqual(os.readlink(workspace.root / ".claude" / "skills" / "yaas-ops"),
                             "../../.yaas/engine/current/skills/yaas-ops")

    def test_sync_resources_leaves_real_files_in_place(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            blocked = workspace.root / "skills" / "yaas-ops"
            blocked.write_text("user-owned override\n")
            claude_root = workspace.root / ".claude"
            claude_root.mkdir()
            (claude_root / "skills").mkdir()
            blocked_claude = claude_root / "skills" / "yaas-gmail-reply"
            blocked_claude.write_text("custom entry\n")

            sync_resources(workspace)

            self.assertFalse(blocked.is_symlink())
            self.assertEqual(blocked.read_text(), "user-owned override\n")
            self.assertFalse(blocked_claude.is_symlink())
            self.assertEqual(blocked_claude.read_text(), "custom entry\n")

    def test_sync_resources_skips_codex_or_claude_paths_blocked_by_files(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            (workspace.root / ".claude").write_text("blocked\n")
            (workspace.root / ".codex").write_text("blocked\n")

            sync_resources(workspace)

            self.assertFalse((workspace.root / ".claude" / "skills").exists())
            self.assertFalse((workspace.root / ".codex" / "prompts").exists())
            self.assertTrue((workspace.root / "skills" / "yaas-ops").is_symlink())


if __name__ == "__main__":
    unittest.main()
