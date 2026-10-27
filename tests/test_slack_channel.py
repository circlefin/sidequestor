from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SURFACES = ROOT / 'src/sidequestor/runtime/yaas-triage/surfaces'


def load_module():
    spec = importlib.util.spec_from_file_location('slack_channel_test', SURFACES / 'slack-channel.py')
    module = importlib.util.module_from_spec(spec)
    saved = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


class SlackChannelTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SIDEQUESTOR_WORKSPACE': str(self.root),
            'SIDEQUESTOR_DISPATCH_TARGET': '', 'YAAS_FORCE_DRAFT': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        self.quest = self.root / 'state/quests/active/quest-test'
        self.quest.mkdir(parents=True)
        self.meta(False)

    def meta(self, allow):
        (self.quest / 'meta.json').write_text(json.dumps({'allow_send': allow}))

    def run_action(self, command='create', **payload):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.mod.main([command, json.dumps(payload)])
        return code, json.loads(out.getvalue())

    def job(self, **values):
        return self.mod.prepare('create', {'name': 'project-test', 'quest_id': 'quest-test', **values})

    def approval(self, job, **values):
        return {'id': 'approval-1', 'quest_id': 'quest-test', 'status': 'executing',
                'lease_expires_at': datetime.fromtimestamp(time.time()+600, timezone.utc).isoformat(),
                **self.mod.approval_spec(job), **values}

    def test_create_private_uses_shared_client(self):
        reply = (200, json.dumps({'ok': True, 'channel':
            {'id': 'C12345678', 'name': 'project-test', 'is_private': True}}))
        with patch.object(self.mod.client, 'slack_request', return_value=reply) as request:
            code, result = self.run_action(name='project-test', interactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['channel_id'], 'C12345678')
        self.assertTrue(result['is_private'])
        self.assertEqual(request.call_count, 1)
        args = request.call_args.args
        self.assertTrue(args[0].endswith('/conversations.create'))
        self.assertEqual(json.loads(args[3]), {'name': 'project-test', 'is_private': True})

    def test_invalid_input_never_calls_slack(self):
        for values in ({'name':'Bad Name'}, {'name':'---'}, {'name':'a'*81},
                       {'name':'valid','is_private':False}, {'name':'valid','quest_id':'../other'},
                       {'name':'valid','interactive':'true'}):
            with self.subTest(values=values), patch.object(self.mod.client, 'slack_request') as request:
                code, _ = self.run_action(**values)
                self.assertEqual(code, 1)
                request.assert_not_called()
        for command, values in [('invite', {'channel_id':'U12345678','users':['U12345678']}),
                                ('invite', {'channel_id':'C12345678','users':['person@example.com']}),
                                ('topic', {'channel_id':'C12345678','topic':'x'*251})]:
            with patch.object(self.mod.client, 'slack_request') as request:
                self.assertEqual(self.run_action(command, **values)[0], 1)
                request.assert_not_called()

    def test_policy_denies_without_network(self):
        for values, dispatch, force in [({}, '', ''), ({'interactive':True}, 'quest-test', ''),
            ({'quest_id':'quest-test'}, 'quest-other', ''),
            ({'quest_id':'quest-test'}, '', ''), ({'interactive':True}, '', '1'),
            ({'quest_id':'quest-test'}, 'reactions', ''),
            ({'interactive':True, 'quest_id':'quest-test'}, '', '')]:
            with self.subTest(values=values, dispatch=dispatch), patch.dict(os.environ,
                {'SIDEQUESTOR_DISPATCH_TARGET':dispatch, 'YAAS_FORCE_DRAFT':force}), \
                patch.object(self.mod.client, 'slack_request') as request:
                self.assertEqual(self.run_action(name='valid', **values)[0], 2)
                request.assert_not_called()

    def test_send_enabled_active_quest_logs(self):
        self.meta(True)
        with patch.object(self.mod, 'mutate', return_value=(0, {'channel_id':'C12345678'})):
            code, result = self.run_action(name='valid', quest_id='quest-test')
        self.assertEqual(code, 0)
        self.assertTrue(result['logged'])
        event = json.loads((self.quest/'timeline.ndjson').read_text())
        self.assertEqual(event['target']['name'], 'valid')
        self.assertEqual(event['channel_id'], 'C12345678')

    def test_exact_approval_and_binding_even_with_allow_send(self):
        job = self.job(approval_id='approval-1')
        item = self.approval(job)
        with patch.object(self.mod.approval_store, 'read_queue', return_value={'items':[item]}):
            self.assertIsNone(self.mod.policy_reason(job))
            changed = self.job(name='different', approval_id='approval-1')
            self.assertIsNotNone(self.mod.policy_reason(changed))
            self.meta(True)
            self.assertIsNotNone(self.mod.policy_reason(changed))
        for changes in ({'status':'reviewed'}, {'quest_id':'other'},
                        {'lease_expires_at':'2000-01-01T00:00:00Z'},
                        {'lease_expires_at':'2099-01-01T00:00:00'}, {'message_text':'different'},
                        {'target':{'surface':'slack','action':'channel_create','name':'project-test','is_private':False}}):
            with patch.object(self.mod.approval_store, 'read_queue',
                              return_value={'items':[{**item, **changes}]}):
                self.assertIsNotNone(self.mod.policy_reason(job))
        with patch.object(self.mod.approval_store, 'read_queue', side_effect=OSError('unreadable')):
            self.assertIsNotNone(self.mod.policy_reason(job))

    def test_inactive_and_invalid_metadata_deny(self):
        self.meta(True)
        (self.quest/'meta.json').write_text('[]')
        self.assertIsNotNone(self.mod.policy_reason(self.job()))
        (self.quest/'meta.json').unlink()
        self.quest.rmdir()
        self.assertIsNotNone(self.mod.policy_reason(self.job()))

    def test_invite_topic_purpose_and_approval_fields(self):
        for action, values, body in [
            ('invite', {'users':['U12345679','U12345678','U12345678']}, {'users':'U12345678,U12345679'}),
            ('topic', {'topic':'new topic'}, {'topic':'new topic'}),
            ('purpose', {'purpose':'description'}, {'purpose':'description'})]:
            job = self.mod.prepare(action, {'channel_id':'G12345678', **values})
            self.assertEqual(job['body'], {'channel':'G12345678', **body})
            self.assertEqual(self.mod.approval_spec(job)['target'],
                {'surface':'slack','action':'channel_'+action,'channel':'G12345678',**body})
            with patch.object(self.mod.client, 'slack_request', return_value=(200,'{"ok":true}')) as req:
                self.assertEqual(self.mod.mutate(job)[0], 0)
                self.assertTrue(req.call_args.args[0].endswith(self.mod.METHODS[action]))

    def test_approval_spec_is_read_only(self):
        with patch.object(self.mod.client, 'slack_request') as request:
            code, spec = self.run_action('approval-spec', action='create', name='valid')
            self.assertEqual(code, 0)
            self.assertTrue(spec['target']['is_private'])
            request.assert_not_called()

    def test_unknown_outcomes_are_never_retried(self):
        for reply in [(500,'{"ok":false,"error":"internal_error"}'), (200,'not-json'),
                      (200,'{"ok":true}'), (200,'{"ok":false}'),
                      (200,'{"ok":false,"error":"fatal_error"}'),
                      (200,'{"ok":true,"channel":{"id":"C12345678","is_private":false}}')]:
            with self.subTest(reply=reply), patch.object(self.mod.client, 'slack_request', return_value=reply) as req:
                code, result = self.mod.mutate(self.job())
                self.assertEqual(code, 3)
                self.assertTrue(result['outcome_unknown'])
                self.assertEqual(req.call_count, 1)
        with patch.object(self.mod.client, 'slack_request', side_effect=TimeoutError) as req:
            self.assertEqual(self.mod.mutate(self.job())[0], 3)
            self.assertEqual(req.call_count, 1)

    def test_known_errors_and_rate_limit(self):
        for reply, expected in [((429,''),4), ((200,'{"ok":false,"error":"ratelimited"}'),4),
            ((200,'{"ok":false,"error":"name_taken"}'),2),
            ((200,'{"ok":false,"error":"missing_scope","needed":"groups:write"}'),2)]:
            with patch.object(self.mod.client, 'slack_request', return_value=reply):
                self.assertEqual(self.mod.mutate(self.job())[0], expected)

    def test_partial_invite_errors_and_credential_failure(self):
        details = [{'user':'U12345678', 'error':'cant_invite'}]
        reply = (200, json.dumps({'ok':False, 'error':'failed_for_some_users', 'errors':details}))
        job = self.mod.prepare('invite', {'channel_id':'C12345678','users':['U12345678']})
        with patch.object(self.mod.client, 'slack_request', return_value=reply):
            code, result = self.mod.mutate(job)
        self.assertEqual(code, 3)
        self.assertEqual(result['errors'], details)
        with patch.object(self.mod.client, 'slack_request',
                          side_effect=self.mod.client.SlackCredentialError('missing credential')):
            code, result = self.mod.mutate(job)
        self.assertEqual(code, 2)
        self.assertEqual(result['error'], 'missing credential')

    def test_log_failure_reports_remote_success(self):
        self.meta(True)
        with patch.object(self.mod, 'mutate', return_value=(0, {'channel_id':'C12345678'})) as mutate, \
             patch.object(self.mod, 'append_timeline', side_effect=OSError('disk full')):
            code, result = self.run_action(name='valid', quest_id='quest-test')
        self.assertEqual(code, 3)
        self.assertTrue(result['remote_succeeded'])
        self.assertEqual(mutate.call_count, 1)

    def test_real_approval_write_review_claim_and_execution(self):
        (self.quest / 'watch.json').write_text('{"watches":[]}')
        spec = self.mod.approval_spec(self.job())
        helper = SURFACES.parent / 'ledger/approval-helper.py'
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        written = subprocess.run([sys.executable, str(helper), 'write',
            json.dumps({'quest_id':'quest-test', **spec})], env=env,
            capture_output=True, text=True, check=True)
        approval_id = written.stdout.strip()
        self.assertTrue(approval_id)
        review_code = """
import sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import approval_store, approval_state
approval_store.mutate_item(sys.argv[2], lambda item: approval_state.apply_transition(
    item, 'review', {}, datetime.now(timezone.utc)))
"""
        subprocess.run([sys.executable, '-c', review_code, str(SURFACES.parent), approval_id],
                       env=env, capture_output=True, text=True, check=True)
        claimed = subprocess.run([sys.executable, str(helper), 'start', approval_id],
                       env=env, capture_output=True, text=True, check=True)
        self.assertEqual(claimed.stdout.strip(), 'ok')
        execute_code = """
import importlib.util, json, sys
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('channel_roundtrip', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
reply = json.dumps({'ok':True, 'channel':{'id':'C12345678','name':'project-test','is_private':True}})
with patch.object(module.client, 'slack_request', return_value=(200, reply)):
    sys.exit(module.main(['create', sys.argv[2]]))
"""
        executed = subprocess.run([sys.executable, '-c', execute_code, str(SURFACES / 'slack-channel.py'),
            json.dumps({'quest_id':'quest-test','approval_id':approval_id,'name':'project-test'})],
            env=env, capture_output=True, text=True)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
        self.assertTrue(json.loads(executed.stdout)['logged'])
        event = json.loads((self.quest / 'timeline.ndjson').read_text().splitlines()[-1])
        self.assertEqual(event['approval_id'], approval_id)
        self.assertEqual(event['event'], 'executed')
        self.assertEqual(event['message_text'], spec['message_text'])
        self.assertNotIn('logged', event)
        closed = subprocess.run([sys.executable, str(helper), 'done', approval_id,
            'C12345678', 'Created private channel'], env=env, capture_output=True, text=True)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        queue = json.loads((self.root / 'state/pending-approvals.json').read_text())
        self.assertEqual(next(i for i in queue['items'] if i['id'] == approval_id)['status'], 'executed')

    def test_oauth_scopes_and_real_cli_route(self):
        from sidequestor import cli
        self.assertEqual(cli.LEGACY_COMMANDS['slack-channel'], 'yaas-triage/surfaces/slack-channel.py')
        config = json.loads((SURFACES.parent/'setup/yaas-app-config.json').read_text())
        self.assertTrue({'channels:write','channels:write.invites','groups:write',
                         'groups:write.invites','groups:write.topic'} <= set(config['user_scopes']))
        manifest = subprocess.run([sys.executable, '-m', 'sidequestor', 'setup', '--manifest'],
                                  capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        generated_scopes = [line.strip()[2:] for line in manifest.stdout.splitlines()
                            if line.startswith('      - ')]
        self.assertEqual(generated_scopes, config['user_scopes'])
        self.assertEqual(len(generated_scopes), len(set(generated_scopes)))


if __name__ == '__main__':
    unittest.main()
