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

    def test_claude_needs_auth_opens_exact_worker_session_then_rechecks(self):
        needs_auth = json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "slack", "status": "needs-auth"}]})
        connected = needs_auth.replace("needs-auth", "connected")
        results = [subprocess.CompletedProcess([], 0, needs_auth, ""),
                   subprocess.CompletedProcess([], 0, "", ""),
                   subprocess.CompletedProcess([], 0, connected, ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch("sidequestor.setup_worker.slack_identity", return_value="U123"), \
                patch("sidequestor.setup_worker.verify_slack_helper", return_value=True), \
                patch("sidequestor.setup_worker.invoke", side_effect=results) as invoke:
            self.assertTrue(verify_claude_slack(self.ws, "/claude", dict(self.env, YAAS_SLACK_CHECKERS_ENABLED="1"), interactive=True))
        login = invoke.call_args_list[1]
        self.assertEqual(login.args[0][-2:], ["--", "/mcp"])
        self.assertIn("--strict-mcp-config", login.args[0])
        self.assertTrue(login.kwargs["interactive"])
        self.assertNotIn("slack", readiness.read_state(self.ws))

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

    def test_claude_requires_actual_read_not_model_claim(self):
        env = dict(self.env, YAAS_SLACK_CHECKERS_ENABLED="1")
        responses = [subprocess.CompletedProcess([], 0, '{"type":"system","subtype":"init","mcp_servers":[{"name":"slack","status":"connected"}]}\n{"result":"read succeeded"}', ""),
                     subprocess.CompletedProcess([], 1, "", "")]
        with patch("sidequestor.setup_worker.slack_identity", return_value="U123"), \
                patch("sidequestor.setup_worker.invoke", side_effect=responses) as invoke:
            self.assertFalse(verify_claude_slack(self.ws, "/claude", env, interactive=False))
        self.assertIn("--strict-mcp-config", invoke.call_args_list[0].args[0])
        self.assertIn("worker.mcp.json", " ".join(invoke.call_args_list[0].args[0]))
        self.assertNotIn("slack", readiness.read_state(self.ws))
