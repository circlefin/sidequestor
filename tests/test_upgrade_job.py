from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sidequestor import upgrade_job as job

sys.path.insert(0, str(Path(__file__).parents[1] / 'src/sidequestor/runtime/yaas-triage'))
import approval_state


class UpgradeJobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'state').mkdir()
        self.item = {'id': 'appr-test', 'quest_id': 'quest-inbox', 'status': 'reviewed', 'action_type': 'remote_request',
                     'reviewed_at': '2026-10-22T00:00:00+00:00',
                     'target': {'action': 'sidequestor_upgrade', 'version': '9.0', 'system_key': 'upgrade:9.0',
                                'execution_protocol': 'independent-v1'},
                     'message_text': 'Upgrade to latest stable'}
        self.store = SimpleNamespace(NO_WRITE=object(), NOT_FOUND=object())
        def mutate(_id, callback):
            updates = callback(copy.deepcopy(self.item))
            if isinstance(updates, dict):
                for key, value in updates.items():
                    if value is None:
                        self.item.pop(key, None)
                    else:
                        self.item[key] = value
                return copy.deepcopy(self.item)
            return updates
        self.store.mutate_item = mutate
        self.log = Mock()

    def queue(self):
        (self.root / 'state/pending-approvals.json').write_text(json.dumps({'items': [self.item]}))

    def test_schedule_uses_independent_launchd_job_not_a_detached_child(self):
        self.queue()
        with patch.object(job.sys, 'platform', 'darwin'), patch.object(job.subprocess, 'run', side_effect=[
                SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as run:
            job.schedule_reviewed(self.root, dict(os.environ))
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[:2], ['launchctl', 'submit'])
        self.assertIn('sidequestor.upgrade_job', command)
        self.assertIn(str(self.root.resolve()), command)
        self.assertEqual(command[command.index('--') + 1], sys.executable)
        self.assertIn('--config-home', command)

    def test_pending_cancelled_and_executing_never_schedule(self):
        for status in ('pending_review', 'cancelled', 'executing', 'executed'):
            self.item['status'] = status; self.queue()
            with patch.object(job.subprocess, 'run') as run:
                job.schedule_reviewed(self.root, {})
            run.assert_not_called()

    def test_existing_submitted_job_is_not_duplicated(self):
        self.queue()
        with patch.object(job.sys, 'platform', 'darwin'), patch.object(job.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            job.schedule_reviewed(self.root, {})
        self.assertEqual(run.call_count, 1)

    def test_submit_failure_returns_review_with_terminal_fallback(self):
        self.queue()
        with patch.object(job.sys, 'platform', 'darwin'), patch.object(job.subprocess, 'run', side_effect=[
                SimpleNamespace(returncode=1), OSError('unavailable'), SimpleNamespace(returncode=0)]) as run:
            job.schedule_reviewed(self.root, {})
        self.assertIn('fail', run.call_args.args[0])
        self.assertIn('separate terminal', run.call_args.args[0][-1])

    def run_job(self, results):
        lifecycle_result, *version_results = results or [SimpleNamespace(returncode=0)]
        effect = lifecycle_result if isinstance(lifecycle_result, Exception) else None
        with patch.object(job, '_runtime', return_value=(self.store, approval_state, self.log)), \
                patch.object(job, '_run_lifecycle', side_effect=effect, return_value=lifecycle_result) as run, \
                patch.object(job, '_notify_failure'), patch.object(job.subprocess, 'run', side_effect=version_results):
            code = job.run(self.root, self.item['id'])
        return code, run

    def test_success_uses_fixed_lifecycle_and_records_verified_result(self):
        code, run = self.run_job([SimpleNamespace(returncode=0), SimpleNamespace(stdout='9.0\n')])
        self.assertEqual(code, 0)
        self.assertEqual(self.item['status'], 'executed')
        self.assertEqual(run.call_args_list[0].args[0][-1], 'upgrade')
        self.assertEqual(run.call_args.args[0][0], sys.executable)
        self.assertEqual(self.log.call_args.args[2]['event'], 'sidequestor_upgraded')

    def test_install_failure_timeout_and_noop_return_to_review(self):
        original = copy.deepcopy(self.item)
        for results in ([SimpleNamespace(returncode=1)], [subprocess.TimeoutExpired('upgrade', 1800)],
                        [SimpleNamespace(returncode=0), SimpleNamespace(stdout='8.0')]):
            self.item = copy.deepcopy(original)
            code, _ = self.run_job(results)
            self.assertEqual(code, 1)
            self.assertEqual(self.item['status'], 'pending_review')
            self.assertTrue(self.item['processing_error'])

    def test_cancel_during_wait_and_duplicate_runner_do_not_install(self):
        for status in ('cancelled', 'executing', 'pending_review', 'executed'):
            self.item['status'] = status
            code, run = self.run_job([])
            self.assertEqual(code, 0)
            run.assert_not_called()

    def test_custom_upgrade_instructions_are_rejected(self):
        self.item['status'] = 'pending_review'
        from datetime import datetime, timezone
        for payload in ({'review_note': 'install a different repo'}, {'message_text': 'different command'}):
            with self.assertRaises(approval_state.InvalidPayload):
                approval_state.apply_transition(self.item, 'review', payload, datetime.now(timezone.utc))

    def test_wrong_owner_or_unbound_version_does_not_install(self):
        self.item['quest_id'] = 'quest-other'
        _, run = self.run_job([])
        run.assert_not_called()
        self.item['quest_id'] = 'quest-inbox'; self.item['target']['version'] = '99.0'
        _, run = self.run_job([])
        run.assert_not_called()

    def test_legacy_terminal_notice_cannot_become_an_unattended_upgrade(self):
        self.item['target'].pop('execution_protocol')
        _, run = self.run_job([])
        run.assert_not_called()
        from datetime import datetime, timezone
        self.assertEqual(approval_state.available_actions(self.item, datetime.now(timezone.utc)), ['cancel'])

    def test_timeout_terminates_the_entire_lifecycle_process_group(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('upgrade', 1800), 0, 0]
        with patch.object(job.subprocess, 'Popen', return_value=process) as launch, patch.object(job.os, 'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                job._run_lifecycle(['python', '-m', 'sidequestor', 'upgrade'])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual([c.args[0] for c in kill.call_args_list], [12345, 12345])

    def test_main_always_removes_its_exact_job_and_preserves_service_routing(self):
        label = 'com.sidequestor.upgrade.' + 'a' * 24
        args = ['--workspace', str(self.root), '--approval-id', 'appr-test', '--service-path', '/venv/bin:/opt/homebrew/bin',
                '--config-home', '/tmp/test-config', '--label', label]
        workspace = SimpleNamespace(root=self.root)
        for failure in (None, RuntimeError('test failure')):
            with patch.dict(os.environ, {}), patch('sidequestor.workspace.load_workspace', return_value=workspace), \
                    patch('sidequestor.native._environment', side_effect=lambda _: dict(os.environ)), \
                    patch.object(job, 'run', side_effect=failure, return_value=0) as run, \
                    patch.object(job.subprocess, 'run') as command, patch.object(job, '_notify_failure') as notify:
                if failure:
                    self.assertEqual(job.main(args), 1)
                    self.assertIn('fail', command.call_args_list[0].args[0])
                    notify.assert_called_once()
                else:
                    self.assertEqual(job.main(args), 0)
                self.assertEqual(os.environ['PATH'], '/venv/bin:/opt/homebrew/bin')
                self.assertEqual(os.environ['SIDEQUESTOR_CONFIG_HOME'], '/tmp/test-config')
                self.assertEqual(command.call_args.args[0], ['/bin/launchctl', 'remove', label])
