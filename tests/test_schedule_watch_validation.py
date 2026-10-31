"""Reject schedules the checker cannot run, before either creator writes state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TRIAGE = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'


class ScheduleWatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.active = self.root / 'state/quests/active'
        self.quest = self.active / 'quest-existing'
        self.quest.mkdir(parents=True)
        self.watch = self.quest / 'watch.json'
        self.watch.write_text('{"watches": []}\n')
        (self.quest / 'meta.json').write_text('{"id":"quest-existing","status":"active"}')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('SIDEQUESTOR_', 'YAAS_'))}
        self.env.update(SIDEQUESTOR_WORKSPACE=str(self.root),
                        YAAS_CONFIG_HOME=str(self.root / 'config'))

    def call(self, path, *args):
        return subprocess.run([sys.executable, str(TRIAGE / path), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def add(self, entry):
        return self.call('ledger/add-watch.py', 'quest-existing', json.dumps(entry))

    def create(self, entry):
        return self.call('skills/yaas-quest-creation/new-quest.py',
                         json.dumps({'title': 'Synthetic schedule', 'watches': [entry]}))

    def test_invalid_timestamps_fail_without_writing_either_creation_path(self):
        invalid = ['2026-10-31T01:20:00+08:00', '', '   ', None, True, False,
                   'NaN', 'Infinity', '-Infinity', float('nan'), float('inf'),
                   0, -1, [], {}, 10 ** 400]
        for creator in (self.add, self.create):
            for value in invalid:
                with self.subTest(creator=creator.__name__, value=str(value)):
                    before = {p.relative_to(self.root): p.read_bytes()
                              for p in self.root.rglob('*') if p.is_file()}
                    result = creator({'type': 'schedule', 'next_fire_ts': value, 'reason': 'test'})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('next_fire_ts', result.stderr)
                    after = {p.relative_to(self.root): p.read_bytes()
                             for p in self.root.rglob('*') if p.is_file()}
                    self.assertEqual(after, before)

    def test_numeric_forms_and_fractional_seconds_still_run(self):
        for creator in (self.add, self.create):
            for value in (1800000060, 1800000060.25, '1800000060', '1800000060.25'):
                with self.subTest(creator=creator.__name__, value=value):
                    entry = {'type': 'schedule', 'next_fire_ts': value, 'reason': 'test'}
                    before = set(self.active.iterdir())
                    result = creator(entry)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if creator == self.add:
                        path = self.watch
                    else:
                        created = set(self.active.iterdir()) - before
                        self.assertEqual(len(created), 1)
                        path = created.pop() / 'watch.json'
                    stored = json.loads(path.read_text())['watches'][-1]
                    self.assertEqual(stored['next_fire_ts'], value)
                    stored['last_checked_ts'] = '0'
                    checked = self.call('checkers/schedule.py', json.dumps(stored))
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    outcome = json.loads(checked.stdout)
                    self.assertIn(outcome['outcome'], {'clean', 'dirty'})
                    self.assertIn(outcome['count'], (0, 1))

    def test_recurring_schedules_remain_valid(self):
        for creator in (self.add, self.create):
            with self.subTest(creator=creator.__name__):
                result = creator({'type': 'schedule', 'cron': '0 9 * * *',
                                  'tz': 'UTC', 'reason': 'test'})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_supplied_invalid_timestamp_is_rejected_even_with_cron(self):
        for creator in (self.add, self.create):
            for value in ('2026-10-31T01:20:00+08:00', None, ''):
                with self.subTest(creator=creator.__name__, value=value):
                    result = creator({'type': 'schedule', 'cron': '0 9 * * *',
                                      'tz': 'UTC', 'next_fire_ts': value, 'reason': 'test'})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('next_fire_ts', result.stderr)
