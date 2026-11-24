"""OAuth must own the callback port before opening Slack, and release it on exit."""
import http.client
import os
from pathlib import Path
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

TRIAGE = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
SCRIPT = TRIAGE / 'setup/setup.sh'


class SlackOAuthListenerTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('jq'):
            self.skipTest('jq unavailable')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / 'browser-opened'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('security', 'curl'):
            self.stub(name, 'exit 91')
        self.stub('open', 'touch "$OPEN_MARKER"\nexit 9')
        (self.root / '.env').write_text(
            'SIDEQUESTOR_AGENT=claude\nSIDEQUESTOR_SLACK_CHECKERS_ENABLED=1\n'
            'SIDEQUESTOR_CHECKER_CONNECTORS=slack\n'
            'SLACK_APP_ID=A08SF47R6P4\nSLACK_CLIENT_ID=1601185624273.8899143856786\n'
            'SLACK_WORKSPACE_NAME=Test\nSLACK_WORKSPACE_DOMAIN=test\n')
        shutil.copy2(TRIAGE / 'setup/yaas-app-config.json', self.root / 'yaas-app-config.json')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('SIDEQUESTOR_', 'YAAS_', 'SLACK_'))}
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        HOME=str(self.root), YAAS_CONFIG_HOME=str(self.root / 'config'),
                        OPEN_MARKER=str(self.marker))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def socket(self):
        sock = socket.socket()
        self.addCleanup(sock.close)
        try:
            sock.bind(('127.0.0.1', 0))
        except PermissionError:
            self.skipTest('localhost binding is forbidden')
        return sock

    def command(self, port):
        script = self.root / 'setup-probe.sh'
        text = SCRIPT.read_text().replace('PORT=3118', f'PORT={port}', 1)
        text = text.replace('TRIAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"',
                            'TRIAGE_DIR=' + shlex.quote(str(TRIAGE)), 1)
        script.write_text(text)
        return ['bash', str(script), '--workspace', str(self.root), '--oauth-only']

    def test_occupied_port_fails_before_opening_browser(self):
        sock = self.socket()
        sock.listen()
        result = subprocess.run(self.command(sock.getsockname()[1]), input='\n',
                                capture_output=True, text=True, env=self.env, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('callback listener could not start', result.stderr)
        self.assertIn('port', result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertNotIn('https://slack.com/oauth/v2/authorize?', result.stdout)

    def test_browser_launch_failure_releases_callback_listener(self):
        sock = self.socket()
        port = sock.getsockname()[1]
        sock.close()
        result = subprocess.run(self.command(port), input='\n', capture_output=True,
                                text=True, env=self.env, timeout=15)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertTrue(self.marker.exists())
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))

    def cancel_setup(self, signum, expected_code):
        sock = self.socket()
        port = sock.getsockname()[1]
        sock.close()
        self.stub('open', 'touch "$OPEN_MARKER"\nexit 0')
        process = subprocess.Popen(self.command(port), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=self.env)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        process.stdin.write('\n')
        process.stdin.flush()
        deadline = time.monotonic() + 8
        while not self.marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.marker.exists())
        process.send_signal(signum)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, expected_code)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))

    def test_termination_releases_callback_listener(self):
        self.cancel_setup(signal.SIGTERM, 143)

    def test_ctrl_c_releases_callback_listener(self):
        self.cancel_setup(signal.SIGINT, 130)

    def callback(self, state):
        sock = self.socket()
        port = sock.getsockname()[1]
        sock.close()
        log = self.root / 'callback-result'
        source = SCRIPT.read_text().split('<<\'PY\' &\n', 1)[1].split('\nPY\n', 1)[0]
        process = subprocess.Popen([sys.executable, '-c', source, str(port), str(log), 'expected-state'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not log.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(log.read_text(), 'READY\n')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        connection.request('GET', '/callback?' + urllib.parse.urlencode({'state': state, 'code': 'synthetic-code'}))
        response = connection.getresponse()
        body, content_type, status = response.read().decode('utf-8'), response.getheader('Content-Type'), response.status
        connection.close()
        process.communicate(timeout=5)
        return log.read_text(), body, content_type, status

    def test_state_mismatch_stays_rejected_and_error_page_is_utf8(self):
        result, body, content_type, status = self.callback('old-attempt-state')
        self.assertEqual(status, 400)
        self.assertEqual(result, 'ERROR state mismatch\n')
        self.assertIn('charset=utf-8', content_type)
        self.assertIn('<meta charset="utf-8">', body)
        self.assertIn('State mismatch', body)
        self.assertNotIn('synthetic-code', result)

    def test_matching_callback_does_not_claim_token_was_already_saved(self):
        result, body, content_type, status = self.callback('expected-state')
        self.assertEqual(status, 200)
        self.assertEqual(result, 'CODE synthetic-code\n')
        self.assertIn('charset=utf-8', content_type)
        self.assertIn('finish exchanging and saving', body)
        self.assertNotIn('has been issued and stored', body)
