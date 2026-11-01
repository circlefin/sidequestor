from __future__ import annotations
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SURFACES = ROOT / 'src/sidequestor/runtime/yaas-triage/surfaces'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SlackReadCooldownTests(unittest.TestCase):
    def setUp(self):
        with patch.object(sys, 'path', [str(SURFACES), *sys.path]):
            self.client = load('cooldown_client_test', SURFACES / 'client.py')
        self.cooldown = self.client.read_cooldown
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'SIDEQUESTOR_WORKSPACE': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def invoke(self, tool='slack_read_thread'):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = self.client.cmd_mcp([tool, '{}'])
        return code, output.getvalue()

    def test_retry_after_seconds_dates_and_unusable_values(self):
        self.assertEqual(self.cooldown.retry_seconds({'rEtRy-AfTeR': '12'}, 1000), 12)
        self.assertEqual(self.cooldown.retry_seconds({'Retry-After': formatdate(1060,usegmt=True)},1000),60)
        for value in ('-1','0','garbage','nan','inf','1e300',formatdate(990,usegmt=True)):
            self.assertEqual(self.cooldown.retry_seconds({'Retry-After':value},1000),60,value)
        self.assertEqual(self.cooldown.retry_seconds({},1000),60)
        # A legitimate long Retry-After must not be shortened into an early retry.
        self.assertEqual(self.cooldown.retry_seconds({'Retry-After':'7200'},1000),7200)

    def test_429_is_one_transport_call_without_holding_other_reads(self):
        with patch.object(self.client, 'slack_request_with_headers', return_value=(429, 'ratelimited', {'Retry-After': '30'})) as request:
            self.assertEqual(self.invoke()[0], self.client.TRANSIENT)
            request.assert_called_once()
        self.assertFalse((self.root / 'state/triage/slack-read-cooldown.json').exists())
        for tool in self.cooldown.READ_TOOLS:
            with self.subTest(tool=tool), patch.object(self.client, 'slack_request_with_headers', return_value=(200, '{"result":{"content":[{"text":"ok"}]}}', {})) as request:
                self.assertEqual(self.invoke(tool)[0], self.client.OK)
                request.assert_called_once()

    def test_saved_legacy_deadline_cannot_suppress_requests(self):
        self.cooldown.record_cooldown(self.root, {'Retry-After': '7200'})
        path = self.root / 'state/triage/slack-read-cooldown.json'
        before = path.read_bytes()
        with patch.object(self.cooldown, 'read_deadline', side_effect=AssertionError('disabled cooldown read')), \
                patch.object(self.cooldown, 'record_cooldown', side_effect=AssertionError('disabled cooldown write')):
            for tool in self.cooldown.READ_TOOLS:
                with self.subTest(tool=tool), patch.object(self.client, 'slack_request_with_headers', return_value=(200, '{"result":{"content":[{"text":"ok"}]}}', {})) as request:
                    self.assertEqual(self.invoke(tool)[0], self.client.OK)
                    request.assert_called_once()
        self.assertEqual(path.read_bytes(), before)

    def test_http_200_tool_rate_limits_remain_transient_without_shared_deadline(self):
        results = [
            {'isError': True, 'content': [{'type': 'text', 'text': 'ratelimited'}]},
            {'content': [{'type': 'text', 'text': json.dumps({'ok': False, 'error': 'rate_limited'})}]},
        ]
        for result in results:
            with self.subTest(result=result), patch.object(self.cooldown.time, 'time', return_value=1000), \
                    patch.object(self.client, 'slack_request_with_headers', return_value=(200, json.dumps({'result': result}), {'Retry-After': '30'})) as request:
                path = self.root / 'state/triage/slack-read-cooldown.json'
                path.unlink(missing_ok=True)
                self.assertEqual(self.invoke()[0], self.client.TRANSIENT)
                self.assertFalse(path.exists())
                request.assert_called_once()

    def test_tool_error_handling_does_not_throttle_success_or_unrelated_failures(self):
        cases = [
            ({'content': [{'type': 'text', 'text': json.dumps({'ok': True, 'messages': [{'text': 'Discuss ratelimited and invalid_auth errors'}]})}]}, self.client.OK),
            ({'isError': True, 'content': [{'type': 'text', 'text': 'timeout'}]}, self.client.TRANSIENT),
            ({'isError': True, 'content': [{'type': 'text', 'text': 'invalid_auth'}]}, self.client.AUTH),
            ({'isError': True, 'content': [{'type': 'text', 'text': 'invalid_arguments'}]}, self.client.ERROR),
            ({'content': [{'type': 'text', 'text': json.dumps({'ok': False, 'error': {}})}]}, self.client.ERROR),
            ({'isError': True, 'content': [{'type': 'text', 'text': {'unexpected': 'object'}}]}, self.client.ERROR),
        ]
        for result, expected in cases:
            with self.subTest(result=result), patch.object(self.client, 'slack_request_with_headers', return_value=(200, json.dumps({'result': result}), {})):
                self.assertEqual(self.invoke()[0], expected)
                self.assertFalse((self.root / 'state/triage/slack-read-cooldown.json').exists())

    def test_auth_transport_never_refreshes_on_429(self):
        with patch.object(self.client,'request_with_headers', return_value=(429,'{"error":"invalid_auth"}',{'Retry-After':'20'})) as transport, \
                patch.object(self.client,'get_slack_access_token', return_value='not-a-real-token') as auth:
            self.assertEqual(self.invoke()[0],4)
            self.assertEqual(transport.call_count,1)
            self.assertEqual(auth.call_count,1)

    def test_rate_limits_and_other_transients_do_not_create_shared_deadlines(self):
        with patch.object(self.cooldown.time,'time',return_value=1000), \
                patch.object(self.client,'slack_request_with_headers',return_value=(200,'{"error":{"message":"ratelimited"}}',{})):
            self.assertEqual(self.invoke()[0],4)
            self.assertFalse((self.root/'state/triage/slack-read-cooldown.json').exists())
        path=self.root/'state/triage/slack-read-cooldown.json'
        for reply in [(503,'service unavailable',{}),(401,'invalid_auth',{}),(200,'{"ok":false,"error":"missing_scope"}',{})]:
            with patch.object(self.client,'slack_request_with_headers',return_value=reply):
                self.invoke()
                self.assertFalse(path.exists())

    def test_writes_unknown_tools_and_other_workspaces_unaffected(self):
        self.cooldown.record_cooldown(self.root,{'Retry-After':'100'})
        for tool in ('slack_send_message','slack_send_message_draft','unknown_future_tool'):
            with patch.object(self.client,'slack_request',return_value=(200,'{"ok":true}')) as mutate:
                self.assertEqual(self.invoke(tool)[0],0)
                mutate.assert_called_once()
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(self.cooldown.read_deadline(Path(other)),0)
        with patch.dict(os.environ,{'SIDEQUESTOR_WORKSPACE':'','YAAS_WORKSPACE':''}):
            self.assertIsNone(self.cooldown.workspace_root())
            self.assertGreater(self.cooldown.record_cooldown(None,{}),0)

    def test_concurrent_updates_keep_latest_deadline_and_corrupt_state_fails_open(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda delay:self.cooldown.record_cooldown(self.root,{'Retry-After':str(delay)},1000),[30,90,10,60]))
        self.assertEqual(self.cooldown.read_deadline(self.root,1000),1090)
        path=self.root/'state/triage/slack-read-cooldown.json'
        for text in ('not json','[]','{"retry_at": "nan"}','{"retry_at":1e300}'):
            path.write_text(text)
            with self.assertLogs('slack_read_cooldown',level='WARNING'):
                self.assertEqual(self.cooldown.read_deadline(self.root,1000),0)

    def test_ratelimited_read_uses_existing_skip_classification(self):
        with patch.object(sys, 'path', [str(SURFACES.parent), *sys.path]):
            check=load('cooldown_tick_check_test',SURFACES.parent/'tick_check.py')
        watch={'type':'slack_thread','watch_id':'watch-abcdef'}
        verdict=check.classify({'outcome':'ratelimited','preview':'slack ratelimited','complete':False},watch)
        self.assertEqual(verdict['verdict'],'skip')
        self.assertNotIn('advance_to',verdict)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for dashboard classification checks')
    def test_temporary_read_delay_stays_watching_without_hiding_actionable_states(self):
        script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync('src/sidequestor/runtime/dashboard.html','utf8');
const definitions=source.split('\n').find(line=>line.startsWith('function isInitialising('));
const context={attentionFor:()=>[],isRunning:()=>false};vm.createContext(context);
vm.runInContext(definitions+';globalThis.group=questGroup',context);
const q={id:'quest',ratelimited_watches:[{type:'slack_thread',reason:'slack ratelimited'}]};
assert.equal(context.group(q),'Watching');
assert.equal(context.group({...q,status:'blocked'}),'Needs a fix');
assert.equal(context.group({...q,backoff_watches:[{}]}),'Investigate');
context.isRunning=()=>true;assert.equal(context.group(q),'Running');
context.attentionFor=()=>[{kind:'review'}];assert.equal(context.group(q),'Needs you');
"""
        result = subprocess.run(['node','-e',script],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
