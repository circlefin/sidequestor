"""Expand writes must be scoped, owned, current, and recoverable without replay."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / 'surfaces'))
spec = importlib.util.spec_from_file_location('slack_expand_test', RUNTIME / 'surfaces/slack-expand.py')
expand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expand)


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'SIDEQUESTOR_DISPATCH_TARGET': 'reactions'}
        self.payload = dict(run_id='r1', item_id='magic_wand:1000.000001', channel_id='C123',
                            message_ts='1000.000001', action='inspect')
        path = self.root / 'state/triage/dispatch-r1.json'
        path.parent.mkdir(parents=True)
        self.manifest = dict(run_id='r1', target='reactions', kind='reactions', items=[
            dict(item_id=self.payload['item_id'], channel_id='C123', acked_utc=None)])
        path.write_text(json.dumps(self.manifest))
        self.message = dict(ts=self.payload['message_ts'], user='U1', text='Please \\expand this', edited={'ts': '1001.000001'})
        self.edits = []
        self.mock = patch.object(expand, 'api', side_effect=self.api)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def api(self, method, parameters=None):
        if method == 'auth.test':
            return dict(ok=True, user_id='U1')
        if method == 'conversations.replies':
            return dict(ok=True, messages=[dict(self.message)])
        if method == 'chat.update':
            self.edits.append(parameters)
            self.message['text'] = parameters['text']
            self.message['edited'] = {'ts': '1002.000001'}
            return dict(ok=True, ts=self.message['ts'])
        raise AssertionError(method)

    def apply(self):
        return expand.expand(self.root, dict(self.payload, action='apply', expected_text='Please \\expand this',
                            expected_revision='1001.000001', message='Expanded, verified text.'), self.env)

    def test_edit_is_independent_from_business_ack_and_retry_does_not_edit_again(self):
        snapshot = expand.expand(self.root, self.payload, self.env)
        self.assertEqual(snapshot['revision'], '1001.000001')
        self.assertFalse(self.apply()['already_edited'])
        self.assertTrue(self.apply()['already_edited'])
        self.assertEqual(len(self.edits), 1)
        original = json.loads((self.root / 'state/triage/dispatch-r1.json').read_text())
        self.assertIsNone(original['items'][0]['acked_utc'])

    def test_other_user_message_cannot_be_edited(self):
        self.message['user'] = 'U2'
        with self.assertRaisesRegex(expand.Blocked, 'authenticated Slack user'):
            self.apply()
        self.assertFalse(self.edits)

    def test_intervening_edit_with_same_text_is_still_detected(self):
        self.message['edited']['ts'] = '1003.000001'
        with self.assertRaisesRegex(expand.Blocked, 'changed while'):
            self.apply()
        self.assertFalse(self.edits)

    def test_wrong_dispatch_or_coordinates_cannot_edit(self):
        for changes in ({'item_id': 'robot_face:1000.000001'}, {'channel_id': 'C999'}, {'run_id': '../r1'}):
            with self.subTest(changes=changes), self.assertRaises(expand.Blocked):
                expand.expand(self.root, dict(self.payload, **changes), self.env)
        with self.assertRaises(expand.Blocked):
            expand.expand(self.root, self.payload, {})
        self.assertFalse(self.edits)

    def test_unknown_outcome_reconciles_delivered_edit_without_repeating(self):
        original = self.api
        def fail_after_write(method, parameters=None):
            result = original(method, parameters)
            if method == 'chat.update':
                raise OSError('Connection lost after delivery')
            return result
        with patch.object(expand, 'api', side_effect=fail_after_write):
            with self.assertRaises(OSError):
                self.apply()
        self.assertTrue(self.apply()['already_edited'])
        self.assertEqual(len(self.edits), 1)

    def test_reply_is_found_after_parent_page(self):
        original = self.api
        def paged(method, parameters=None):
            if method == 'conversations.replies' and not parameters.get('cursor'):
                return dict(ok=True, messages=[dict(ts='999.000001', user='U2', text='Parent')],
                            response_metadata=dict(next_cursor='page2'))
            return original(method, parameters)
        with patch.object(expand, 'api', side_effect=paged):
            result = expand.expand(self.root, dict(self.payload, thread_ts='999.000001'), self.env)
        self.assertEqual(result['text'], self.message['text'])

    def test_user_edit_after_success_is_not_overwritten(self):
        self.apply()
        self.message['text'] = 'User changed this after expansion'
        result = self.apply()
        self.assertTrue(result['changed_after_edit'])
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(self.message['text'], 'User changed this after expansion')

    def test_only_claimed_human_review_can_retry_old_uncertain_edit(self):
        path = self.root / 'state/triage/reaction-edits.json'
        path.write_text(json.dumps({'items': {self.payload['item_id']: {
            'channel_id': 'C123', 'before_hash': expand.digest(self.message['text']),
            'after_hash': expand.digest('Desired'), 'before_revision': '1001.000001', 'phase': 'prepared',
            'prepared_at': expand.time.time() - 120}}}))
        approval = dict(id='review1', quest_id='quest-inbox', status='executing',
                        lease_expires_at='2099-01-01T00:00:00Z', target=dict(
                            action='reaction_recovery', system_key='reaction:' + self.payload['item_id'],
                            channel_id='C123', message_ts=self.payload['message_ts']))
        (self.root / 'state/pending-approvals.json').write_text(json.dumps({'items': [approval]}))
        payload = dict(self.payload, approval_id='review1', action='apply',
                       expected_text=self.message['text'], expected_revision='1001.000001', message='Desired')
        with self.assertRaises(expand.Blocked):
            expand.expand(self.root, payload, self.env)
        result = expand.expand(self.root, payload, {'SIDEQUESTOR_DISPATCH_TARGET': 'quest-inbox'})
        self.assertTrue(result['edited'])
        self.assertEqual(len(self.edits), 1)

    def test_api_server_error_is_never_a_definite_write_rejection(self):
        self.mock.stop()
        try:
            with patch.object(expand.client, 'slack_request', return_value=(500, '{"ok":false,"error":"internal_error"}')):
                with self.assertRaises(expand.Blocked) as raised:
                    expand.api('chat.update', {})
            self.assertNotIsInstance(raised.exception, expand.Rejected)
        finally:
            self.mock.start()

    def test_reviewed_retry_cannot_overwrite_a_user_revert(self):
        original_text = self.message['text']
        self.test_only_claimed_human_review_can_retry_old_uncertain_edit()
        path = self.root / 'state/triage/reaction-edits.json'
        data = json.loads(path.read_text())
        record = data['items'][self.payload['item_id']]
        record.update(phase='prepared', prepared_at=expand.time.time()-120,
                      before_hash=expand.digest(original_text), before_revision='1001.000001')
        path.write_text(json.dumps(data))
        self.message.update(text=original_text, edited={'ts': '1004.000001'})
        with self.assertRaises(expand.Blocked):
            expand.expand(self.root, dict(self.payload, approval_id='review1'),
                          {'SIDEQUESTOR_DISPATCH_TARGET': 'quest-inbox'})
        resolution = expand.expand(self.root, dict(self.payload, approval_id='review1', action='resolve', resolution='abandon'),
                                   {'SIDEQUESTOR_DISPATCH_TARGET': 'quest-inbox'})
        self.assertTrue(resolution['resolved'])
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(self.message['text'], original_text)

    def test_review_can_accept_unchanged_text_without_claiming_an_edit(self):
        self.test_only_claimed_human_review_can_retry_old_uncertain_edit()
        path = self.root / 'state/triage/reaction-edits.json'
        data = json.loads(path.read_text())
        data['items'][self.payload['item_id']].update(phase='prepared', prepared_at=expand.time.time()-120,
                                                   before_hash=expand.digest(self.message['text']))
        path.write_text(json.dumps(data))
        result = expand.expand(self.root, dict(self.payload, approval_id='review1', action='resolve', resolution='accept_current'),
                               {'SIDEQUESTOR_DISPATCH_TARGET': 'quest-inbox'})
        self.assertTrue(result['resolved'])
        self.assertFalse(result['edited'])
        self.assertTrue(result['accepted_unchanged'])
        self.assertEqual(len(self.edits), 1)

    def test_uncertain_undelivered_edit_is_held_for_reconciliation(self):
        original = self.api
        def fail_before_write(method, parameters=None):
            if method == 'chat.update':
                raise OSError('Connection lost')
            return original(method, parameters)
        with patch.object(expand, 'api', side_effect=fail_before_write):
            with self.assertRaises(OSError):
                self.apply()
        with self.assertRaisesRegex(expand.Blocked, 'uncertain'):
            self.apply()
        self.assertFalse(self.edits)
