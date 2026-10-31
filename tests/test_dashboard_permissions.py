from __future__ import annotations

import json
import importlib.util
import builtins
from unittest.mock import patch
import os
from pathlib import Path
import sys
import tempfile
import unittest

from sidequestor.workspace import file_lock

TRIAGE = Path(__file__).resolve().parents[1] / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(TRIAGE))
from dashboard_permissions import PermissionConflict, set_send_permission
sys.path.remove(str(TRIAGE))


class DashboardPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.quest = self.root / 'state/quests/active/quest-demo'
        self.quest.mkdir(parents=True)
        self.meta = self.quest / 'meta.json'
        self.original = {'title':'Demo', 'status':'active', 'allow_send':False,
                         'allow_send_channels':['C123'], 'custom':{'preserve':True}}
        self.meta.write_text(json.dumps(self.original))
        self.queue = self.root / 'state/pending-approvals.json'
        self.queue.write_bytes(b'{"items":[{"status":"reviewed","id":"approved-item"}]}')

    def change(self, **payload):
        return set_send_permission(self.root, 'quest-demo', payload)

    def test_optional_permission_module_imports_without_package_runtime(self):
        original = builtins.__import__
        def guard(name, *args, **kwargs):
            if name == 'sidequestor' or name.startswith('sidequestor.'):
                raise ModuleNotFoundError('package intentionally unavailable')
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('standalone_permissions', TRIAGE / 'dashboard_permissions.py')
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=guard):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.set_send_permission))

    def test_enable_and_disable_are_audited_preserving_exceptions_and_queue(self):
        queue_before = self.queue.read_bytes()
        result=self.change(allow_send=True, expected_allow_send=False, confirm_send=True)
        self.assertTrue(result['allow_send'])
        meta=json.loads(self.meta.read_text())
        self.assertEqual(meta['allow_send_channels'], ['C123'])
        self.assertEqual(meta['custom'], self.original['custom'])
        self.assertEqual(meta['send_permission_changes'][0]['previous'], False)
        self.assertEqual(meta['send_permission_changes'][0]['source'], 'dashboard')
        self.change(allow_send=False, expected_allow_send=True)
        self.assertEqual(len(json.loads(self.meta.read_text())['send_permission_changes']),2)
        self.assertEqual(self.queue.read_bytes(),queue_before)

    def test_requires_explicit_confirmation_and_boolean_compare_and_set(self):
        original=self.meta.read_bytes()
        for payload in ({'allow_send':True,'expected_allow_send':False},
                        {'allow_send':'true','expected_allow_send':False,'confirm_send':True},
                        {'allow_send':True,'confirm_send':True}):
            with self.assertRaises(ValueError):self.change(**payload)
        with self.assertRaises(PermissionConflict):
            self.change(allow_send=False,expected_allow_send=True)
        self.assertEqual(self.meta.read_bytes(), original)

    def test_active_triage_lock_rejects_change_without_waiting(self):
        with file_lock(self.root/'logs/triage.lock'):
            with self.assertRaises(PermissionConflict):
                self.change(allow_send=True,expected_allow_send=False,confirm_send=True)
        self.assertFalse(json.loads(self.meta.read_text())['allow_send'])

    def test_inactive_and_invalid_paths_cannot_change_policy(self):
        self.meta.write_text(json.dumps({**self.original,'status':'completed'}))
        with self.assertRaises(ValueError):self.change(allow_send=True,expected_allow_send=False,confirm_send=True)
        for qid in ('../quest-demo','quest-demo/..','..','', '/etc'):
            with self.assertRaises(ValueError):
                set_send_permission(self.root,qid,{'allow_send':False,'expected_allow_send':False})
        self.meta.unlink()
        outside=self.root/'outside.json';outside.write_text(json.dumps(self.original));self.meta.symlink_to(outside)
        with self.assertRaises(ValueError):self.change(allow_send=True,expected_allow_send=False,confirm_send=True)
        self.assertFalse(json.loads(outside.read_text())['allow_send'])


class DashboardPermissionHTTPTests(unittest.TestCase):
    # Reuse the local-only HTTP fixture; no task actions or production state.
    from test_dashboard_theme import ThemeHTTPTests as _HTTPFixture
    setUp = _HTTPFixture.setUp
    shutdown = _HTTPFixture.shutdown
    request = _HTTPFixture.request

    def post_policy(self, payload, *, cookie=True):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type':'application/json'}
        if cookie and self.cookie:
            headers['Cookie']=self.cookie
        connection.request('POST','/api/quest/quest-demo/send-permission',json.dumps(payload),headers)
        response=connection.getresponse()
        result=(response.status,response.read())
        connection.close()
        return result

    def test_authenticated_confirmed_compare_and_set_endpoint(self):
        meta=self.root/'state/quests/active/quest-demo/meta.json'
        meta.parent.mkdir(parents=True)
        meta.write_text('{"status":"active","allow_send":false}')
        payload={'allow_send':True,'expected_allow_send':False,'confirm_send':True}
        self.assertEqual(self.post_policy(payload,cookie=False)[0],403)
        status,headers,_=self.request('/')
        self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.post_policy({**payload,'confirm_send':False})[0],400)
        self.assertEqual(self.post_policy(payload)[0],200)
        self.assertTrue(json.loads(meta.read_text())['allow_send'])
        self.assertEqual(self.post_policy(payload)[0],409)
