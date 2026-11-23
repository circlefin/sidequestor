"""Notion watermark, registration and transport regression tests (no credentials)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / 'src/sidequestor/runtime/yaas-triage'
ID = '00000000-0000-0000-0000-000000000001'


def load(name, path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def row(second, ident=ID, **extra):
    from datetime import datetime, timezone
    return {'object': 'page', 'id': ident,
            'last_edited_time': datetime.fromtimestamp(second, timezone.utc).isoformat(), **extra}


def page(rows, more=False, cursor=None):
    return {'object': 'list', 'results': rows, 'has_more': more, 'next_cursor': cursor}


class NotionSurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = load('notion_surface_test', TRIAGE/'surfaces/notion-call.py')

    def test_token_kept_out_of_argv_and_json_output(self):
        response=mock.MagicMock()
        response.__enter__.return_value=io.StringIO('{"object":"page","id":"safe"}')
        opener=mock.Mock()
        opener.open.return_value=response
        with mock.patch.dict(os.environ,{'NOTION_TOKEN':'unique-secret'}), mock.patch.object(self.s.urllib.request,'build_opener',return_value=opener), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.s.main(['GET','/v1/pages/'+ID]),0)
        self.assertNotIn('unique-secret',output.getvalue())
        req=opener.open.call_args.args[0]
        self.assertEqual(req.get_header('Authorization'),'Bearer unique-secret')
        self.assertEqual(req.get_header('Notion-version'),'2026-03-11')
        self.assertNotIn('unique-secret',req.full_url)

    def test_refuses_writes_and_host_injection_before_token(self):
        for method,path in [('POST','/v1/comments'),('PATCH','/v1/pages/'+ID),('GET','https://evil.test'),('GET','//evil.test')]:
            with mock.patch.object(self.s,'token') as token:
                with self.assertRaises(ValueError): self.s.call(method,path)
                token.assert_not_called()

    def test_http_failure_classification_and_no_secret_body(self):
        for status,code in ((400,5),(401,1),(403,1),(404,5),(409,4),(429,4),(503,4),(406,2),(302,2)):
            with mock.patch.object(self.s,'call',side_effect=urllib.error.HTTPError('',status,'',{},io.BytesIO(b'secret'))), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(self.s.main(['GET','/v1/pages/'+ID]),code)
            self.assertNotIn('secret',output.getvalue())

    def test_malformed_success_json_is_error_not_misconfig(self):
        response=mock.MagicMock()
        response.__enter__.return_value=io.StringIO('broken')
        opener=mock.Mock()
        opener.open.return_value=response
        with mock.patch.object(self.s,'token',return_value='secret'), mock.patch.object(self.s.urllib.request,'build_opener',return_value=opener), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.s.main(['GET','/v1/pages/'+ID]),2)

    def test_default_credential_name_retains_legacy_test_item(self):
        with mock.patch.dict(os.environ,{'NOTION_TOKEN':''}), mock.patch.object(self.s.subprocess,'run',side_effect=[subprocess.CompletedProcess([],44,'',''),subprocess.CompletedProcess([],0,'saved-token','')]) as call:
            with mock.patch.dict(os.environ):
                os.environ.pop('NOTION_KEYCHAIN_SERVICE',None)
                self.assertEqual(self.s.token(),'saved-token')
        self.assertIn('sidequestor-notion',call.call_args_list[0].args[0])
        self.assertIn('sidequestor-notion-test',call.call_args_list[1].args[0])
        with mock.patch.dict(os.environ,{'NOTION_TOKEN':'','NOTION_KEYCHAIN_SERVICE':'chosen-item'}), mock.patch.object(self.s.subprocess,'run',return_value=subprocess.CompletedProcess([],44,'','')) as call:
            with self.assertRaises(self.s.CredentialError): self.s.token()
        self.assertEqual(call.call_count,1)
        self.assertIn('chosen-item',call.call_args.args[0])

    def test_unknown_keychain_failure_uses_error_backoff(self):
        with mock.patch.dict(os.environ,{'NOTION_TOKEN':''}), mock.patch.object(self.s.subprocess,'run',return_value=subprocess.CompletedProcess([],13,'','secret')), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(self.s.main(['GET','/v1/pages/'+ID]),2)
        self.assertNotIn('secret',output.getvalue())

    def test_keychain_missing_vs_locked(self):
        for rc,exc in ((44,self.s.CredentialError),(36,self.s.TransientCredentialError)):
            with mock.patch.dict(os.environ,{'NOTION_TOKEN':''}), mock.patch.object(self.s.subprocess,'run',return_value=subprocess.CompletedProcess([],rc,'','')):
                with self.assertRaises(exc): self.s.token()


if __name__ == '__main__':
    unittest.main()
