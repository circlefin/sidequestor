"""Native trust stays scoped to Jira, Notion and X; Slack keeps its transport."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

SURFACES = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage/surfaces'


def load(name):
    sys.path.insert(0, str(SURFACES))
    try:
        spec = importlib.util.spec_from_file_location('tls_test_' + name.replace('-', '_'), SURFACES / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class NativeTrustTest(unittest.TestCase):
    def assert_verified(self, context):
        import truststore
        self.assertIsInstance(context, truststore.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def response(self, value):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return response

    def test_x_auth_api_and_upload_use_native_trust(self):
        credentials = load('x_credentials')
        api = load('x-call')
        send = load('x-send')
        with mock.patch.object(urllib.request, 'urlopen', return_value=self.response({'data': {'id': 'safe'}})) as transport:
            credentials._request_json(urllib.request.Request(credentials.ME_URL))
            self.assert_verified(transport.call_args.kwargs['context'])
            self.assertEqual(transport.call_args.kwargs['timeout'], credentials.HTTP_TIMEOUT_SECONDS)
            with mock.patch.object(api, 'get_access_token', return_value='dummy'):
                api.call('GET', '/2/users/me')
            self.assert_verified(transport.call_args.kwargs['context'])
            self.assertEqual(transport.call_args.kwargs['timeout'], 30)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'image.png'
                path.write_bytes(b'test')
                with mock.patch.object(send, 'get_access_token', return_value='dummy'):
                    send._upload_media({'file': str(path)}, 'unused')
            self.assert_verified(transport.call_args.kwargs['context'])
            self.assertEqual(transport.call_args.kwargs['timeout'], 120)

    def test_notion_keeps_redirect_refusal_and_verified_https_handler(self):
        notion = load('notion-call')
        build = urllib.request.build_opener
        captured = []
        def opener(*handlers):
            result = build(*handlers)
            captured.append(result)
            result.open = mock.Mock()
            result.open.return_value.__enter__ = mock.Mock(return_value=io.StringIO('{"object":"page"}'))
            result.open.return_value.__exit__ = mock.Mock(return_value=False)
            return result
        with mock.patch.object(notion, 'token', return_value='dummy'), mock.patch.object(urllib.request, 'build_opener', side_effect=opener):
            notion.call('GET', '/v1/pages/00000000-0000-0000-0000-000000000000')
        https = next(h for h in captured[0].handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assert_verified(https._context)
        redirect = next(h for h in captured[0].handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
        self.assertIsInstance(redirect, notion.NoRedirect)
        request = urllib.request.Request('https://api.notion.com/v1/pages/test')
        for status in (301, 302, 303, 307, 308):
            self.assertIsNone(redirect.redirect_request(request, None, status, '', {}, 'https://other.test/'))

    def test_jira_selects_native_context_and_slack_does_not(self):
        client = load('client')
        response = self.response({'ok': True})
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.headers = {}
        settings = {'JIRA_BASE_URL': 'https://jira.test', 'JIRA_EMAIL': 'test@example.test'}
        with mock.patch.object(client, 'env_from_dotenv', side_effect=settings.get), mock.patch.object(client, 'keychain', return_value='dummy'), mock.patch.object(urllib.request, 'urlopen', return_value=response) as transport, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.cmd_jira(['GET', '/rest/api/3/myself']), client.OK)
            self.assert_verified(transport.call_args.kwargs['context'])
            self.assertEqual(transport.call_args.args[0].full_url, 'https://jira.test/rest/api/3/myself')
            with mock.patch.object(client, 'get_slack_access_token', return_value='dummy'):
                client.slack_request('https://slack.com/api/api.test')
            self.assertNotIn('context', transport.call_args.kwargs)
