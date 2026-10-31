from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(TRIAGE))
from dashboard_visualizer import build_visualization
sys.path.remove(str(TRIAGE))


class QuestVisualizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.quest = self.root / 'state/quests/active/quest-test'
        self.quest.mkdir(parents=True)
        self.timeline = self.quest / 'timeline.ndjson'
        self.detail = {'meta': {'title': 'Test'}, 'watches': [{'watch_id': 'w1'}]}

    def read(self, qid='quest-test'):
        return build_visualization(self.root, qid, self.detail)

    def test_preserves_bounded_resource_aliases_without_unknown_payloads(self):
        self.timeline.write_text(json.dumps({'event': 'info_logged', 'ts': '2026-01-01',
            'prs': [{'number': 17, 'title': 'Readable title', 'secret': 'omit'}] * 30,
            'jira': ['ABC-42', 'XYZ-9'], 'new_file_id': 'copy123', 'doc_title': 'Document',
            'ticket_url': 'https://example.atlassian.net/browse/ABC-42',
            'message_text': 'omit', 'unknown': {'token': 'omit'}}) + '\n')
        row = self.read()['events'][0]
        self.assertEqual(len(row['prs']), 20)
        self.assertEqual(row['prs'][0], {'number': 17, 'title': 'Readable title'})
        self.assertEqual(row['jira'], ['ABC-42', 'XYZ-9'])
        self.assertEqual(row['new_file_id'], 'copy123')
        self.assertNotIn('unknown', row)
        self.assertNotIn('message_text', row)

    def test_reads_rotated_timeline_and_deduplicates_rotation_overlap(self):
        first = {'event_id': 'created', 'event': 'created', 'ts': '2026-07-08T03:19:11Z'}
        overlap = {'event_id': 'reply', 'event': 'info_received', 'ts': '2026-07-09T03:19:11Z', 'channel_id': 'C12345678'}
        (self.quest / 'timeline.archive.ndjson').write_text('\n'.join(map(json.dumps, [first, overlap])) + '\n')
        self.timeline.write_text(json.dumps(overlap) + '\n')
        result = self.read()
        self.assertEqual([e['event_id'] for e in result['events']], ['created', 'reply'])
        self.assertEqual(result['coverage']['timeline_archived'], 2)
        self.assertEqual(result['coverage']['timeline'], 2)

    def test_historical_watch_snapshot_identity_requires_recorded_hash_match(self):
        import hashlib
        watch = {'type': 'slack_channel', 'channel_id': 'C12345678', 'last_checked_ts': '100'}
        canonical = json.dumps({k: v for k, v in watch.items() if k != 'last_checked_ts'}, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        wid = 'watch-' + hashlib.sha256(f'quest-test\0{0}\0{canonical}'.encode()).hexdigest()[:16]
        (self.quest / 'watch.json.bak-20260725135707').write_text(json.dumps({'watches': [watch, {'type': 'schedule'}]}))
        state = self.root / 'state'
        (state / 'run-log.ndjson').write_text(json.dumps({'event': 'gate_dispatch', 'ts': '2026-08-03T04:12:54Z', 'dirty_watches': [{'quest_id': 'quest-test', 'watch_id': wid, 'type': 'slack_channel'}]}) + '\n')
        result = self.read()
        a, b = result['historical_watches']
        self.assertEqual(a['watch_id'], wid)
        self.assertTrue(a['historical'])
        self.assertTrue(b['watch_id'].startswith('snapshot:'))
        self.assertIn('observed_at', a)
        self.assertNotIn('created_ts', a)
        self.detail['watches'][0]['created_ts'] = '1760000000.1'
        self.assertEqual(self.read()['watches'][0]['created_ts'], '1760000000.1')

    def test_all_registered_watch_criteria_survive_projection(self):
        watches = []
        for path in sorted((TRIAGE / 'checkers').glob('*.watch.json')):
            shape = json.loads(path.read_text())
            watches.append(dict(shape['checker_example'], watch_id=path.stem))
        self.detail['watches'] = watches
        result = self.read()
        self.assertEqual(len(result['watch_types']), len(watches))
        for source, projected in zip(watches, result['watches']):
            for key, value in source.items():
                if key != 'last_checked_ts':
                    self.assertEqual(projected.get(key), value, (source['type'], key))

    def test_nested_resource_subjects_and_retirement_identity_are_safe(self):
        row = {'event': 'executed', 'ts': '2026-01-01', 'surface': 'gmail',
               'resource': {'surface': 'gmail', 'thread_id': 'thread-1', 'subject': 'Planning',
                            'access_token': 'never-project', 'message_text': 'never-project'},
               'watch_identity': {'repo': 'example/repo', 'search': 'is:open', 'api_key': 'never-project'},
               'secret_unknown_field': 'never-project'}
        self.timeline.write_text(json.dumps(row) + '\n')
        projected = self.read()['events'][0]
        self.assertEqual(projected['resource'], {'surface': 'gmail', 'thread_id': 'thread-1', 'subject': 'Planning'})
        self.assertEqual(projected['watch_identity'], {'repo': 'example/repo', 'search': 'is:open'})
        self.assertNotIn('never-project', json.dumps(projected))

    def test_oversized_watch_configuration_reports_incomplete_evidence(self):
        (self.quest / 'watch.json').write_text(json.dumps({'watches': [], 'padding': 'x' * (1024 * 1024)}))
        result = build_visualization(self.root, 'quest-test')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['coverage']['skipped_files'], 1)

    def test_external_timeline_symlink_is_not_projected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / 'secret.ndjson'
            outside.write_text(json.dumps({'event': 'note', 'note': 'not-workspace-data'}) + '\n')
            self.timeline.symlink_to(outside)
            result = self.read()
            self.assertEqual(result['events'], [])
            self.assertEqual(result['coverage']['skipped_files'], 1)

    def test_identical_rows_within_one_timeline_are_retained(self):
        row = json.dumps({'event': 'info_logged', 'ts': '2026-01-01', 'note': 'same-second'}) + '\n'
        self.timeline.write_text(row * 2)
        self.assertEqual(len(self.read()['events']), 2)

    def test_legacy_watch_snapshots_deduplicate_by_rule_identity(self):
        watch = {'type': 'email', 'query': 'from:person@example.test'}
        for date in ['20260725135707', '20260726135707']:
            (self.quest / ('watch.json.bak-' + date)).write_text(json.dumps({'watches': [watch]}))
        result = self.read()
        self.assertEqual(len(result['historical_watches']), 1)
        self.assertTrue(result['historical_watches'][0]['watch_id'].startswith('snapshot:'))
        self.assertTrue(result['historical_watches'][0]['observed_at'].startswith('2026-07-25'))

    def test_url_secrets_and_shortened_descriptions_are_explicit(self):
        row = {'event': 'info_logged', 'ts': '2026-01-01', 'note': 'x' * 1600,
               'url': 'https://username:password@example.test/resource?token=sensitivevalue',
               'query': 'https://example.test/?api_key=anothersecret'}
        self.timeline.write_text(json.dumps(row) + '\n')
        projected = self.read()['events'][0]
        self.assertTrue(projected['projection_truncated'])
        self.assertNotIn('sensitivevalue', json.dumps(projected))
        self.assertNotIn('anothersecret', json.dumps(projected))
        self.assertNotIn('username:password', json.dumps(projected))
        self.assertEqual(len(projected['note']), 1500)

    def test_tail_budget_keeps_first_complete_line(self):
        first = json.dumps({'event': 'note', 'ts': '2026-01-01', 'note': 'old'}) + '\n'
        last = json.dumps({'event': 'note', 'ts': '2026-01-02', 'note': 'retained'}) + '\n'
        self.timeline.write_text(first + last)
        with patch('dashboard_visualizer.MAX_BYTES', len(last)):
            result = self.read()
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['events'][0]['note'], 'retained')
        self.assertTrue(result['truncated'])

    def test_reads_historical_evidence_and_keeps_state_unchanged(self):
        entry = {'id': 'a', 'event': 'message_sent', 'ts': '2026-01-01',
                 'parent_event_id': 'source', 'watch_id': 'w1', 'approval_id': 'approval',
                 'message_text': 'not needed on the map', 'secret_unknown_field': 'excluded'}
        self.timeline.write_text(json.dumps(entry)+'\nnot json\n')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.read()
        self.assertEqual(result['events'][0]['parent_event_id'], 'source')
        self.assertNotIn('message_text', result['events'][0])
        self.assertNotIn('secret_unknown_field', result['events'][0])
        self.assertEqual(result['watches'], self.detail['watches'])
        self.assertFalse(result['truncated'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_standalone_adapter_does_not_require_dashboard_detail(self):
        (self.quest / 'meta.json').write_text('{"title":"Standalone"}')
        (self.quest / 'watch.json').write_text('{"watches":[{"watch_id":"w1"}]}')
        result = build_visualization(self.root, 'quest-test')
        self.assertEqual(result['title'], 'Standalone')
        self.assertEqual(result['watches'], [{'watch_id':'w1'}])

    def test_event_and_byte_caps_are_explicit(self):
        self.timeline.write_text(''.join(json.dumps({'id': str(i), 'event': 'note'})+'\n' for i in range(5005)))
        result = self.read()
        self.assertEqual(len(result['events']), 5000)
        self.assertEqual(result['events'][0]['id'], '5')
        self.assertTrue(result['truncated'])
        with patch('dashboard_visualizer.MAX_BYTES', 100):
            result = self.read()
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['events']), 10)

    def test_recovers_fired_watches_from_run_log_without_fabricating_causality(self):
        (self.root / 'state/run-log.ndjson').write_text(json.dumps({
            'ts':'2026-01-01T00:00:00Z','event':'gate_dispatch',
            'targets':['quest-test','another-quest'], 'dirty_watches':[
                {'quest_id':'quest-test','watch_id':'w1','type':'slack_thread','advance_to':'1767225500'},
                {'quest_id':'another-quest','watch_id':'other','type':'email'}]})+'\n')
        self.timeline.write_text(json.dumps({'ts':'2026-01-01T00:00:10Z',
            'event':'message_sent','source_file_id':'doc-a','target_file_id':'doc-b'})+'\n'+
            json.dumps({'ts':'2026-02-01T00:00:00Z','event':'executed'})+'\n')
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        data=self.read()
        self.assertEqual(data['coverage']['triggers'],1)
        self.assertEqual(data['coverage']['dispatches'],1)
        trigger=next(e for e in data['events'] if e['event']=='watch_fired')
        dispatch=next(e for e in data['events'] if e['event']=='dispatch_started')
        action=next(e for e in data['events'] if e['event']=='message_sent')
        self.assertEqual(dispatch['parent_event_ids'],[trigger['event_id']])
        self.assertEqual(action['associated_dispatch_id'],dispatch['event_id'])
        self.assertNotIn('parent_event_id',action)
        self.assertEqual(action['source_file_id'],'doc-a')
        self.assertNotIn('associated_dispatch_id',next(e for e in data['events'] if e['event']=='executed'))
        self.assertFalse(any(e.get('watch_id')=='other' for e in data['events']))
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_monthly_archives_recover_old_triggers_and_deduplicate_current_log(self):
        row={'ts':'2026-01-01T00:00:00Z','event':'gate_dispatch', 'dirty_watches':[
          {'quest_id':'quest-test','watch_id':'w1','type':'schedule'}]}
        state=self.root/'state'
        (state/'run-log.ndjson').write_text(json.dumps(row)+'\n')
        (state/'run-log-archive-2026-01.ndjson').write_text(json.dumps(row)+'\n'+json.dumps({
          **row,'ts':'2025-12-01T00:00:00Z'})+'\n')
        data=self.read()
        self.assertEqual(data['coverage']['triggers'],2)
        self.assertEqual(data['coverage']['dispatches'],2)
        self.assertEqual(data['coverage']['run_log_files'],2)
        self.assertEqual(len(set(e['event_id'] for e in data['events'])),len(data['events']))
        with patch('dashboard_visualizer.MAX_RUN_BYTES',20):
            self.assertTrue(self.read()['truncated'])

    def test_irrelevant_run_rows_are_filtered_before_json_parsing(self):
        other={'event':'gate_dispatch','dirty_watches':[{'quest_id':'unrelated','watch_id':'other'}]}
        mine={'event':'gate_dispatch','ts':'2026-01-01T00:00:00Z',
          'dirty_watches':[{'quest_id':'quest-test','watch_id':'w1','type':'schedule'}]}
        (self.root/'state/run-log.ndjson').write_text((json.dumps(other)+'\n')*1000+json.dumps(mine)+'\n')
        with patch('dashboard_visualizer.json.loads',wraps=json.loads) as parse:
            data=self.read()
        self.assertEqual(parse.call_count,1)
        self.assertEqual(data['coverage']['triggers'],1)

    def test_bootstrap_receipt_is_not_invented_as_a_watch(self):
        folder=self.root/'state/triage';folder.mkdir()
        (folder/'dispatch-test.json').write_text(json.dumps({'target':'quest-test','run_id':'r1',
          'created_utc':'2026-01-01T00:00:00Z','items':[{'item_id':'sidequestor-bootstrap','type':'bootstrap','status':'handled'}]}))
        event=self.read()['events'][0]
        self.assertEqual(event['item_id'],'sidequestor-bootstrap')
        self.assertEqual(event['watch_id'],'')

    def test_retained_dispatch_receipts_are_additional_evidence(self):
        folder=self.root/'state/triage';folder.mkdir()
        (folder/'dispatch-test.json').write_text(json.dumps({'target':'quest-test','run_id':'r1',
          'created_utc':'2026-01-01T00:00:00Z','items':[{'item_id':'w1','type':'schedule',
          'status':'handled','acked_utc':'2026-01-01T00:01:00Z','note':'Done'}]}))
        data=self.read()
        self.assertEqual(data['coverage']['receipts'],1)
        self.assertEqual(data['events'][0]['watch_id'],'w1')
        self.assertEqual(data['events'][0]['evidence_source'],'dispatch receipt')

    def test_dotted_quest_identifier(self):
        dotted = self.quest.parent / 'quest.test'
        dotted.mkdir()
        result = build_visualization(self.root, 'quest.test')
        self.assertEqual(result['id'], 'quest.test')

    def test_invalid_paths_and_escaped_quest_symlink(self):
        for qid in ('../quest-test', 'a/b', '', '..', 'quest-test/../../'):
            self.assertIsNone(self.read(qid))
        (self.quest.parent / 'outside').symlink_to(self.root, target_is_directory=True)
        self.assertIsNone(self.read('outside'))
        self.assertEqual(self.read()['events'], [])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for visualization model checks')
    def test_few_and_large_histories_and_safe_rendering(self):
        result = subprocess.run(['node', str(ROOT/'tests/dashboard_quest_visualizer.cjs')],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_disabled_feature_returns_no_module_or_history(self):
        # Exercise the handler directly: disabled mode must not read any quest data.
        with patch.dict(os.environ, {'YAAS_WORKSPACE': str(self.root), 'YAAS_RUNTIME_ROOT': str(TRIAGE.parent)}), \
                patch.object(sys, 'argv', ['dashboard-server.py']), \
                patch.object(sys, 'path', [str(TRIAGE), *sys.path]):
            spec = importlib.util.spec_from_file_location('visualizer_server_test', TRIAGE/'ops/dashboard-server.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        from io import BytesIO
        handler = object.__new__(module.Handler)
        handler.path = '/api/quest/quest-test/visualization'
        handler._host_ok = lambda: True
        handler._authed = lambda: True
        handler.wfile = BytesIO()
        responses = []
        handler._send_json = lambda value, status=200, **kw: responses.append((status,value))
        with patch.object(module, '_dotenv', return_value='0'), patch.object(module, 'build_quest_detail') as detail:
            handler.do_GET()
            detail.assert_not_called()
        self.assertEqual(responses[0][0], 404)
        handler.path = '/dashboard-assets/dashboard-quest-visualizer.js'
        headers = []
        handler.send_response = lambda value: responses.append(value)
        handler.send_header = lambda key,value: headers.append((key,value))
        handler.end_headers = lambda: None
        with patch.object(module, '_dotenv', return_value='0'):
            handler.do_GET()
        self.assertEqual(handler.wfile.getvalue(), b'')
        self.assertIn(('Content-Length', '0'), headers)


if __name__ == '__main__':
    unittest.main()
