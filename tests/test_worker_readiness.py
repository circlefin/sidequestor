import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor import worker_readiness as readiness
from sidequestor.setup_worker import verify_claude_slack, worker_environment
from sidequestor.workspace import init_workspace


class WorkerReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {"YAAS_CONFIG_HOME": self.temp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.ws = init_workspace(Path(self.temp.name) / "workspace")
        self.env = {"HOME": self.temp.name, "PATH": "/usr/bin:/bin"}

    def test_unavailable_or_old_auth_command_does_not_block_existing_users(self):
        for response in (subprocess.CompletedProcess([], 1, "", "unknown command auth"),
                         subprocess.TimeoutExpired([], 1)):
            with self.subTest(response=response):
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(readiness, "invoke", **kwargs):
                    self.assertIsNone(readiness.auth_check(self.ws, "claude", "/missing", self.env))

    def test_missing_login_blocks_without_touching_quest_ledger(self):
        ledger = self.ws.state / "unacked.json"
        ledger.write_text('{"keep": 1}')
        with patch.object(readiness, "invoke", return_value=subprocess.CompletedProcess([], 1, '{"loggedIn":false}', "")):
            reason = readiness.preflight(self.ws, "claude", "/missing", self.env)
        self.assertIn("sq setup", reason)
        self.assertEqual(ledger.read_text(), '{"keep": 1}')
        self.assertEqual(readiness.blocked_reason(self.ws, "claude", "/missing", self.env), reason)

    def test_recovery_clears_only_verified_scope(self):
        readiness.save_state(self.ws, "claude", "/missing", self.env, "AI login", scope="blocked")
        readiness.save_state(self.ws, "claude", "/missing", self.env, "Slack login", scope="slack")
        readiness.save_state(self.ws, "claude", "/missing", self.env)
        self.assertIsNone(readiness.blocked_reason(self.ws, "claude", "/missing", self.env))
        self.assertEqual(readiness.blocked_reason(self.ws, "claude", "/missing", self.env, needs_slack=True), "Slack login")
        readiness.save_state(self.ws, "claude", "/missing", self.env, scope="slack")
        self.assertIsNone(readiness.blocked_reason(self.ws, "claude", "/missing", self.env, needs_slack=True))

    def test_changed_model_invalidates_old_failure(self):
        readiness.save_state(self.ws, "codex", "/missing", self.env, "old model")
        changed = dict(self.env, YAAS_CODEX_MODEL="new-model")
        self.assertIsNone(readiness.blocked_reason(self.ws, "codex", "/missing", changed))

    def test_network_and_rate_limit_errors_are_not_auth_failures(self):
        for text in ("HTTP 429 rate limit", "Connection timed out", "HTTP 503"):
            self.assertIsNone(readiness.invocation_failure(self.ws, "codex", "/missing", self.env, text))
        self.assertFalse(readiness.state_path(self.ws).exists())

    def test_external_credentials_do_not_trigger_false_logged_out(self):
        for agent, key in (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY"),
                           ("claude", "CLAUDE_CODE_USE_BEDROCK")):
            with patch.object(readiness, "invoke") as invoke:
                self.assertIsNone(readiness.auth_check(self.ws, agent, "/binary", dict(self.env, **{key: "1"})))
                invoke.assert_not_called()

    def test_worker_tool_error_cannot_persist_ai_block_without_confirmation(self):
        with patch.object(readiness, "auth_check", return_value=None):
            self.assertIsNone(readiness.invocation_failure(self.ws, "claude", "/binary", self.env,
                                                           "gh is not logged in; unknown option"))
        self.assertFalse(readiness.state_path(self.ws).exists())

    def test_claude_setup_accepts_verified_helper_without_native_oauth(self):
        with patch("sidequestor.setup_worker.verify_slack_helper", return_value=True), \
                patch("sidequestor.setup_worker.invoke") as invoke:
            self.assertTrue(verify_claude_slack(self.ws, "/claude", dict(self.env, YAAS_SLACK_CHECKERS_ENABLED="1"), interactive=True))
        invoke.assert_not_called()
        self.assertNotIn("slack", readiness.read_state(self.ws))

