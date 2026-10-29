import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / 'src/sidequestor/runtime/yaas-triage'
TEMPLATE = ROOT / 'src/sidequestor/package_data/env.example'
sys.path.insert(0, str(TRIAGE))
import dashboard_settings as settings
from tick_state import NUMERIC_KNOBS
sys.path.remove(str(TRIAGE))


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def read(self, text=None, inherited=None):
        if text is not None:
            (self.workspace / '.env').write_text(text)
        return settings.build_settings(self.workspace, TEMPLATE, inherited or {})

    def test_defaults_come_from_runtime_not_setup_examples(self):
        result = self.read()
        self.assertEqual(result['overrides'], [])
        rows = {row['key']: row for row in result['defaults']}
        for key, value in NUMERIC_KNOBS.items():
            self.assertEqual(rows[settings.canonical(key)]['value'], str(value))
        self.assertEqual(rows['SIDEQUESTOR_CLAUDE_MODEL']['default'], 'opus')
        self.assertEqual(rows['SIDEQUESTOR_CLAUDE_PERMISSION_MODE']['default'], 'acceptEdits')
        self.assertEqual(rows['SIDEQUESTOR_CODEX_PERMISSION_MODE']['default'], 'workspace-write')
        self.assertEqual(rows['SIDEQUESTOR_CODEX_MODEL']['default'], 'Not set')
        self.assertEqual(rows['SIDEQUESTOR_OPEN_IN']['value'], 'codex')
        self.assertGreater(len(rows), 60)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_cursor_permission_display_matches_runtime_fallback(self):
        dispatcher = (TRIAGE / 'dispatch/dispatch-agent.sh').read_text()
        fallback = re.search(r'YAAS_CURSOR_PERMISSION_MODE:-([^}]+)', dispatcher).group(1)
        rows = {row['key']: row for row in self.read()['defaults']}
        self.assertEqual(rows['SIDEQUESTOR_CURSOR_PERMISSION_MODE']['default'], fallback)
        self.assertEqual(rows['SIDEQUESTOR_CURSOR_PERMISSION_MODE']['value'], fallback)
        configured = self.read('SIDEQUESTOR_CURSOR_PERMISSION_MODE=bypassPermissions\n')['overrides'][0]
        self.assertEqual(configured['value'], 'bypassPermissions')
        self.assertEqual(configured['default'], fallback)

    def test_dotenv_entries_come_first_even_when_equal_to_default_or_empty(self):
        result = self.read('export SIDEQUESTOR_OPEN_IN="cursor"\nSIDEQUESTOR_AGENT=codex\nSIDEQUESTOR_CURSOR_MODEL=\n')
        self.assertEqual([row['key'] for row in result['overrides']],
                         ['SIDEQUESTOR_AGENT', 'SIDEQUESTOR_CURSOR_MODEL', 'SIDEQUESTOR_OPEN_IN'])
        self.assertEqual(result['overrides'][-1]['value'], 'cursor')
        self.assertFalse(result['overrides'][1]['using_default'])
        self.assertEqual(result['overrides'][1]['value'], 'Empty')
        self.assertNotIn('SIDEQUESTOR_AGENT', {row['key'] for row in result['defaults']})

    def test_real_environment_beats_dotenv_and_aliases_are_honored(self):
        result = self.read('YAAS_WORKER_TIMEOUT=12\nSIDEQUESTOR_WORKER_TIMEOUT=34\n',
                           {'YAAS_WORKER_TIMEOUT': '56'})
        row = result['overrides'][0]
        self.assertEqual((row['key'], row['value'], row['source'], row['dotenv_value']),
                         ('SIDEQUESTOR_WORKER_TIMEOUT', '56', 'environment', '34'))
        result = self.read('YAAS_WORKER_TIMEOUT=12\nYAAS_WORKER_TIMEOUT=99\n')
        self.assertEqual(result['overrides'][0]['value'], '12')
        self.assertEqual(result['overrides'][0]['dotenv_key'], 'YAAS_WORKER_TIMEOUT')

    def test_secrets_custom_values_and_credential_urls_never_reach_browser(self):
        result = self.read('CODA_API_KEY=secret-coda\nNOTION_TOKEN=secret-notion\n'
                           'CUSTOM_VALUE=secret-custom\nSIDEQUESTOR_FAKE_SECRET=secret-fake\n'
                           'JIRA_BASE_URL=https://user:secret-url@example.com\n',
                           {'AWS_SECRET_ACCESS_KEY': 'secret-inherited', 'CODA_API_KEY': 'secret-process'})
        body = json.dumps(result)
        for secret in ['secret-coda', 'secret-notion', 'secret-custom', 'secret-fake',
                       'secret-url', 'secret-inherited', 'secret-process']:
            self.assertNotIn(secret, body)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', body)
        self.assertIn('Set (hidden)', body)
        self.assertEqual(len(result['overrides']), 5)

    def test_explicit_empty_values_are_never_presented_as_enabled_defaults(self):
        result = self.read('SIDEQUESTOR_CHECKER_CONNECTORS=\nSIDEQUESTOR_GDOC_COMMENTS_ENABLED=\n'
                           'SIDEQUESTOR_REACTION_DONE_EMOJI=\nSIDEQUESTOR_MAX_SPEND_1H=\n')
        for row in result['overrides']:
            self.assertEqual(row['value'], 'Empty')
            self.assertEqual(row['dotenv_value'], 'Empty')
            self.assertFalse(row['using_default'])
        rows = {row['key']: row for row in result['overrides']}
        self.assertIn('disables all external checkers', rows['SIDEQUESTOR_CHECKER_CONNECTORS']['description'])
        self.assertIn('empty value is invalid', rows['SIDEQUESTOR_GDOC_COMMENTS_ENABLED']['description'])

    def test_adjacent_descriptions_do_not_bleed_and_invalid_launch_is_explained(self):
        result = self.read('SIDEQUESTOR_OPEN_IN=iterm\n')
        rows = {row['key']: row for row in result['defaults']}
        self.assertNotIn('Dashboard launch target', rows['SIDEQUESTOR_IDE_APP']['description'])
        self.assertNotIn('workspace name link', rows['SIDEQUESTOR_TERMINAL_APP']['description'])
        self.assertIn('falls back to codex', result['overrides'][0]['description'])

    def test_urls_with_credentials_or_parameters_are_redacted(self):
        for value in ('https://user:password@example.com',
                      'https://example.com?token=do-not-expose',
                      'https://example.com?access_token=do-not-expose',
                      'https://example.com?auth=do-not-expose',
                      'https://example.com#access_token=do-not-expose'):
            with self.subTest(value=value):
                result = self.read(f'JIRA_BASE_URL={value}\n')
                row = result['overrides'][0]
                self.assertFalse(row['hidden'])
                self.assertEqual(row['value'], 'Set (hidden: URL credentials or parameters)')
                self.assertEqual(row['dotenv_value'], 'Set (hidden: URL credentials or parameters)')
                self.assertNotIn('do-not-expose', json.dumps(result))

    def test_plain_base_urls_remain_visible(self):
        result = self.read('JIRA_BASE_URL=https://example.com\n')
        row = result['overrides'][0]
        self.assertFalse(row['hidden'])
        self.assertEqual(row['value'], 'https://example.com')
        self.assertEqual(row['dotenv_value'], 'https://example.com')

    def test_binary_dotenv_returns_safe_in_band_error(self):
        (self.workspace / '.env').write_bytes(b'\xff\xfe')
        result = self.read()
        self.assertIn('Could not read .env', result['error'])
        self.assertEqual(result['overrides'], [])

    def test_catalog_drops_banners_and_reuses_only_shared_descriptions(self):
        template = self.workspace / 'env.example'
        template.write_text('# ═════════\n# SECTION TITLE\n# ═════════\n'
                            '# Shared description.\nFIRST=\nSECOND=\n'
                            '# Third description.\nTHIRD=\n\nFOURTH=\n')
        catalog = settings._catalog(template)
        self.assertEqual(catalog['FIRST'], 'Shared description.')
        self.assertEqual(catalog['SECOND'], 'Shared description.')
        self.assertEqual(catalog['THIRD'], 'Third description.')
        self.assertEqual(catalog['FOURTH'], 'Optional workspace setting.')

    def test_adaptive_emoji_defaults_and_legacy_permission_fallback(self):
        result = self.read('SIDEQUESTOR_REACTION_LOADING_EMOJI=clock3\n'
                           'YAAS_WORKER_PERMISSION_MODE=bypassPermissions\n')
        rows = {row['key']: row for row in result['defaults']}
        self.assertEqual(rows['SIDEQUESTOR_REACTION_BLOCKED_EMOJI']['value'], 'clock1')
        self.assertEqual(rows['SIDEQUESTOR_CLAUDE_PERMISSION_MODE']['value'], 'bypassPermissions')
        self.assertEqual(rows['SIDEQUESTOR_CLAUDE_PERMISSION_MODE']['source'], 'dotenv')

    def test_unreadable_file_shows_safe_error(self):
        with patch.object(settings, '_dotenv_entries', side_effect=PermissionError('sensitive detail')):
            result = self.read()
        self.assertIn('Could not read .env', result['error'])
        self.assertNotIn('sensitive detail', json.dumps(result))

    def test_dotenv_is_never_executed(self):
        sentinel = self.workspace / 'should-not-exist'
        result = self.read(f'SIDEQUESTOR_OPEN_IN=$(touch {sentinel})\n')
        self.assertFalse(sentinel.exists())
        self.assertIn('$(touch', result['overrides'][0]['value'])

    def test_settings_endpoint_requires_cookie_and_does_not_accept_browser_path(self):
        sys.path.insert(0, str(TRIAGE))
        try:
            spec = importlib.util.spec_from_file_location('settings_server_test', TRIAGE / 'ops/dashboard-server.py')
            server = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {'SIDEQUESTOR_WORKSPACE': str(self.workspace), 'YAAS_WORKSPACE': str(self.workspace),
                                        'YAAS_RUNTIME_ROOT': str(TRIAGE.parent)}), \
                    patch.object(sys, 'argv', ['dashboard-server.py']):
                spec.loader.exec_module(server)
            handler = object.__new__(server.Handler)
            handler.path = '/api/settings?path=/different/workspace'
            handler._host_ok = lambda: True
            handler._authed = lambda: False
            errors, replies, cache_options = [], [], []
            handler.send_error = lambda *args: errors.append(args)
            handler._send_json = lambda *args, **kwargs: (replies.append(args), cache_options.append(kwargs))
            with patch.object(server, 'build_settings', return_value={'safe': True}) as build:
                handler.do_GET()
                self.assertEqual(errors[0][0], 403)
                build.assert_not_called()
                handler._authed = lambda: True
                handler.do_GET()
                self.assertEqual(replies[-1][0], {'safe': True})
                self.assertEqual(build.call_args.args[0], self.workspace.resolve())
                self.assertEqual(cache_options[-1]['cache_control'], 'no-store')
            with patch.object(server, 'build_settings', side_effect=FileNotFoundError('private-path')):
                handler.do_GET()
            self.assertEqual(replies[-1][1], 500)
            self.assertNotIn('private-path', json.dumps(replies[-1]))
            self.assertEqual(cache_options[-1]['cache_control'], 'no-store')
        finally:
            sys.path.remove(str(TRIAGE))
