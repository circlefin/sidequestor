"""Business receipts and cosmetic delivery must survive independently."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(RUNTIME))
import reaction_lifecycle as lifecycle


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = lifecycle.Lifecycle(self.root, {})
        self.item = {'item_id': 'robot_face:1000.000001', 'channel_id': 'C123'}

    def records(self):
        return json.loads(self.manager.path.read_text())['items']

    def test_delivery_failure_survives_restart_without_business_retry(self):
        self.manager.request(self.item, 'done', final=True)
        with patch.object(self.manager, 'deliver', return_value=False):
            self.manager.flush()
        self.assertTrue(self.records()[self.item['item_id']]['final'])
        with self.manager.locked() as records:
            records[self.item['item_id']]['next_retry'] = 0
        restarted = lifecycle.Lifecycle(self.root, {})
        with patch.object(restarted, 'deliver', return_value=True) as deliver:
            restarted.flush()
        deliver.assert_called_once_with('C123', '1000.000001', 'done')
        self.assertTrue(self.records()[self.item['item_id']]['confirmed'])

    def test_ack_recovered_from_manifest_after_enqueue_crash(self):
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.parent.joinpath('dispatch-test.json').write_text(json.dumps({
            'kind': 'reactions', 'items': [dict(self.item, status='nothing_to_do',
                acked_utc='2026-10-23T01:02:03.123456Z')]}))
        self.manager.recover()
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'done')

    def test_malformed_receipt_does_not_starve_valid_recovery(self):
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.parent.joinpath('dispatch-test.json').write_text(json.dumps({
            'kind': 'reactions', 'items': [None, dict(self.item, status='handled', acked_utc='bad'),
                dict(self.item, status='handled', acked_utc='2026-10-23T01:02:03.123456Z')]}))
        (self.root / 'state/pending-approvals.json').write_text('{"items": null}')
        self.manager.recover()
        self.assertTrue(self.records()[self.item['item_id']]['final'])
        self.manager.request(self.item, 'loading')
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'done')

    def test_blocked_is_retryable_and_never_done(self):
        self.manager.acknowledge(dict(self.item, status='blocked',
            acked_utc='2026-10-23T01:02:03.123456Z'))
        self.assertFalse(self.records()[self.item['item_id']]['final'])
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'blocked')
        self.manager.request(self.item, 'loading')
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'loading')

    def test_legacy_warning_cleanup_is_queued_only_once_without_repeating_business_action(self):
        self.manager.request(self.item, 'blocked')
        with self.manager.locked() as records:
            records[self.item['item_id']].update(confirmed=True, delivered='blocked')
        self.manager.request(self.item, 'loading')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
            deliver.assert_called_once_with('C123', '1000.000001', 'loading', retire_warning=True)
        record = self.records()[self.item['item_id']]
        self.assertFalse(record['final'])
        self.assertEqual(record['delivered_emoji'], 'hourglass_flowing_sand')
        self.manager.request(self.item, 'blocked')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
            deliver.assert_called_once_with('C123', '1000.000001', 'blocked')

    def test_previous_repeat_marker_is_retired_once_when_default_changes(self):
        self.manager.request(self.item, 'blocked')
        with self.manager.locked() as records:
            records[self.item['item_id']].update(confirmed=True, delivered='blocked', delivered_emoji='repeat')
        self.manager.request(self.item, 'loading')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
            deliver.assert_called_once_with('C123', '1000.000001', 'loading', retire_emojis=['repeat'])
        self.manager.request(self.item, 'blocked')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
            deliver.assert_called_once_with('C123', '1000.000001', 'blocked')
        self.assertEqual(self.records()[self.item['item_id']]['delivered_emoji'], 'clock3')

    def test_failed_review_does_not_remove_work_from_retry(self):
        with patch.object(lifecycle.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'helper')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.manager.review(self.item, 'No owning quest')
        self.assertFalse(self.records()[self.item['item_id']]['final'])
        self.assertTrue(self.records()[self.item['item_id']]['review_pending'])

    def test_completed_review_updates_status_without_replaying_action(self):
        self.manager.review(self.item, 'No owning quest')
        path = self.root / 'state/pending-approvals.json'
        queue = json.loads(path.read_text())
        queue['items'][0]['status'] = 'reviewed'
        path.write_text(json.dumps(queue))
        helper = RUNTIME / 'ledger/approval-helper.py'
        for command in ('start', 'done'):
            subprocess.run([sys.executable, str(helper), command, queue['items'][0]['id']],
                           env=self.manager.env, capture_output=True, check=True)
        self.assertEqual(json.loads(path.read_text())['items'][0]['status'], 'executed')
        self.manager.recover()
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'done')
        self.assertTrue(self.records()[self.item['item_id']]['final'])

    def test_failed_review_is_retried_without_another_worker_dispatch(self):
        import tick
        from types import SimpleNamespace
        with patch.object(lifecycle.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'helper')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.manager.review(self.item, 'No owning quest')
        t = SimpleNamespace(repo_root=self.root, env={}, dispatched=0,
                            pending_reactions=self.root / 'pending.json', log=lambda text: None,
                            _read_json=lambda *args: {'robot_face': ['1000.000001']})
        tick.dispatch_one(t, 'reactions', 60, [])
        self.assertEqual(t.dispatch_exit, 0)
        self.manager.recover()
        record = self.records()[self.item['item_id']]
        self.assertTrue(record['final'])
        self.assertFalse(record['review_pending'])

    def test_review_created_before_terminal_state(self):
        with patch.object(lifecycle.subprocess, 'run') as run:
            self.manager.review(self.item, 'No owning quest')
        payload = json.loads(run.call_args_list[-1].args[0][-1])
        self.assertEqual(payload['quest_id'], 'quest-inbox')
        self.assertEqual(payload['target']['action'], 'reaction_recovery')
        self.assertTrue(self.records()[self.item['item_id']]['final'])

    def test_real_review_helper_persists_deduplicated_inbox_item(self):
        self.manager.review(self.item, 'No owning quest')
        self.manager.review(self.item, 'Still no owning quest')
        queue = json.loads((self.root / 'state/pending-approvals.json').read_text())
        items = [i for i in queue['items'] if i['target'].get('action') == 'reaction_recovery']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['quest_id'], 'quest-inbox')
        self.assertEqual(items[0]['status'], 'pending_review')

    def test_shared_message_stays_loading_until_all_actions_stop(self):
        self.manager.request(self.item, 'done', final=True)
        second = dict(self.item, item_id='writing_hand:1000.000001')
        self.manager.request(second, 'loading')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
        self.assertTrue(all(call.args[-1] == 'loading' for call in deliver.call_args_list))
        self.manager.request(second, 'done', final=True)
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
        deliver.assert_called_once_with('C123', '1000.000001', 'done')

    def test_expand_and_process_on_same_message_keep_independent_receipts(self):
        other = dict(self.item, item_id='magic_wand:1000.000001')
        self.manager.request(self.item, 'done', final=True)
        self.manager.request(other, 'loading')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
        self.assertEqual(deliver.call_args.args[-1], 'loading')
        self.manager.request(other, 'blocked')
        with patch.object(self.manager, 'deliver', return_value=True) as deliver:
            self.manager.flush()
        self.assertEqual(deliver.call_args.args[-1], 'blocked')
        self.assertTrue(self.records()[self.item['item_id']]['final'])
        self.assertFalse(self.records()[other['item_id']]['final'])

    def test_expand_recovery_review_explains_guarded_helper(self):
        self.manager.review(dict(self.item, item_id='magic_wand:1000.000001'), 'Uncertain edit')
        queue = json.loads((self.root / 'state/pending-approvals.json').read_text())
        self.assertIn('slack-expand.py', queue['items'][0]['message_text'])
        self.assertIn('claimed approval_id', queue['items'][0]['message_text'])

    def test_new_ack_during_delivery_is_not_lost(self):
        self.manager.request(self.item, 'loading')
        def delivered(*args):
            self.manager.request(self.item, 'done', final=True)
            return True
        with patch.object(self.manager, 'deliver', side_effect=delivered):
            self.manager.flush(limit=1)
        self.assertFalse(self.records()[self.item['item_id']]['confirmed'])
        self.assertEqual(self.records()[self.item['item_id']]['desired'], 'done')

    def test_corrupt_outbox_not_overwritten(self):
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.write_text('{bad')
        with self.assertRaises(ValueError):
            self.manager.request(self.item, 'done', final=True)
        self.assertEqual(self.manager.path.read_text(), '{bad')

    def test_missing_coordinates_does_not_spawn_delivery(self):
        self.manager.request({'item_id': self.item['item_id']}, 'loading')
        with patch.object(lifecycle.subprocess, 'Popen') as spawn:
            lifecycle.kick(self.root, {})
        spawn.assert_not_called()

    def test_dispatch_refuses_completed_item_even_with_stale_pending_queue(self):
        import tick
        from types import SimpleNamespace
        self.manager.request(self.item, 'done', final=True)
        t = SimpleNamespace(repo_root=self.root, env={}, dispatched=0,
                            pending_reactions=self.root / 'pending.json', log=lambda text: None,
                            _read_json=lambda *args: {'robot_face': ['1000.000001']})
        tick.dispatch_one(t, 'reactions', 60, [])
        self.assertEqual(t.dispatch_exit, 0)

    def test_prune_final_and_held_items_preserves_only_real_work(self):
        self.manager.request(self.item, 'done', final=True)
        held = dict(self.item, item_id='writing_hand:1000.000002')
        self.manager.request(held, 'blocked')
        with self.manager.locked() as records:
            records[held['item_id']]['review_pending'] = True
        pending = self.manager.path.parent / 'pending_reactions.json'
        pending.write_text(json.dumps({'robot_face': ['1000.000001', '1000.000003'],
                                       'writing_hand': ['1000.000002']}))
        self.manager.prune_pending()
        self.assertEqual(json.loads(pending.read_text()), {'robot_face': ['1000.000003']})

    def test_dispatch_holds_corrupt_receipts(self):
        import tick
        from types import SimpleNamespace
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.write_text('{bad')
        t = SimpleNamespace(repo_root=self.root, env={}, dispatched=0,
                            pending_reactions=self.root / 'pending.json', log=lambda text: None,
                            _read_json=lambda *args: {'robot_face': ['1000.000001']})
        tick.dispatch_one(t, 'reactions', 60, [])
        self.assertEqual(t.dispatch_exit, 8)

    def test_locate_registered_item_and_reject_changed_channel(self):
        helper = RUNTIME / 'ledger/ack-watch.py'
        env = dict(os.environ, SIDEQUESTOR_WORKSPACE=str(self.root))
        def run(*args):
            return subprocess.run([sys.executable, str(helper), *args], env=env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(run('open', 'r1', 'reactions', 'reactions', json.dumps([
            {'item_id': self.item['item_id']}])).returncode, 0)
        # Use save (no emoji delivery) to test locate through real CLI without Slack.
        manifest = self.root / 'state/triage/dispatch-r1.json'
        data = json.loads(manifest.read_text())
        data['items'][0]['item_id'] = 'floppy_disk:1000.000001'
        manifest.write_text(json.dumps(data))
        self.assertEqual(run('locate', 'r1', 'floppy_disk:1000.000001', 'C123').returncode, 0)
        self.assertNotEqual(run('locate', 'r1', 'floppy_disk:1000.000001', 'C456').returncode, 0)
        self.assertNotEqual(run('locate', 'r1', 'robot_face:1000.000001', 'C123').returncode, 0)


class SurfaceTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('reaction_surface', RUNTIME / 'surfaces/react-lifecycle.py')
        self.surface = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.surface)

    def test_failed_add_preserves_previous_status(self):
        with patch.object(self.surface, '_react', return_value=False) as react:
            self.assertFalse(self.surface.advance('C123', '1000.1', 'done', 'unused', env={}))
        self.assertEqual(react.call_count, 1)
        self.assertEqual(react.call_args.args[1], 'add')

    def test_retry_marker_replaces_legacy_warning_after_successful_add(self):
        with patch.object(self.surface, '_react', return_value=True) as react:
            self.assertTrue(self.surface.advance('C123', '1000.1', 'blocked', 'unused', env={}, retire_warning=True))
        self.assertEqual(react.call_args_list[0].args[-1], 'clock3')
        self.assertIn(('remove', 'warning'), [(c.args[1], c.args[-1]) for c in react.call_args_list])

    def test_legacy_warning_is_preserved_when_it_is_a_configured_role(self):
        with patch.object(self.surface, '_react', return_value=True) as react:
            self.assertTrue(self.surface.advance('C123', '1000.1', 'done', 'unused',
                env={'SIDEQUESTOR_REACTION_SAVE_EMOJI': 'warning'}, retire_warning=True))
        self.assertNotIn(('remove', 'warning'), [(c.args[1], c.args[-1]) for c in react.call_args_list])

    def test_previous_repeat_is_removed_after_clock_is_added(self):
        with patch.object(self.surface, '_react', return_value=True) as react:
            self.assertTrue(self.surface.advance('C123', '1000.1', 'blocked', 'unused', env={}, retire_emojis=['repeat']))
        self.assertEqual(react.call_args_list[0].args[-1], 'clock3')
        self.assertIn(('remove', 'repeat'), [(c.args[1], c.args[-1]) for c in react.call_args_list])

    def test_retired_emoji_cannot_remove_an_explicit_save_marker(self):
        with patch.object(self.surface, '_react', return_value=True) as react:
            self.assertTrue(self.surface.advance('C123', '1000.1', 'blocked', 'unused',
                env={'SIDEQUESTOR_REACTION_SAVE_EMOJI': 'repeat'}, retire_emojis=['repeat']))
        self.assertNotIn(('remove', 'repeat'), [(c.args[1], c.args[-1]) for c in react.call_args_list])

    def test_failed_cleanup_is_retryable(self):
        def react(binary, verb, *args):
            return verb == 'add'
        with patch.object(self.surface, '_react', side_effect=react) as calls:
            self.assertFalse(self.surface.advance('C123', '1000.1', 'done', 'unused', env={}))
        self.assertEqual(calls.call_args_list[0].args[1], 'add')


if __name__ == '__main__':
    unittest.main()