    def test_setup_printed_auth_and_model_hints_target_the_selected_workspace(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from sidequestor.setup_worker import setup_worker
        from sidequestor.workspace import workspace_command
        path = SimpleNamespace(agent="claude", agent_binary="/claude")
        reason = "AI account needs attention. Run sq setup."
        for auth_reason in (reason, None):
            with self.subTest(auth_reason=auth_reason), \
                    patch("sidequestor.setup_worker.worker_environment", return_value=(path, self.env)), \
                    patch("sidequestor.setup_worker.invoke", return_value=subprocess.CompletedProcess([], 1, "", "failure")), \
                    patch("sidequestor.setup_worker.auth_check", return_value=auth_reason), \
                    patch("sidequestor.setup_worker.invocation_failure", return_value=reason), \
                    redirect_stdout(StringIO()) as output:
                self.assertFalse(setup_worker(self.ws, Path("/python"), interactive=False))
            self.assertIn(workspace_command(self.ws, "setup"), output.getvalue())
            self.assertNotIn("Run sq setup.", output.getvalue())

    def test_installed_claude_parses_mcp_menu_as_prompt_not_config(self):
        import shutil
        binary = shutil.which("claude")
        if not binary:
            self.skipTest("Claude CLI is not installed")
        config = self.ws.root / "worker.mcp.json"
        config.write_text('{"mcpServers":{}}')
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": self.temp.name,
                       "CLAUDE_CONFIG_DIR": self.temp.name,
                       "ANTHROPIC_API_KEY": "parser-test-not-a-real-key",
                       "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
                       "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        command = [binary, "-p", "--strict-mcp-config", "--mcp-config", str(config)]
        broken = readiness.invoke(command + ["/mcp"], self.ws, environment, timeout=10)
        self.assertIn("MCP config file not found: /mcp", broken.stderr)
        fixed = readiness.invoke(command + ["--", "/mcp"], self.ws, environment, timeout=10)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertIn("No MCP servers", fixed.stdout)

    def test_interrupting_headless_probe_cleans_up_its_process_group(self):
        from unittest.mock import MagicMock
        process = MagicMock(pid=12345)
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch.object(readiness.subprocess, "Popen", return_value=process), \
                patch.object(readiness.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                readiness.invoke(["/cli"], self.ws, self.env)
        kill.assert_called_once_with(12345, readiness.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_worker_environment_does_not_accept_shell_only_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shell-only"}), \
                patch("sidequestor.job_path.login_shell_path", return_value=[]):
            _, environment = worker_environment(self.ws, Path("/usr/bin/python3"))
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_claude_setup_failed_helper_does_not_claim_recovery(self):
        with patch("sidequestor.setup_worker.verify_slack_helper", return_value=False), \
                patch("sidequestor.setup_worker.invoke") as invoke:
            self.assertFalse(verify_claude_slack(self.ws, "/claude", dict(self.env, YAAS_SLACK_CHECKERS_ENABLED="1"), interactive=False))
        invoke.assert_not_called()
        self.assertIn("slack", readiness.read_state(self.ws))

    def test_cached_auth_recovery_is_throttled_and_requires_positive_status(self):
        with patch.object(readiness.time, "time", return_value=100):
            readiness.save_state(self.ws, "claude", "/missing", self.env, "AI login", kind="auth")
        with patch.object(readiness, "auth_status", return_value=("ok", None)) as probe:
            with patch.object(readiness.time, "time", return_value=129):
                self.assertEqual(readiness.preflight(self.ws, "claude", "/missing", self.env), "AI login")
                probe.assert_not_called()
            with patch.object(readiness.time, "time", return_value=130):
                self.assertIsNone(readiness.preflight(self.ws, "claude", "/missing", self.env))
                self.assertIsNone(readiness.preflight(self.ws, "claude", "/missing", self.env))
                probe.assert_called_once()
        self.assertNotIn("blocked", readiness.read_state(self.ws))

    def test_unknown_status_keeps_known_block_and_does_not_retry_each_target(self):
        with patch.object(readiness.time, "time", return_value=100):
            readiness.save_state(self.ws, "claude", "/missing", self.env, "AI login")
        with patch.object(readiness.time, "time", return_value=130), \
                patch.object(readiness, "auth_status", return_value=("unknown", None)) as probe:
            for _ in range(10):
                self.assertEqual(readiness.preflight(self.ws, "claude", "/missing", self.env), "AI login")
            probe.assert_called_once()

    def test_compat_block_is_not_cleared_by_successful_login(self):
        readiness.save_state(self.ws, "claude", "/missing", self.env, "Bad model", kind="compat")
        with patch.object(readiness, "auth_status") as probe:
            self.assertEqual(readiness.preflight(self.ws, "claude", "/missing", self.env), "Bad model")
            probe.assert_not_called()

    def test_relevant_provider_and_permission_changes_invalidate_block(self):
        readiness.save_state(self.ws, "claude", "/missing", self.env, "Old config", kind="compat")
        for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_MODEL",
                    "YAAS_CLAUDE_PERMISSION_MODE", "YAAS_CODEX_PERMISSION_MODE",
                    "YAAS_CURSOR_PERMISSION_MODE", "SIDEQUESTOR_CURSOR_PERMISSION_MODE"):
            with self.subTest(key=key):
                changed = dict(self.env, **{key: "changed"})
                self.assertIsNone(readiness.blocked_reason(self.ws, "claude", "/missing", changed))

    def test_model_entitlement_is_compat_not_login_failure(self):
        with patch.object(readiness, "auth_status", return_value=("ok", None)):
            reason = readiness.invocation_failure(self.ws, "claude", "/missing", self.env,
                "Unauthorized to use this model", trusted_cli=True, direct_auth=True)
        self.assertIn("model", reason)
        self.assertEqual(readiness.read_state(self.ws)["blocked_kind"], "compat")

    def test_external_key_rejection_still_blocks(self):
        with patch.object(readiness, "auth_status", return_value=("unknown", None)):
            reason = readiness.invocation_failure(self.ws, "claude", "/missing", self.env,
                "401 invalid api key", trusted_cli=True, direct_auth=True)
        self.assertIn("authenticate", reason)
        self.assertEqual(readiness.read_state(self.ws)["blocked_kind"], "auth")

    def test_slack_block_recovery_and_auth_timestamps_are_independent(self):
        with patch.object(readiness.time, "time", return_value=100):
            readiness.save_state(self.ws, "claude", "/missing", self.env, "Slack down", scope="slack")
        with patch.object(readiness.time, "time", return_value=129):
            readiness.save_state(self.ws, "claude", "/missing", self.env)
        with patch.object(readiness.time, "time", return_value=130), \
                patch.object(readiness, "slack_helper_ready", return_value=True) as probe:
            self.assertIsNone(readiness.preflight(self.ws, "claude", "/missing", self.env, needs_slack=True))
            probe.assert_called_once()
        self.assertNotIn("slack", readiness.read_state(self.ws))

    def test_slack_probe_failure_keeps_block_and_throttles(self):
        with patch.object(readiness, "slack_helper_ready", return_value=False) as probe:
            for _ in range(10):
                self.assertIsNotNone(readiness.verify_slack_access(self.ws, "claude", "/missing", self.env))
            probe.assert_called_once()
        with patch.object(readiness, "slack_helper_ready") as probe:
            self.assertIsNone(readiness.verify_slack_access(self.ws, "claude", "/missing", self.env, read_succeeded=True))
            probe.assert_not_called()

    def test_unknown_auth_command_does_not_clear_failure(self):
        with patch.object(readiness, "invoke", side_effect=subprocess.TimeoutExpired([], 5)):
            self.assertEqual(readiness.auth_status(self.ws, "claude", "/missing", self.env), ("unknown", None))

    def test_auth_status_distinguishes_positive_claude_and_codex_login(self):
        for agent, output in (("claude", '{"loggedIn":true}'), ("codex", "Logged in using ChatGPT")):
            with patch.object(readiness, "invoke", return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assertEqual(readiness.auth_status(self.ws, agent, "/missing", self.env), ("ok", None))

    def test_disabled_slack_does_not_require_reconnection_during_setup(self):
        readiness.save_state(self.ws, "claude", "/missing", self.env, "Old Slack failure", scope="slack")
        with patch("sidequestor.setup_worker.verify_slack_helper") as helper:
            self.assertTrue(verify_claude_slack(self.ws, "/missing", self.env, interactive=False))
            helper.assert_not_called()
        self.assertNotIn("slack", readiness.read_state(self.ws))

    def test_setup_and_runtime_use_the_same_probe(self):
        from sidequestor.setup_worker import verify_slack_helper
        with patch("sidequestor.setup_worker.slack_helper_ready", return_value=True) as helper:
            self.assertTrue(verify_slack_helper(self.ws, dict(self.env, YAAS_SLACK_CHECKERS_ENABLED="1")))
            helper.assert_called_once()

    def test_temporary_model_unavailability_is_not_persistent_compat(self):
        self.assertIsNone(readiness.invocation_failure(self.ws, "claude", "/missing", self.env,
            "Model is currently not available, please retry later", trusted_cli=True))
        self.assertFalse(readiness.read_state(self.ws))

    def test_config_file_repair_invalidates_block(self):
        for agent, dirname, filename in (("codex", ".codex", "config.toml"), ("claude", ".claude", "settings.json")):
            with self.subTest(agent=agent):
                path = Path(self.env["HOME"]) / dirname / filename
                path.parent.mkdir(exist_ok=True)
                path.write_text("old config")
                readiness.save_state(self.ws, agent, "/missing", self.env, "Old model", kind="compat")
                path.write_text("repaired config")
                self.assertIsNone(readiness.blocked_reason(self.ws, agent, "/missing", self.env))

    def test_auth_recheck_cannot_clear_concurrent_stronger_block(self):
        for kind in ("compat", "auth_direct"):
            with self.subTest(kind=kind):
                readiness.state_path(self.ws).unlink(missing_ok=True)
                def status(*args):
                    readiness.save_state(self.ws, "claude", "/missing", self.env, "Concurrent failure", kind=kind)
                    return "ok", None
                with patch.object(readiness, "auth_status", side_effect=status):
                    self.assertEqual(readiness.preflight(self.ws, "claude", "/missing", self.env), "Concurrent failure")
                self.assertEqual(readiness.read_state(self.ws)["blocked_kind"], kind)

    def test_direct_provider_rejection_is_not_overruled_by_local_logged_in_flag(self):
        with patch.object(readiness, "auth_status", return_value=("ok", None)):
            reason = readiness.invocation_failure(self.ws, "claude", "/missing", self.env,
                "OAuth token has expired", trusted_cli=True, direct_auth=True)
            self.assertIn("authenticate", reason)
            self.assertEqual(readiness.read_state(self.ws)["blocked_kind"], "auth_direct")
            self.assertEqual(readiness.preflight(self.ws, "claude", "/missing", self.env), reason)

    def test_dashboard_reads_the_same_plist_environment_as_preflight(self):
        import plistlib
        env = dict(self.env, SIDEQUESTOR_WORKSPACE=str(self.ws.root), YAAS_AGENT="claude")
        readiness.save_state(self.ws, "claude", "/missing", env, "Slack unavailable", scope="slack")
        path = self.ws.root / "job.plist"
        path.write_bytes(plistlib.dumps({"EnvironmentVariables": env}))
        with patch("sidequestor.launchd.production_status", return_value={"jobs": {"triage": {"plist": str(path)}}}), \
                patch("sidequestor.job_path.find_agent", return_value="/missing"):
            self.assertEqual(readiness.current_state(self.ws)["slack"], "Slack unavailable")
