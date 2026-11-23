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

    def test_successful_read_never_clears_a_delivery_uncertainty_blocker(self):
        quest = self.ws.root / 'state/quests/active/quest-delivery-recovery'
        quest.mkdir()
        timeline = quest / 'timeline.ndjson'
        t = SimpleNamespace(quests_dir=quest.parent, now_utc='2026-11-07T12:00:00Z',
                            log=lambda _: None)
        for markers in ({'blocker_kind': 'delivery_uncertain'},
                        {'delivery_status': 'uncertain'},
                        {'blocker_kind': 'slack_tooling_outage', 'delivery_status': 'uncertain'}):
            with self.subTest(markers=markers):
                record = {'ts': '2026-11-07T10:00:00Z', 'event': 'blocked',
                          'note': 'slack mcp send (HTTP 503): service_unavailable', **markers}
                original = json.dumps(record) + '\n'
                timeline.write_text(original)
                tick.mark_recovered_if_blocked(t, quest.name, 'slack', 'Slack read succeeded',
                                               '2026-11-07T11:00:00Z')
                self.assertEqual(timeline.read_text(), original)

    def test_successful_read_still_clears_existing_slack_outage_blockers(self):
        quest = self.ws.root / 'state/quests/active/quest-outage-recovery'
        quest.mkdir()
        timeline = quest / 'timeline.ndjson'
        t = SimpleNamespace(quests_dir=quest.parent, now_utc='2026-11-07T12:00:00Z',
                            log=lambda _: None)
        for markers in ({}, {'blocker_kind': 'slack_tooling_outage'}):
            with self.subTest(markers=markers):
                timeline.write_text(json.dumps({
                    'ts': '2026-11-07T10:00:00Z', 'event': 'blocked',
                    'note': 'slack mcp failed: service_unavailable', **markers}) + '\n')
                tick.mark_recovered_if_blocked(t, quest.name, 'slack', 'Slack read succeeded',
                                               '2026-11-07T11:00:00Z')
                events = [json.loads(line) for line in timeline.read_text().splitlines()]
                self.assertEqual(len(events), 2)
                self.assertEqual(events[-1]['event'], 'note')
                self.assertEqual(events[-1]['recovered_from'], 'blocked')

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

    def dispatch(self, events, helper_ready, worker_exit=0, watch_type='slack_thread'):
        self.write(events)
        def run(args):
            if args[1].endswith('run-agent.py'):
                return subprocess.CompletedProcess(args, 0, json.dumps({'exit': worker_exit, 'ndjson': str(self.path)}), '')
            if args[1].endswith('slack-read-health.py'):
                if '--failed-access' in args:
                    return subprocess.CompletedProcess(args, 0 if health.has_failed_slack_access(self.path) else 1, '', '')
                return subprocess.CompletedProcess(args, 0 if health.has_successful_slack_read(self.path) else 1, '', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        t = SimpleNamespace(repo_root=self.ws.root, env=self.env, agent='claude', dispatched=0,
            cfg=SimpleNamespace(checker_connectors={'slack'}, checker_enabled=lambda _: True),
            quests_dir=self.ws.root / 'state/quests/active', run_log=self.root / 'runs.jsonl',
            log_file=self.root / 'triage.log', log=lambda _: None, event=lambda _: None,
            helper=lambda *parts: str(RUNTIME.joinpath(*parts)), py=lambda *args: [sys.executable, *map(str, args)], run=run)
        with patch.object(readiness, 'slack_helper_ready', return_value=helper_ready) as probe, patch('sidequestor.job_path.find_agent', return_value='/missing'):
            tick.dispatch_one(t, 'quest-test', 30, [{'quest_id': 'quest-test', 'watch_id': 'watch', 'type': watch_type}])
        return t, probe

    def test_schedule_with_failed_slack_read_and_failed_probe_cannot_commit(self):
        t, probe = self.dispatch(stream(error=True, output='invalid_auth'), helper_ready=False, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 9)
        self.assertFalse(t.dispatch_dependency_blocked)
        probe.assert_called_once()

    def test_schedule_with_recovered_helper_preserves_existing_fallback(self):
        t, probe = self.dispatch(stream(error=True, output='invalid_auth'), helper_ready=True, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 0)
        probe.assert_called_once()

    def test_schedule_read_quoting_error_text_does_not_gate_or_probe(self):
        t, probe = self.dispatch(stream(output='Messages about failed to connect'), helper_ready=False, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 0)
        probe.assert_not_called()

    def test_schedule_can_handle_a_missing_thread_without_an_access_gate(self):
        t, probe = self.dispatch(stream(error=True, output='thread_not_found'), helper_ready=False, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 0)
        probe.assert_not_called()

    def test_schedule_access_failure_holds_watermark_and_retains_bounded_backoff(self):
        t, _ = self.dispatch(stream(error=True, output='invalid_auth'), helper_ready=False, watch_type='schedule')
        quest = t.quests_dir / 'quest-test'
        quest.mkdir()
        watch = {'watches': [{'type': 'schedule', 'watch_id': 'watch', 'last_checked_ts': '1'}]}
        (quest / 'watch.json').write_text(json.dumps(watch))
        t.manifest_dir = self.root / 'manifests'; t.manifest_dir.mkdir()
        (t.manifest_dir / f'dispatch-{t.dispatch_run_id}.json').write_text(json.dumps({
            'items': [{'item_id': 'watch', 'type': 'schedule', 'status': 'handled'}]}))
        t.unacked_file = self.root / 'unacked.json'
        t.now_utc, t.now_ts, t.unacked_promote = '2026-11-07T12:00:00Z', 1794052800, 3
        t._read_json = lambda p, default: json.loads(p.read_text()) if p.exists() else default
        dirty = [{'quest_id': 'quest-test', 'watch_id': 'watch', 'type': 'schedule'}]
        for _ in range(3):
            tick.commit_quest(t, 'quest-test', dirty)
        self.assertEqual(json.loads((quest / 'watch.json').read_text()), watch)
        record = json.loads(t.unacked_file.read_text())['quest-test|watch']
        self.assertEqual(record['count'], 3)
        self.assertGreater(float(record['next_retry_ts']), t.now_ts)
        self.assertIn('Slack', record['last_error'])

    def test_schedule_with_successful_read_keeps_success(self):
        t, probe = self.dispatch(stream(), helper_ready=False, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 0)
        self.assertFalse(t.dispatch_dependency_blocked)
        probe.assert_not_called()

    def test_unused_failed_native_connection_does_not_gate_non_slack_schedule(self):
        t, probe = self.dispatch([stream()[0]], helper_ready=False, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 0)
        self.assertFalse(t.dispatch_dependency_blocked)
        probe.assert_not_called()

    def test_scheduled_worker_failure_is_not_replaced_by_readiness_result(self):
        t, _ = self.dispatch(stream(error=True), helper_ready=False, worker_exit=2, watch_type='schedule')
        self.assertEqual(t.dispatch_exit, 2)

    def test_access_failures_require_tool_evidence_not_mentions_or_startup_metadata(self):
        for events, expected in [(stream(error=True, output='invalid_auth'), True), ([stream()[0]], False),
                                 (stream(command='echo "mcp-call.sh slack_read_thread"'), False),
                                 (stream(tool='mcp__slack__slack_read_thread', error=True, output='invalid_auth'), True),
                                 ([{'type': 'item.completed', 'item': {'type': 'command_execution',
                                    'command': "bash /runtime/mcp-call.sh slack_read_thread '{}'", 'exit_code': 1}}], True)]:
            with self.subTest(events=events):
                self.write(events)
                self.assertEqual(health.has_failed_slack_access(self.path), expected)

    def test_observed_claude_bash_failure_record_shape_is_recognized(self):
        # Shape verified with the installed Claude CLI running `false`: the
        # result is a string, while the tool_result carries is_error + Exit code.
        for code, expected in ((1, True), (4, True), (2, False), (10, False)):
            with self.subTest(code=code):
                events = stream(error=True, output=f'Exit code {code}')
                events[-1]['tool_use_result'] = f'Error: Exit code {code}'
                self.write(events)
                self.assertEqual(health.has_failed_slack_access(self.path), expected)

    def test_real_credential_diagnostics_require_error_evidence(self):
        for diagnostic in ('Slack authorization must be completed again',
                           'Slack rejected the long-lived credential',
                           'macOS Keychain is locked during Slack credential read',
                           'slack mcp (HTTP 401): unauthorized'):
            for is_error in (False, True):
                with self.subTest(diagnostic=diagnostic, is_error=is_error):
                    events = stream(tool='mcp__slack__slack_read_thread', error=is_error, output=diagnostic)
                    self.write(events)
                    self.assertEqual(health.has_failed_slack_access(self.path), is_error)

    def test_cursor_failure_exit_code_is_not_lost_when_text_is_absent(self):
        self.write([{'type': 'tool_call', 'subtype': 'completed', 'tool_call': {
            'shellToolCall': {'args': {'command': "bash /runtime/mcp-call.sh slack_read_thread '{}'"},
                              'result': {'failure': {'exitCode': 1}}}}}])
        self.assertTrue(health.has_failed_slack_access(self.path))

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
