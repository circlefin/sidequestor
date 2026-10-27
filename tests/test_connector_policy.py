"""Regression cases for workspaces without access to every connector."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TRIAGE = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(TRIAGE))
from tick_state import Config, load_environment


class ConnectorPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.qdir = self.root / 'state/quests/active/quest-test'
        self.qdir.mkdir(parents=True)
        self.watch = self.qdir / 'watch.json'
        self.watch.write_text('{"watches": []}')
        self.envfile = self.root / '.env'
        self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=slack\n')
        self.env = dict(os.environ, SIDEQUESTOR_WORKSPACE=str(self.root))
        for key in list(self.env):
            if key.startswith(('SIDEQUESTOR_CHECKER_', 'YAAS_CHECKER_')):
                self.env.pop(key)

    def run_helper(self, script, *args, ok=True):
        result = subprocess.run([sys.executable, str(TRIAGE / script), *args],
                                env=self.env, text=True, capture_output=True, timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def config(self):
        return Config(TRIAGE, self.env, repo_root=self.root)

    def test_long_running_services_reload_edits_and_deletions(self):
        from sidequestor.native import run_native
        workspace = SimpleNamespace(root=self.root, yaas_dir=self.root / '.yaas', env_file=self.envfile)
        for service in ('triage-loop.sh', 'ops/heartbeat-loop.sh', 'ops/dashboard-server.py'):
            with self.subTest(service=service), patch.dict(os.environ, self.env, clear=True), \
                    patch('sidequestor.native.subprocess.run') as run:
                self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=slack,email\n')
                run_native(workspace, 'yaas-triage/' + service, [])
                inherited = run.call_args.kwargs['env']
                self.assertNotIn('SIDEQUESTOR_CHECKER_CONNECTORS', inherited)
                self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=slack\n')
                self.assertEqual(load_environment(self.root, inherited)['YAAS_CHECKER_CONNECTORS'], 'slack')
                self.envfile.write_text('')
                self.assertNotIn('YAAS_CHECKER_CONNECTORS', load_environment(self.root, inherited))

    def test_explicit_export_still_wins_on_next_tick(self):
        inherited = dict(self.env, YAAS_CHECKER_CONNECTORS='github')
        self.assertEqual(load_environment(self.root, inherited)['SIDEQUESTOR_CHECKER_CONNECTORS'], 'github')

    def test_single_run_helpers_still_receive_dotenv(self):
        from sidequestor.native import run_native
        workspace = SimpleNamespace(root=self.root, yaas_dir=self.root / '.yaas', env_file=self.envfile)
        with patch.dict(os.environ, self.env, clear=True), patch('sidequestor.native.subprocess.run') as run:
            run_native(workspace, 'yaas-triage/tick.py', [])
        self.assertEqual(run.call_args.kwargs['env']['SIDEQUESTOR_CHECKER_CONNECTORS'], 'slack')

    def test_dashboard_controller_does_not_freeze_dotenv(self):
        from sidequestor.dashboard import serve
        workspace = SimpleNamespace(root=self.root, yaas_dir=self.root / '.yaas',
                                    state=self.root / 'state', env_file=self.envfile)
        with patch.dict(os.environ, self.env, clear=True), \
                patch('sidequestor.dashboard.subprocess.Popen', side_effect=RuntimeError('captured')) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'captured'):
                serve(workspace)
        self.assertNotIn('SIDEQUESTOR_CHECKER_CONNECTORS', spawn.call_args.kwargs['env'])

    def test_watch_rejects_disabled_connector_before_writing(self):
        entry = json.dumps({'type': 'email', 'query': 'from:synthetic@example.com', 'reason': 'test'})
        result = self.run_helper('ledger/add-watch.py', 'quest-test', entry, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('connector_disabled:email', result.stderr)
        self.assertIn('SIDEQUESTOR_CHECKER_CONNECTORS', result.stderr)
        self.assertEqual(json.loads(self.watch.read_text())['watches'], [])
        self.run_helper('ledger/add-watch.py', '--allow-dormant', 'quest-test', entry)
        self.assertEqual(json.loads(self.watch.read_text())['watches'][0]['type'], 'email')

    def test_new_quest_rejects_disabled_connector_and_supports_dormant_opt_in(self):
        spec = json.dumps({'title': 'Synthetic email', 'watches': [
            {'type': 'email', 'query': 'from:synthetic@example.com', 'reason': 'test'}]})
        result = self.run_helper('skills/yaas-quest-creation/new-quest.py', spec, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('connector_disabled:email', result.stderr)
        self.assertEqual(len(list(self.qdir.parent.iterdir())), 1)
        self.run_helper('skills/yaas-quest-creation/new-quest.py', spec, '--allow-dormant')
        self.assertEqual(len(list(self.qdir.parent.iterdir())), 2)

    def test_synced_creation_script_uses_packaged_connector_manifests(self):
        copied = self.root / '.yaas/engine/current/skills/yaas-quest-creation/new-quest.py'
        copied.parent.mkdir(parents=True)
        shutil.copy2(TRIAGE / 'skills/yaas-quest-creation/new-quest.py', copied)
        self.env['SIDEQUESTOR_RUNTIME_ROOT'] = str(TRIAGE.parent)
        spec = json.dumps({'title': 'Synthetic email', 'watches': [
            {'type': 'email', 'query': 'from:synthetic@example.com', 'reason': 'test'}]})
        result = self.run_helper(copied, spec, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('connector_disabled:email', result.stderr)

    def test_invalid_connector_setting_fails_cleanly_without_writes(self):
        self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=slack,gmail\n')
        watch = {'type': 'email', 'query': 'from:synthetic@example.com', 'reason': 'test'}
        for script, args in (
            ('ledger/add-watch.py', ['quest-test', json.dumps(watch)]),
            ('skills/yaas-quest-creation/new-quest.py', [json.dumps({'title': 'Test', 'watches': [watch]})]),
        ):
            result = self.run_helper(script, *args, ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('gmail', result.stderr)
        self.assertEqual(json.loads(self.watch.read_text())['watches'], [])

    def test_existing_dormant_watch_can_be_refreshed_without_creating_another(self):
        self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=email\n')
        entry = {'type': 'slack_thread', 'channel_id': 'C123', 'thread_ts': '1000.000001', 'reason': 'test'}
        self.run_helper('ledger/add-watch.py', '--allow-dormant', 'quest-test', json.dumps(entry))
        before = json.loads(self.watch.read_text())['watches'][0]
        entry['refresh_activity'] = True
        self.run_helper('ledger/add-watch.py', 'quest-test', json.dumps(entry))
        after = json.loads(self.watch.read_text())['watches']
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['last_checked_ts'], before['last_checked_ts'])
        self.assertIn('last_activity_ts', after[0])

    def install_health(self):
        watches = [
            {'watch_id': 'watch-live', 'type': 'schedule', 'next_fire_ts': '9999999999'},
            {'watch_id': 'watch-disabled', 'type': 'email', 'query': 'from:synthetic@example.com'}]
        self.watch.write_text(json.dumps({'watches': watches}))
        path = self.root / 'state/triage/checker-health.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({wid: {'consecutive_errors': 6, 'last_error': 'synthetic'}
                                   for wid in ('watch-live', 'watch-disabled', 'watch-retired')}))
        return path

    def test_prune_and_monitor_ignore_retired_and_disabled_watches(self):
        path = self.install_health()
        original = self.watch.read_text()
        spec = importlib.util.spec_from_file_location('policy_health_monitor', TRIAGE / 'ops/health-monitor.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, self.env, clear=True):
            health = module.Health(self.root)
            health.check_checker_health()
        self.assertEqual([p['signature'] for p in health.problems], ['watch-live'])
        self.run_helper('ledger/checker-health.py', 'prune-inactive')
        self.assertEqual(set(json.loads(path.read_text())), {'watch-live'})
        self.assertEqual(self.watch.read_text(), original)

    def test_unreadable_watch_does_not_erase_health_evidence(self):
        path = self.install_health()
        original = path.read_text()
        self.watch.write_text('{bad')
        self.assertIsNone(self.config().active_checker_ids())
        self.run_helper('ledger/checker-health.py', 'prune-inactive')
        self.assertEqual(path.read_text(), original)

    def test_bad_entry_does_not_revive_disabled_slack_health(self):
        path = self.install_health()
        self.envfile.write_text('SIDEQUESTOR_CHECKER_CONNECTORS=slack\nSIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n')
        data = json.loads(self.watch.read_text())
        data['watches'].extend([{'watch_id': 'watch-slack', 'type': 'slack_thread'}, {'type': 'email'}])
        self.watch.write_text(json.dumps(data))
        health = json.loads(path.read_text())
        health['watch-slack'] = {'consecutive_errors': 6}
        path.write_text(json.dumps(health))
        self.assertEqual(self.config().active_checker_ids(), {'watch-live'})
        self.run_helper('ledger/checker-health.py', 'prune-inactive')
        self.assertEqual(set(json.loads(path.read_text())), {'watch-live'})

    def test_workspace_opener_reads_ide_from_dotenv(self):
        spec = importlib.util.spec_from_file_location('policy_dashboard', TRIAGE / 'ops/dashboard-server.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, 'argv', ['dashboard-server.py']):
            spec.loader.exec_module(module)
            handler = object.__new__(module.Handler)
            handler._send_json = lambda *args: None
            self.envfile.write_text('YAAS_IDE_APP=Visual Studio Code\n')
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                handler._handle_open_workspace()
            self.assertEqual(run.call_args.args[0][2], 'Visual Studio Code')

    def test_retire_clears_its_health_record(self):
        path = self.install_health()
        self.run_helper('ledger/add-watch.py', 'retire', 'quest-test', 'watch-live', 'obsolete')
        self.assertNotIn('watch-live', json.loads(path.read_text()))


if __name__ == '__main__':
    unittest.main()
