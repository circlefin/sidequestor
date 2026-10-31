"""Claude Slack fallback must never replace an existing custom app identity."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from sidequestor.setup import provision_env
from sidequestor.workspace import init_workspace

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / 'src/sidequestor/runtime/yaas-triage'
CONFIG = json.loads((TRIAGE / 'setup/yaas-app-config.json').read_text())
CLAUDE = CONFIG['claude_app']


class ClaudeSlackDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {'YAAS_CONFIG_HOME': str(self.root / 'config')})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workspace = init_workspace(self.root / 'workspace')

    def configure(self, agent='claude', app='', client='', enabled='1'):
        self.workspace.env_file.write_text(
            f'SIDEQUESTOR_AGENT={agent}\n'
            'SIDEQUESTOR_OPEN_IN=claude\n'
            f'SIDEQUESTOR_SLACK_CHECKERS_ENABLED={enabled}\n'
            'SIDEQUESTOR_CHECKER_CONNECTORS=slack\n'
            f'SLACK_APP_ID={app}\nSLACK_CLIENT_ID={client}\n'
            'SLACK_WORKSPACE_NAME=Test\nSLACK_WORKSPACE_DOMAIN=test\n')

    def provision(self):
        with redirect_stdout(io.StringIO()):
            return provision_env(self.workspace, interactive=False)

    def provision_with_answers(self, answers):
        with self.workspace.env_file.open('a') as env:
            env.write('SIDEQUESTOR_CLAUDE_MODEL=test-model\n'
                      'SIDEQUESTOR_CLAUDE_EFFORT=high\n'
                      'SIDEQUESTOR_CLAUDE_PERMISSION_MODE=acceptEdits\n')
        prompts = []
        answers = iter(answers)
        def answer(prompt):
            prompts.append(prompt)
            return next(answers)
        with redirect_stdout(io.StringIO()):
            provision_env(self.workspace, interactive=True, input_fn=answer)
        return prompts

    def test_claude_fills_both_blank_ids_and_is_idempotent(self):
        self.configure()
        prompts = self.provision_with_answers(['', ''])
        self.assertEqual(prompts, [f"  Slack App ID (Enter for Claude: {CLAUDE['app_id']}): ",
                                   f"  Slack Client ID (Enter for Claude: {CLAUDE['client_id']}): "])
        content = self.workspace.env_file.read_text()
        self.assertIn('SLACK_APP_ID=' + CLAUDE['app_id'], content)
        self.assertIn('SLACK_CLIENT_ID=' + CLAUDE['client_id'], content)
        self.assertIn('SIDEQUESTOR_SLACK_CHECKERS_ENABLED=1', content)
        self.assertEqual(self.provision(), ({}, False))
        self.assertEqual(self.workspace.env_file.read_text(), content)

    def test_fresh_init_wizard_collects_and_saves_every_slack_setting(self):
        # Start from init's untouched configuration, as a new user would.
        answers = iter(['yes', 'claude', '', '', 'acceptEdits', '', '', 'Circle', 'circle', 'claude'])
        prompts = []
        def answer(prompt):
            prompts.append(prompt)
            return next(answers)
        output = io.StringIO()
        with redirect_stdout(output):
            provision_env(self.workspace, interactive=True, input_fn=answer)
        content = self.workspace.env_file.read_text()
        self.assertIn('SLACK_APP_ID=' + CLAUDE['app_id'], content)
        self.assertIn('SLACK_CLIENT_ID=' + CLAUDE['client_id'], content)
        self.assertIn('SLACK_WORKSPACE_NAME=Circle', content)
        self.assertIn('SLACK_WORKSPACE_DOMAIN=circle', content)
        self.assertIn('SIDEQUESTOR_OPEN_IN=claude', content)
        self.assertTrue(any('Dashboard app' in p for p in prompts))
        self.assertTrue(any('Slack workspace name' in p for p in prompts))
        self.assertTrue(any('before .slack.com' in p for p in prompts))
        text = output.getvalue()
        self.assertIn('no configuration-file editing is needed', text)
        self.assertIn('press Enter at BOTH', text)
        command = next(line.strip() for line in text.splitlines() if line.strip().startswith('sq --workspace '))
        self.assertEqual(shlex.split(command), ['sq', '--workspace', str(self.workspace.root),
                                                'setup', '--manifest'])
        self.assertIn('Basic Information', text)
        self.assertLess(text.index('Recommended: a workspace-owned Sidequestor app'),
                        text.index("Alternative: Claude's app for core functionality"))
        self.assertIn('sufficient for basic checks', text)
        self.assertNotIn('\033[', text)

    def test_explicit_custom_pair_is_preserved_for_every_backend(self):
        for agent in ('claude', 'codex', 'cursor'):
            with self.subTest(agent=agent):
                self.configure(agent, 'ACUSTOM', '123.456')
                self.provision()
                content = self.workspace.env_file.read_text()
                self.assertIn('SLACK_APP_ID=ACUSTOM\n', content)
                self.assertIn('SLACK_CLIENT_ID=123.456\n', content)
                self.assertNotIn(CLAUDE['client_id'], content)

    def test_interactive_setup_skips_all_saved_slack_fields(self):
        self.configure(app='ACUSTOM', client='123.456')
        self.assertEqual(self.provision_with_answers([]), [])
        self.assertIn('SLACK_APP_ID=ACUSTOM', self.workspace.env_file.read_text())

    def test_interactive_setup_asks_only_for_missing_slack_fields(self):
        self.configure(app='ACUSTOM', client='')
        self.assertEqual(self.provision_with_answers(['123.456']), ['  Slack Client ID: '])
        self.assertIn('SLACK_APP_ID=ACUSTOM', self.workspace.env_file.read_text())
        self.assertIn('SLACK_CLIENT_ID=123.456', self.workspace.env_file.read_text())

    def test_partial_identity_never_receives_claude_defaults(self):
        for app, client, missing in (('ACUSTOM', '', 'SLACK_CLIENT_ID'),
                                     ('', '123.456', 'SLACK_APP_ID')):
            with self.subTest(app=app, client=client):
                self.configure(app=app, client=client)
                before = self.workspace.env_file.read_text()
                with self.assertRaisesRegex(SystemExit, missing):
                    self.provision()
                self.assertEqual(self.workspace.env_file.read_text(), before)

    def test_other_backends_still_require_explicit_ids(self):
        for agent in ('codex', 'cursor'):
            with self.subTest(agent=agent):
                self.configure(agent)
                with self.assertRaisesRegex(SystemExit, 'SLACK_APP_ID'):
                    self.provision()

    def test_disabled_slack_does_not_fill_ids(self):
        self.configure(enabled='0')
        self.provision()
        self.assertNotIn(CLAUDE['client_id'], self.workspace.env_file.read_text())

    def shell_preflight(self):
        # Stop at the OAuth prompt (EOF), before generating credentials or calling
        # security/open/curl. Stubs make accidental external calls fail locally.
        if not shutil.which('jq'):
            self.skipTest('jq unavailable')
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        for name in ('security', 'open', 'curl', 'openssl'):
            path = bindir / name
            path.write_text('#!/bin/sh\necho unexpected-external-call >&2\nexit 91\n')
            path.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SLACK_', 'SIDEQUESTOR_', 'YAAS_'))}
        env.update(PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   HOME=str(self.root), YAAS_CONFIG_HOME=str(self.root / 'config'))
        return subprocess.run(['bash', str(TRIAGE / 'setup/setup.sh'), '--workspace',
                               str(self.workspace.root), '--oauth-only'],
                              input='', text=True, capture_output=True, env=env, timeout=20)

    def test_shell_does_not_infer_intent_from_blank_identifiers(self):
        self.configure()
        before = self.workspace.env_file.read_text()
        result = self.shell_preflight()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Slack app not configured yet', result.stderr)
        self.assertNotIn('Using Claude', result.stdout)
        self.assertEqual(self.workspace.env_file.read_text(), before)

    def test_noninteractive_claude_requires_configured_ids(self):
        self.configure()
        before = self.workspace.env_file.read_text()
        with self.assertRaisesRegex(SystemExit, 'SLACK_APP_ID'):
            self.provision()
        self.assertEqual(self.workspace.env_file.read_text(), before)

    def test_wizard_accepts_custom_identifiers_in_both_prompts(self):
        self.configure()
        prompts = self.provision_with_answers(['ACUSTOM', '123.456'])
        self.assertEqual(prompts, [f"  Slack App ID (Enter for Claude: {CLAUDE['app_id']}): ",
                                   f"  Slack Client ID (Enter for Claude: {CLAUDE['client_id']}): "])
        content = self.workspace.env_file.read_text()
        self.assertIn('SLACK_APP_ID=ACUSTOM\n', content)
        self.assertIn('SLACK_CLIENT_ID=123.456\n', content)
        self.assertNotIn(CLAUDE['client_id'], content)

    def test_wizard_rejects_only_one_blank_answer_without_writing_defaults(self):
        for answers, missing in ((['', '123.456'], 'SLACK_APP_ID'),
                                  (['ACUSTOM', ''], 'SLACK_CLIENT_ID')):
            with self.subTest(answers=answers):
                self.configure()
                with self.assertRaisesRegex(SystemExit, missing):
                    self.provision_with_answers(answers)
                self.assertNotIn(CLAUDE['client_id'], self.workspace.env_file.read_text())

    def test_shell_custom_apps_keep_all_36_scopes(self):
        self.configure(app='ACUSTOM', client='123.456')
        result = self.shell_preflight()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('(36 total)', result.stdout)
        self.assertIn('ACUSTOM/app-assistant', result.stdout)
        self.assertNotIn('unexpected-external-call', result.stderr)

    def test_shell_other_backend_and_partial_custom_identity_require_ids(self):
        for agent, app, client in (('codex', '', ''), ('claude', 'ACUSTOM', ''),
                                   ('claude', '', '123.456')):
            with self.subTest(agent=agent, app=app, client=client):
                self.configure(agent, app, client)
                result = self.shell_preflight()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Slack app not configured yet', result.stderr)
                self.assertNotIn('Using Claude', result.stdout)

    def test_explicit_claude_identity_keeps_reduced_scopes_after_backend_switch(self):
        self.configure('codex', CLAUDE['app_id'], CLAUDE['client_id'])
        result = self.shell_preflight()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('(29 total)', result.stdout)

    def test_mixed_claude_and_custom_ids_are_rejected_before_oauth(self):
        self.configure(app='ACUSTOM', client=CLAUDE['client_id'])
        result = self.shell_preflight()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('requires SLACK_APP_ID=' + CLAUDE['app_id'], result.stderr)
        self.assertNotIn('unexpected-external-call', result.stderr)

    def test_manifest_still_has_full_custom_app_scopes(self):
        result = subprocess.run(['bash', str(TRIAGE / 'setup/setup.sh'), '--manifest'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for scope in CONFIG['user_scopes']:
            self.assertIn('      - ' + scope + '\n', result.stdout)
        self.assertNotIn(CLAUDE['client_id'], result.stdout)

    def test_profile_missing_scope_retains_exit_code_and_explains_optional_feature(self):
        spec = importlib.util.spec_from_file_location('claude_scope_client', TRIAGE / 'surfaces/client.py')
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        body = json.dumps({'ok': False, 'error': 'missing_scope',
                           'needed': 'users.profile:write', 'provided': 'chat:write'})
        output = io.StringIO()
        with patch.object(client, 'slack_request', return_value=(200, body)) as request, \
                redirect_stderr(output), redirect_stdout(output):
            result = client.cmd_slack_profile(['set', 'Busy', 'hourglass'])
        self.assertEqual(result, client.classify_body(body))
        self.assertEqual(request.call_count, 1)
        self.assertIn('optional', output.getvalue())
        self.assertIn('needed=users.profile:write', output.getvalue())
