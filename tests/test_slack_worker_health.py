"""Real worker stream evidence must take precedence over native startup metadata."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sidequestor import worker_readiness as readiness
from sidequestor.workspace import init_workspace

RUNTIME = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(RUNTIME))
import tick
spec = importlib.util.spec_from_file_location('worker_slack_health', RUNTIME / 'dispatch/slack-read-health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def stream(command='bash "/runtime/mcp-call.sh" slack_search_public_and_private \'{"query":"test"}\'', *, error=False, background=False, output='{"messages":[]}', native='needs-auth', tool='Bash'):
    return [
        {'type': 'system', 'subtype': 'init', 'mcp_servers': [{'name': 'slack', 'status': native}]},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'read', 'name': tool, 'input': {'command': command, 'run_in_background': background}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'read', 'is_error': error, 'content': output}]}, 'tool_use_result': {'stdout': output}},
    ]


class SlackWorkerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'HOME': self.temp.name, 'PATH': '/usr/bin:/bin'}
        self.patch = patch.dict(os.environ, {'YAAS_CONFIG_HOME': str(self.root / 'config')})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.ws = init_workspace(self.root / 'workspace')
        self.path = self.root / 'worker.ndjson'

    def write(self, events):
        self.path.write_text('\n'.join(json.dumps(event) for event in events))

    def test_claude_foreground_wrapper_read_is_recognized(self):
        self.write(stream())
        self.assertTrue(health.has_successful_slack_read(self.path))

    def test_background_error_empty_and_error_envelope_reads_do_not_count(self):
        for kw in ({'background': True}, {'error': True}, {'output': ''}, {'output': '{"ok":false,"error":"invalid_auth"}'}, {'command': 'echo "mcp-call.sh slack_read_user_profile"'}):
            with self.subTest(kw=kw):
                self.write(stream(**kw))
                self.assertFalse(health.has_successful_slack_read(self.path))

    def test_foreground_redirects_are_not_mistaken_for_background_execution(self):
        self.write(stream(command="bash '/runtime/mcp-call.sh' slack_read_user_profile '{}' 2>&1"))
        self.assertTrue(health.has_successful_slack_read(self.path))

    def test_shell_background_execution_does_not_count(self):
        self.write(stream(command="bash '/runtime/mcp-call.sh' slack_read_user_profile '{}' &"))
        self.assertFalse(health.has_successful_slack_read(self.path))

    def test_codex_and_cursor_wrapper_reads_still_work(self):
        command = "bash /runtime/mcp-call.sh slack_read_user_profile '{}'"
        events = [
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 0, 'command': command, 'aggregated_output': '{"ok":true}'}},
            {'type': 'tool_call', 'subtype': 'completed', 'tool_call': {'shellToolCall': {'args': {'command': command}, 'result': {'success': {'exitCode': 0, 'stdout': '{"ok":true}'}}}}},
        ]
        for event in events:
            with self.subTest(event=event['type']):
                self.write([event])
                self.assertTrue(health.has_successful_slack_read(self.path))

    def test_shell_compositions_cannot_hide_a_failed_wrapper(self):
        for suffix in (" || echo '{}'", "; echo done", " | head -c 100", "\necho done", " && echo done"):
            with self.subTest(suffix=suffix):
                self.write(stream(command="bash /runtime/mcp-call.sh slack_read_user_profile '{}'" + suffix, output='{}'))
                self.assertFalse(health.has_successful_slack_read(self.path))

    def test_grouped_shell_operators_cannot_mask_failed_reads(self):
        wrapper = "bash /runtime/mcp-call.sh slack_read_thread '{}'"
        for command in (wrapper + " |& tail -5", "(" + wrapper + ");true", "(" + wrapper + ")||true", "(" + wrapper + ")&"):
            with self.subTest(command=command):
                self.write(stream(command=command, output='temporary server error'))
                self.assertFalse(health.has_successful_slack_read(self.path))

    def test_simple_redirects_do_not_change_wrapper_exit_status(self):
        self.assertTrue(health.is_shell_slack_read("bash /runtime/mcp-call.sh slack_read_user_profile '{}' 2>&1 >/dev/null"))

    def test_bash_expansion_and_comment_desyncs_are_not_read_evidence(self):
        commands = [
            "bash /runtime/mcp-call.sh slack_read_thread x#;true",
            "bash /runtime/mcp-call.sh slack_read_thread $'\\'' ;true; : $'\\'''",
            "bash /runtime/mcp-call.sh slack_read_thread $(echo test)",
            "bash /runtime/mcp-call.sh slack_read_thread `echo test`",
            'bash "$RUNTIME/mcp-call.sh" slack_read_thread test',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.write(stream(command=command, output='temporary server error'))
                self.assertFalse(health.has_successful_slack_read(self.path))

    def test_native_tool_reads_still_work(self):
        self.write(stream(tool='mcp__slack__slack_read_user_profile'))
        self.assertTrue(health.has_successful_slack_read(self.path))

    def test_interrupted_or_background_results_do_not_count(self):
        for result in ({'interrupted': True}, {'backgroundTaskId': 'task'}, {'exitCode': 1}):
            events = stream(); events[-1]['tool_use_result'].update(result)
            self.write(events)
            self.assertFalse(health.has_successful_slack_read(self.path))

    def dispatch(self, events, helper_ready, worker_exit=0):
        self.write(events)
        def run(args):
            if args[1].endswith('run-agent.py'):
                return subprocess.CompletedProcess(args, 0, json.dumps({'exit': worker_exit, 'ndjson': str(self.path)}), '')
            if args[1].endswith('slack-read-health.py'):
                return subprocess.CompletedProcess(args, 0 if health.has_successful_slack_read(self.path) else 1, '', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        t = SimpleNamespace(repo_root=self.ws.root, env=self.env, agent='claude', dispatched=0,
            cfg=SimpleNamespace(checker_connectors={'slack'}, checker_enabled=lambda _: True),
            quests_dir=self.ws.root / 'state/quests/active', run_log=self.root / 'runs.jsonl',
            log_file=self.root / 'triage.log', log=lambda _: None, event=lambda _: None,
            helper=lambda *parts: str(RUNTIME.joinpath(*parts)), py=lambda *args: [sys.executable, *map(str, args)], run=run)
        with patch.object(readiness, 'slack_helper_ready', return_value=helper_ready) as probe, patch('sidequestor.job_path.find_agent', return_value='/missing'):
            tick.dispatch_one(t, 'quest-test', 30, [{'quest_id': 'quest-test', 'watch_id': 'watch', 'type': 'slack_thread'}])
        return t, probe

    def test_successful_helper_read_preserves_worker_success_despite_native_needs_auth(self):
        t, probe = self.dispatch(stream(), helper_ready=False)
        self.assertEqual(t.dispatch_exit, 0)
        self.assertEqual(t.dispatch_slack_read_ok, 1)
        self.assertFalse(t.dispatch_dependency_blocked)
        self.assertNotIn('slack', readiness.read_state(self.ws))
        probe.assert_not_called()

    def test_failed_worker_read_and_failed_probe_remain_blocking(self):
        t, probe = self.dispatch(stream(error=True), helper_ready=False)
        self.assertEqual(t.dispatch_exit, 9)
        self.assertTrue(t.dispatch_dependency_blocked)
        self.assertIn('slack', readiness.read_state(self.ws))
        probe.assert_called_once()

    def test_missing_read_is_verified_through_helper_before_blocking(self):
        t, probe = self.dispatch(stream(error=True), helper_ready=True)
        self.assertEqual(t.dispatch_exit, 0)
        probe.assert_called_once()

    def test_helper_success_does_not_turn_worker_failure_into_success(self):
        t, _ = self.dispatch(stream(), helper_ready=True, worker_exit=2)
        self.assertEqual(t.dispatch_exit, 2)

    def test_existing_tick_probe_clears_stale_block_without_another_slack_probe(self):
        readiness.save_state(self.ws, 'claude', '/missing', self.env, 'Old native auth error', scope='slack')
        calls = []
        t = SimpleNamespace(repo_root=self.ws.root, env=self.env, agent='claude', mcp_call='/runtime/mcp-call.sh',
                            run=lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, '{}', ''))
        with patch('sidequestor.job_path.find_agent', return_value='/missing'):
            self.assertTrue(tick.slack_health_ok(t))
        with patch.object(readiness, 'auth_status', return_value=('ok', None)), patch.object(readiness, 'slack_helper_ready') as extra:
            self.assertIsNone(readiness.preflight(self.ws, 'claude', '/missing', self.env, needs_slack=True))
            extra.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_ambiguous_shell_read_uses_live_probe_instead_of_unproven_success(self):
        events = stream(command="bash /runtime/mcp-call.sh slack_read_thread '{}' || echo '{}'", output='{}')
        t, probe = self.dispatch(events, helper_ready=False)
        self.assertEqual(t.dispatch_exit, 9)
        self.assertEqual(t.dispatch_slack_read_ok, 0)
        probe.assert_called_once()
