import os
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.resources import files
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidequestor.launchd import _production_jobs
from sidequestor.resources import ENGINE_VERSION, current_engine_version, sync_resources
from sidequestor.setup import check_worker_clis, print_worker_instructions, provision_env, run_setup
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
            self.assertIn("SIDEQUESTOR_CODEX_PERMISSION_MODE=bypassPermissions", content)

    def test_interactive_empty_permission_answer_uses_bypass_default(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=claude\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
            )
            answers = iter(["", "", ""])

            provision_env(workspace, input_fn=lambda _prompt: next(answers), interactive=True)

            self.assertIn("SIDEQUESTOR_CLAUDE_PERMISSION_MODE=bypassPermissions",
                          workspace.env_file.read_text())

    def test_existing_permission_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
                "SIDEQUESTOR_CODEX_PERMISSION_MODE=workspace-write\n"
            )
            provision_env(workspace, interactive=False)
            self.assertIn("SIDEQUESTOR_CODEX_PERMISSION_MODE=workspace-write",
                          workspace.env_file.read_text())

    def test_cli_check_installs_missing_cli_and_rechecks_job_path(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            found = {"codex": None, "claude": "/bin/claude", "cursor": None}
            prompts = []

            def answer(prompt):
                prompts.append(prompt)
                return "yes"

            def install(*_args, **_kwargs):
                found["codex"] = "/bin/codex"
                return SimpleNamespace(returncode=0)

            with patch("sidequestor.setup.resolve_job_path", return_value=SimpleNamespace(value="/bin")), \
                 patch("sidequestor.setup.find_agent", side_effect=lambda name, _path: found[name]), \
                 patch("sidequestor.setup.shutil.which", return_value="/bin/npm"), \
                 patch("sidequestor.setup.subprocess.run", side_effect=install) as run, \
                 redirect_stdout(StringIO()) as output:
                ready = check_worker_clis(workspace, Path("/usr/bin/python3"), agent="codex",
                                          input_fn=answer)

            self.assertTrue(ready)
            self.assertEqual(len(prompts), 1)
            run.assert_called_once_with(["/bin/npm", "install", "-g", "@openai/codex"],
                                        cwd=workspace.root, check=False)
            self.assertIn("claude CLI: installed", output.getvalue())

    def test_cli_check_reports_both_and_requires_selected_cli(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            with patch("sidequestor.setup.resolve_job_path", return_value=SimpleNamespace(value="/bin")), \
                 patch("sidequestor.setup.find_agent", return_value=None), \
                 redirect_stdout(StringIO()) as output:
                ready = check_worker_clis(workspace, Path("/usr/bin/python3"), agent="claude",
                                          interactive=False)

            self.assertFalse(ready)
            self.assertIn("codex CLI: missing", output.getvalue())
            self.assertIn("claude CLI: missing", output.getvalue())

    def test_successful_npm_install_outside_job_path_explains_location(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            answers = iter(["yes", "no"])

            def run(args, **_kwargs):
                if args[1:] == ["prefix", "-g"]:
                    return SimpleNamespace(returncode=0, stdout="/opt/node\n")
                return SimpleNamespace(returncode=0)

            with patch("sidequestor.setup.resolve_job_path", return_value=SimpleNamespace(value="/bin")), \
                 patch("sidequestor.setup.find_agent", return_value=None), \
                 patch("sidequestor.setup.shutil.which", side_effect=[None, "/opt/node/bin/npm"]), \
                 patch("sidequestor.setup.subprocess.run", side_effect=run), \
                 redirect_stdout(StringIO()) as output:
                ready = check_worker_clis(workspace, Path("/usr/bin/python3"), agent="codex",
                                          input_fn=lambda _prompt: next(answers))

            self.assertFalse(ready)
            self.assertIn("/opt/node/bin is not on", output.getvalue())
            self.assertNotIn("To install codex later", output.getvalue())

    def test_missing_npm_does_not_attempt_install(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            answers = iter(["yes", "no"])
            with patch("sidequestor.setup.resolve_job_path", return_value=SimpleNamespace(value="/bin")), \
                 patch("sidequestor.setup.find_agent", return_value=None), \
                 patch("sidequestor.setup.shutil.which", return_value=None), \
                 patch("sidequestor.setup.subprocess.run") as run, \
                 redirect_stdout(StringIO()) as output:
                ready = check_worker_clis(workspace, Path("/usr/bin/python3"), agent="codex",
                                          input_fn=lambda _prompt: next(answers))
            self.assertFalse(ready)
            run.assert_not_called()
            self.assertIn("npm is unavailable", output.getvalue())

    def test_cursor_check_requires_cursor_agent_without_install_prompts(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            with patch("sidequestor.setup.resolve_job_path", return_value=SimpleNamespace(value="/bin")), \
                 patch("sidequestor.setup.find_agent", return_value=None), \
                 redirect_stdout(StringIO()) as output:
                ready = check_worker_clis(workspace, Path("/usr/bin/python3"), agent="cursor",
                                          input_fn=lambda _prompt: self.fail("unexpected install prompt"))
            self.assertFalse(ready)
            self.assertIn("Cursor CLI: missing", output.getvalue())

    def test_setup_checks_clis_after_slack_prompt_before_starting_jobs(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_CHECKER_CONNECTORS=slack\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=1\n"
            )
            prompts = []

            def decline(prompt):
                prompts.append(prompt)
                return "n"

            with patch("sidequestor.setup.sync_resources"), \
                 patch("sidequestor.setup.provision_env"), \
                 patch("sidequestor.setup.check_worker_clis", return_value=False) as check, \
                 patch("sidequestor.setup.install_production") as install, \
                 redirect_stdout(StringIO()):
                result = run_setup(workspace, Path("/usr/bin/python3"), input_fn=decline)
            self.assertEqual(result, 1)
            self.assertTrue(any("Slack OAuth" in prompt for prompt in prompts))
            check.assert_called_once()
            install.assert_not_called()

    def test_cursor_provisioning_uses_cursor_defaults_without_foreign_knobs(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            workspace.env_file.write_text(
                "SIDEQUESTOR_AGENT=cursor\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
            )

            provision_env(workspace, interactive=False)

            content = workspace.env_file.read_text()
            self.assertNotIn("SIDEQUESTOR_CURSOR_MODEL=", content)
            self.assertNotIn("SIDEQUESTOR_CURSOR_EFFORT", content)
            self.assertNotIn("SIDEQUESTOR_CURSOR_PERMISSION_MODE", content)

    def test_interactive_setup_accepts_cursor(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            workspace.env_file.write_text("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n")
            answers = iter(["cursor", "sonnet-4-thinking"])

            provision_env(workspace, input_fn=lambda _prompt: next(answers), interactive=True)

            content = workspace.env_file.read_text()
            self.assertIn("SIDEQUESTOR_AGENT=cursor", content)
            self.assertIn("SIDEQUESTOR_CURSOR_MODEL=sonnet-4-thinking", content)
            self.assertNotIn("SIDEQUESTOR_CURSOR_EFFORT", content)
            self.assertNotIn("SIDEQUESTOR_CURSOR_PERMISSION_MODE", content)

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

    def test_sync_resources_creates_workspace_agent_and_claude_skill_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)

            destination = sync_resources(workspace)

            managed_skill = workspace.root / "skills" / "yaas-ops"
            self.assertTrue(managed_skill.is_symlink())
            self.assertEqual(os.readlink(managed_skill), "../.yaas/engine/current/skills/yaas-ops")
            agent_skill = workspace.root / ".agents" / "skills" / "yaas-ops"
            self.assertTrue(agent_skill.is_symlink())
            self.assertEqual(os.readlink(agent_skill), "../../.yaas/engine/current/skills/yaas-ops")
            claude_skill = workspace.root / ".claude" / "skills" / "yaas-ops"
            self.assertTrue(claude_skill.is_symlink())
            self.assertEqual(os.readlink(claude_skill), "../../.yaas/engine/current/skills/yaas-ops")
            prompt = workspace.root / ".codex" / "prompts" / "new-quest.md"
            self.assertTrue(prompt.is_file())
            self.assertIn("sq new-quest '<spec_json>'", prompt.read_text())
            self.assertEqual(current_engine_version(workspace), ENGINE_VERSION)
            self.assertTrue((destination / "skills" / "yaas-ops" / "SKILL.md").is_file())
            for parent in ("skills", ".agents/skills", ".claude/skills"):
                link = workspace.root / parent / "yaas-slack-file"
                self.assertTrue(link.is_symlink())
                self.assertTrue((link / "SKILL.md").is_file())

    def test_sync_resources_is_idempotent_and_prunes_stale_skill_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            sync_resources(workspace)
            stale = workspace.root / "skills" / "yaas-stale"
            stale.symlink_to("../.yaas/engine/current/skills/yaas-stale", target_is_directory=True)
            wrong = workspace.root / ".claude" / "skills" / "yaas-ops"
            wrong.unlink()
            wrong.symlink_to("../../broken-target", target_is_directory=True)
            wrong_agent = workspace.root / ".agents" / "skills" / "yaas-ops"
            wrong_agent.unlink()
            wrong_agent.symlink_to("../../broken-target", target_is_directory=True)

            first = sync_resources(workspace)
            second = sync_resources(workspace)

            self.assertEqual(first, second)
            self.assertFalse(stale.exists())
            self.assertEqual(os.readlink(workspace.root / ".agents" / "skills" / "yaas-ops"),
                             "../../.yaas/engine/current/skills/yaas-ops")
            self.assertEqual(os.readlink(workspace.root / ".claude" / "skills" / "yaas-ops"),
                             "../../.yaas/engine/current/skills/yaas-ops")

    def test_sync_resources_backfills_agent_skills_in_existing_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            agent_skills = workspace.root / ".agents" / "skills"
            user_skill = agent_skills / "user-owned-skill"
            user_skill.mkdir(parents=True)
            (user_skill / "SKILL.md").write_text("user owned\n")

            sync_resources(workspace)

            managed_skill = agent_skills / "yaas-ops"
            self.assertTrue(managed_skill.is_symlink())
            self.assertEqual(os.readlink(managed_skill),
                             "../../.yaas/engine/current/skills/yaas-ops")
            self.assertEqual((user_skill / "SKILL.md").read_text(), "user owned\n")

    def test_sync_resources_refreshes_examples_without_touching_live_configuration(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            workspace.env_file.write_text("CUSTOM_SECRET=keep-me\n")
            settings = workspace.root / "settings.json"
            settings.write_text('{"custom": "keep-me"}\n')
            (workspace.root / ".env.example").write_text("stale env example\n")
            (workspace.root / "settings.json.example").write_text("stale settings example\n")

            sync_resources(workspace)

            packaged = files("sidequestor").joinpath("package_data")
            self.assertEqual((workspace.root / ".env.example").read_text(),
                             packaged.joinpath("env.example").read_text())
            self.assertEqual((workspace.root / "settings.json.example").read_text(),
                             packaged.joinpath("settings.json.example").read_text())
            self.assertEqual(workspace.env_file.read_text(), "CUSTOM_SECRET=keep-me\n")
            self.assertEqual(settings.read_text(), '{"custom": "keep-me"}\n')

    def test_sync_resources_leaves_real_files_in_place(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            blocked = workspace.root / "skills" / "yaas-ops"
            blocked.write_text("user-owned override\n")
            agents_root = workspace.root / ".agents"
            agents_root.mkdir()
            (agents_root / "skills").mkdir()
            blocked_agent = agents_root / "skills" / "yaas-gmail-reply"
            blocked_agent.write_text("custom entry\n")
            claude_root = workspace.root / ".claude"
            claude_root.mkdir()
            (claude_root / "skills").mkdir()
            blocked_claude = claude_root / "skills" / "yaas-gmail-reply"
            blocked_claude.write_text("custom entry\n")

            sync_resources(workspace)

            self.assertFalse(blocked.is_symlink())
            self.assertEqual(blocked.read_text(), "user-owned override\n")
            self.assertFalse(blocked_agent.is_symlink())
            self.assertEqual(blocked_agent.read_text(), "custom entry\n")
            self.assertFalse(blocked_claude.is_symlink())
            self.assertEqual(blocked_claude.read_text(), "custom entry\n")

    def test_sync_resources_skips_agent_codex_or_claude_paths_blocked_by_files(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = init_workspace(raw)
            (workspace.root / ".agents").write_text("blocked\n")
            (workspace.root / ".claude").write_text("blocked\n")
            (workspace.root / ".codex").write_text("blocked\n")

            sync_resources(workspace)

            self.assertFalse((workspace.root / ".agents" / "skills").exists())
            self.assertFalse((workspace.root / ".claude" / "skills").exists())
            self.assertFalse((workspace.root / ".codex" / "prompts").exists())
            self.assertTrue((workspace.root / "skills" / "yaas-ops").is_symlink())


if __name__ == "__main__":
    unittest.main()
