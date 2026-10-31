"""Behavior tests for bounded discovery, comment detection and durable delivery."""
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
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

ROOT=Path(__file__).resolve().parents[1]
TRIAGE=ROOT/'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0,str(TRIAGE/'checkers'))
import notion
import notion_tree as tree
from notion_store import Store, watch_key, credential_key
sys.path.pop(0)


def uid(n):return str(UUID(int=n))
def row(n,kind='page',text='old',edited=200,children=False,parent=None):
    from datetime import datetime,timezone
    stamp=datetime.fromtimestamp(edited,timezone.utc).isoformat()
    r={'id':uid(n),'object':kind,'last_edited_time':stamp,'created_time':stamp,'in_trash':False}
    if kind=='page':r['properties']={'Name':{'type':'title','title':[{'plain_text':text}]}}
    elif kind=='comment':r.update(rich_text=[{'plain_text':text}],discussion_id=uid(n+10000),parent={'block_id':parent or uid(10)})
    else:r.update(object='block',type=kind,has_children=children,parent={'page_id':parent or uid(1)}) ;r[kind]={'rich_text':[{'plain_text':text}]}
    return r


class FakeNotion:
    def __init__(self):
        self.pages={uid(1):row(1),uid(2):row(2),uid(3):row(3),uid(4):row(4)}
        self.children={uid(1):[row(10,'paragraph'),row(20,'toggle',children=True),row(2,'child_page')],
                       uid(20):[row(21,'paragraph')],uid(2):[row(22,'paragraph'),row(3,'child_page')],
                       uid(3):[row(23,'paragraph')],uid(4):[row(40,'paragraph')]}
        self.comments={uid(10):[row(100,'comment')]}
        self.source=[uid(4)]
        self.calls=[]
        self.page_size=100
        self.incomplete=False
        self.repeated_cursor=False

    def __call__(self,method,path,body=None,timeout=35):
        self.calls.append((method,path,body))
        parts=urlsplit(path);bits=parts.path.strip('/').split('/')[1:]
        query={k:v[0] for k,v in parse_qs(parts.query).items()}
        if bits[0]=='pages':return json.loads(json.dumps(self.pages[bits[1]]))
        if bits[0]=='databases':return {'object':'database','data_sources':[{'id':uid(90)}]}
        if bits[0]=='blocks' and len(bits)==2:
            ident=bits[1]
            if ident in self.pages:return row(UUID(ident).int,'child_page',children=True)
            return next(r for rows in self.children.values() for r in rows if r['id']==ident)
        if bits[0]=='data_sources':
            query=body or {}
            items=[self.pages[i] for i in self.source if bool(self.pages[i].get('is_archived'))==bool(query.get('is_archived')) and not self.pages[i].get('in_trash')]
        elif bits[0]=='comments':items=self.comments.get(query['block_id'],[])
        elif bits[0]=='blocks':items=self.children.get(bits[1],[])
        else:raise AssertionError(path)
        offset=int(query.get('start_cursor','0'));size=min(int(query.get('page_size',100)),self.page_size)
        more=offset+size<len(items)
        result={'object':'list','results':items[offset:offset+size],'has_more':more,
                'next_cursor':str(offset+size) if more else None}
        if self.incomplete and bits[0]=='data_sources':result['request_status']={'type':'incomplete'}
        if self.repeated_cursor and more:result['next_cursor']='1'
        return json.loads(json.dumps(result))


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.api=FakeNotion()
        self.entry={'type':'notion_page','watch_id':'watch-abcdef','page_id':uid(1),'last_checked_ts':'1000','max_requests':50,'scan_seconds':30}
        # Unit API fixtures don't need actual network pacing. Dedicated tests
        # below exercise the shared cooldown with the real Store.defer.
        self.pacing=mock.patch.object(Store,'defer',lambda *args:None);self.pacing.start();self.addCleanup(self.pacing.stop)

    def check(self,entry=None,now=2000,clear=True,api=None):
        e={**self.entry,**(entry or {})}
        if clear and (self.root/'collector.sqlite3').exists():
            with contextlib.closing(Store(self.root)) as store:
                store.db.execute('DELETE FROM cache');store.db.commit()
        output=io.StringIO()
        with mock.patch.object(notion,'call',side_effect=api or self.api),contextlib.redirect_stdout(output):
            notion.run(e,e['type'],now=now,store_dir=self.root)
        value=json.loads(output.getvalue())
        with contextlib.closing(Store(self.root)) as store:
            state=store.load(watch_key(e,e['type'],tree.options(e,e['type'])))
        return value,state

    def baseline(self,entry=None):
        value,state=self.check(entry)
        self.assertEqual(value['outcome'],'clean',value)
        self.assertEqual(state['coverage']['status'],'complete')
        return state

    def test_hundred_blocks_are_one_watch_with_bounded_resumable_work(self):
        self.api.children[uid(1)]=[row(i,'paragraph') for i in range(1000,1100)]
        for _ in range(50):
            before=len(self.api.calls)
            value,state=self.check({'max_requests':3},clear=False)
            self.assertLessEqual(len(self.api.calls)-before,3)
            if state['coverage']['status']=='complete':break
        else:self.fail('100-block traversal did not complete')
        self.assertEqual(len([k for k in state['snapshots'] if k.startswith('block:')]),100)
        with contextlib.closing(Store(self.root)) as store:
            self.assertEqual(store.db.execute('SELECT count(*) FROM watches').fetchone()[0],1)

    def test_initial_cursor_minute_is_replayed_to_avoid_rounding_loss(self):
        self.api.comments[uid(10)]=[row(150,'comment',edited=960)]
        value,state=self.check({'last_checked_ts':'1000.500000'})
        self.assertEqual(value['outcome'],'dirty')
        self.assertIn(uid(150),{event['id'] for event in state['events']})

    def test_live_edit_under_read_failure_is_retained_for_retry(self):
        self.baseline()
        self.api.comments[uid(1)]=[row(151,'comment')]
        def interrupted(method,path,body=None,timeout=35):
            if '/children?' in path:raise RuntimeError('read interrupted')
            return self.api(method,path,body,timeout)
        with self.assertRaises(RuntimeError):self.check(api=interrupted,now=2001)
        value,state=self.check(now=2002)
        self.assertEqual(value['outcome'],'dirty')
        self.assertIn(uid(151),{event['id'] for event in state['events']})

    def test_manifest_and_add_watch_canonical_scope_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            quest=root/'state/quests/active/notion-test'
            quest.mkdir(parents=True)
            path=quest/'watch.json'
            path.write_text('{"watches":[]}')
            env={**os.environ,'SIDEQUESTOR_WORKSPACE':raw,'SIDEQUESTOR_CHECKER_CONNECTORS':'notion','YAAS_CHECKER_CONNECTORS':'notion'}
            helper=TRIAGE/'ledger/add-watch.py'
            for entry in [{'type':'notion_page','page_id':uid(1),'reason':'test'},
                          {'type':'notion_page','page_id':uid(1).replace('-',''),'subpage_depth':0,'include_subpage_content':False,'include_subpage_comments':False,'reason':'same'},
                          {'type':'notion_page','page_id':uid(1),'subpage_depth':1,'include_subpage_comments':True,'reason':'different'}]:
                proc=subprocess.run([sys.executable,str(helper),'notion-test',json.dumps(entry)],env=env,text=True,capture_output=True)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            self.assertEqual(len(json.loads(path.read_text())['watches']),2)
        for kind in ['notion_page','notion_comments','notion_data_source']:
            manifest=json.loads((TRIAGE/f'checkers/{kind}.watch.json').read_text())
            self.assertTrue({'subpage_depth','include_subpage_content','include_subpage_comments'}<=set(manifest['identity']))

    def test_file_replacement_is_detected_but_signature_refresh_is_not(self):
        block=row(60,'image')
        block['image']={'type':'file','file':{'url':'https://files.example/object-A/image.png?signature=one','expiry_time':'soon'}}
        self.api.children[uid(1)].append(block)
        self.baseline()
        block['image']['file'].update(url='https://files.example/object-A/image.png?signature=two',expiry_time='later')
        value,_=self.check()
        self.assertEqual(value['outcome'],'clean')
        block['image']['file']['url']='https://files.example/object-B/image.png?signature=three'
        value,state=self.check()
        self.assertIn(uid(60),{e['id'] for e in state['events']})

    def test_incremental_scans_do_not_accumulate_runtime_backoff(self):
        from types import SimpleNamespace
        sys.path.insert(0,str(TRIAGE))
        try:
            spec=importlib.util.spec_from_file_location('notion_runtime_tick',TRIAGE/'tick.py')
            tick=importlib.util.module_from_spec(spec);spec.loader.exec_module(tick)
        finally:sys.path.pop(0)
        self.api.children[uid(1)]=[row(i,'paragraph') for i in range(2000,2030)]
        t=SimpleNamespace(clean_watches=[],dirty_watches=[],dirty_quests=[],skipped_quests=[],results=[],log=lambda _:None,event=lambda _:None)
        with mock.patch.object(tick,'_bump_unacked') as bump:
            for _ in range(12):
                value,state=self.check({'max_requests':1},clear=False)
                verdict=tick.tick_check.classify(tick._parse_checker(json.dumps(value)),self.entry)
                self.assertEqual(verdict['verdict'],'clean')
                t.results=[{'status':'ok','qid':'quest-test','watch_id':self.entry['watch_id'],'type':'notion_page',**verdict}]
                tick.analyze(t)
                self.assertEqual(float(t.clean_watches[-1]['advance_to']),1000)
            bump.assert_not_called()

    def test_disappeared_descendant_does_not_stop_other_comments_or_repeat_gap(self):
        self.baseline()
        self.api.comments[uid(21)]=[row(160,'comment')]
        def partly_gone(method,path,body=None,timeout=35):
            if 'block_id='+uid(10) in path:raise notion.ResourceUnavailable('Notion HTTP 404')
            return self.api(method,path,body,timeout)
        value,state=self.check(api=partly_gone,now=2001)
        self.assertEqual(state['coverage']['status'],'partial')
        self.assertIn(uid(160),{e['id'] for e in state['events']})
        self.assertIn('coverage_gap',{e['kind'] for e in state['events']})
        self.assertEqual(len(state['coverage']['gaps']),1)
        later,state=self.check({'last_checked_ts':value['advance_to']},api=partly_gone,now=2002)
        self.assertEqual(later['outcome'],'clean')
        self.assertEqual(state['events'],[])
        self.assertEqual(state['coverage']['status'],'partial')
        self.assertEqual(state['coverage']['last_complete_scan'],2000)

    def test_unavailable_selected_root_is_still_misconfiguration(self):
        def missing(*args,**kwargs):raise notion.ResourceUnavailable('Notion HTTP 404')
        with self.assertRaises(notion.Misconfig):self.check(api=missing)

    def test_unsupported_descendant_children_can_recover_next_cycle(self):
        self.baseline()
        def unsupported(method,path,body=None,timeout=35):
            if '/blocks/'+uid(20)+'/children' in path:raise notion.ResourceUnavailable('Notion HTTP 400')
            return self.api(method,path,body,timeout)
        value,state=self.check(api=unsupported,now=2001)
        self.assertEqual(state['coverage']['status'],'partial')
        self.api.comments[uid(21)]=[row(161,'comment')]
        recovered,state=self.check({'last_checked_ts':value['advance_to']},now=2002)
        self.assertEqual(state['coverage']['status'],'complete')
        self.assertIn(uid(161),{e['id'] for e in state['events']})

    def test_parallel_watches_wait_for_shared_request_and_both_progress(self):
        import threading,time
        entered=threading.Event()
        outputs=[];errors=[]
        def slow(method,path,body=None,timeout=35):
            entered.set();time.sleep(0.1)
            return self.api(method,path,body,timeout)
        def worker(watch_id):
            try:
                entry={**self.entry,'watch_id':watch_id,'max_requests':2,'scan_seconds':5}
                tree.run(entry,'notion_page',api=slow,now=2000,root=self.root)
                with contextlib.closing(Store(self.root)) as store:
                    outputs.append(store.load(watch_key(entry,'notion_page',tree.options(entry,'notion_page'))))
            except Exception as exc:errors.append(exc)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            first=threading.Thread(target=worker,args=('watch-first',));first.start()
            self.assertTrue(entered.wait(2))
            second=threading.Thread(target=worker,args=('watch-second',));second.start()
            first.join(7);second.join(7)
        self.assertFalse(errors,errors)
        self.assertEqual(len(outputs),2)
        self.assertTrue(all(s['coverage']['requests_this_tick']>0 for s in outputs))
        self.assertEqual(len([p for _,p,_ in self.api.calls if p=='/v1/pages/'+uid(1)]),1)

    def test_local_scan_timeout_resumes_without_global_rate_limit_cooldown(self):
        import time
        self.pacing.stop()
        transport=notion.call
        with mock.patch.object(notion.subprocess,'run',side_effect=subprocess.TimeoutExpired('bridge',3)):
            value,state=self.check({'scan_seconds':3},api=transport)
        self.assertEqual(value['outcome'],'clean')
        self.assertEqual(float(value['advance_to']),1000)
        self.assertEqual(state['coverage']['status'],'scanning')
        self.assertIn('local scan deadline',state['coverage']['reason'])
        with contextlib.closing(Store(self.root)) as store:
            self.assertLess(store.not_before(credential_key()),time.time()+1)
        value,state=self.check({'watch_id':'watch-other','max_requests':1})
        self.assertEqual(state['coverage']['requests_this_tick'],1)

    def test_nearly_spent_budget_does_not_launch_an_api_process(self):
        with contextlib.closing(Store(self.root)) as store:
            collector=tree.Collector(store,'budget-test',self.entry,'notion_page',tree.options(self.entry,'notion_page'),self.api,2000)
            import time
            collector.deadline=time.monotonic()+0.5
            with self.assertRaises(tree.Budget):collector.fetch('GET','/v1/pages/'+uid(1))
        self.assertEqual(self.api.calls,[])

    def test_expired_cache_is_removed_and_secure_delete_enabled(self):
        import time
        store=Store(self.root)
        store.cache('old',{'body':'private fixture text'},time.time()-100)
        self.assertEqual(store.db.execute('PRAGMA secure_delete').fetchone()[0],1)
        store.close()
        with contextlib.closing(Store(self.root)) as store:
            self.assertEqual(store.db.execute('SELECT count(*) FROM cache').fetchone()[0],0)

    def test_page_revision_preserves_detection_of_projected_property_edits(self):
        self.baseline()
        self.api.pages[uid(1)]['last_edited_time']=row(1,edited=1300)['last_edited_time']
        value,state=self.check()
        self.assertIn('page',{e['kind'] for e in state['events']})

    def test_default_covers_nested_blocks_and_stops_at_subpages(self):
        state=self.baseline()
        self.assertIn('block:'+uid(21),state['snapshots'])
        self.assertNotIn('page:'+uid(2),state['snapshots'])
        self.assertFalse(any('/pages/'+uid(2) in path for _,path,_ in self.api.calls))
        self.assertFalse(any('block_id='+uid(22) in path for _,path,_ in self.api.calls))

    def test_anchored_comment_and_reply_detected_without_any_page_edit(self):
        self.baseline()
        self.api.comments[uid(21)]=[row(101,'comment',text='anchored')]
        first,state=self.check(now=2001)
        self.assertEqual(first['count'],1)
        self.assertEqual(state['events'][0]['container_id'],uid(21))
        self.api.comments[uid(21)].append(row(102,'comment',text='reply'))
        second,state=self.check({'last_checked_ts':first['advance_to']},now=2002)
        self.assertEqual(second['count'],1)
        self.assertEqual(state['events'][0]['id'],uid(102))

    def test_comment_edit_same_timestamp_uses_fingerprint(self):
        self.baseline()
        self.api.comments[uid(10)][0]['rich_text']=[{'plain_text':'edited in the same minute'}]
        value,state=self.check()
        self.assertEqual(value['count'],1)
        self.assertEqual(state['events'][0]['kind'],'comment')

    def test_new_block_enrolls_its_existing_comment(self):
        self.baseline()
        self.api.children[uid(1)].append(row(30,'paragraph'))
        self.api.comments[uid(30)]=[row(103,'comment')]
        value,state=self.check()
        self.assertEqual(value['outcome'],'dirty')
        self.assertIn(uid(103),{e['id'] for e in state['events']})
        self.assertIn('block:'+uid(30),state['snapshots'])

    def test_block_content_change_detected_when_page_timestamp_unchanged(self):
        self.baseline()
        self.api.children[uid(20)][0]['paragraph']['rich_text']=[{'plain_text':'new nested content'}]
        value,state=self.check()
        self.assertEqual(value['count'],1)
        self.assertEqual(state['events'][0]['kind'],'block')

    def test_content_reorders_and_removals_are_structural_not_deletion_claims(self):
        self.baseline()
        self.api.children[uid(1)]=list(reversed(self.api.children[uid(1)]))
        value,state=self.check()
        self.assertEqual({e['kind'] for e in state['events']},{'children'})

    def test_comment_only_watch_never_triggers_on_body_edits(self):
        entry={'type':'notion_comments','block_id':uid(1)}
        self.baseline(entry)
        self.api.children[uid(20)][0]['paragraph']['rich_text']=[{'plain_text':'new'}]
        value,state=self.check(entry)
        self.assertEqual(value['outcome'],'clean')
        self.api.comments[uid(21)]=[row(104,'comment')]
        value,state=self.check(entry)
        self.assertEqual({e['kind'] for e in state['events']},{'comment'})

    def test_depth_one_comments_only_and_depth_two_boundary(self):
        opts={'subpage_depth':1,'include_subpage_comments':True}
        state=self.baseline(opts)
        self.assertNotIn('page:'+uid(2),state['snapshots'])
        self.api.pages[uid(2)]['properties']['Name']['title']=[{'plain_text':'subpage title edit'}]
        self.api.comments[uid(22)]=[row(105,'comment')]
        self.api.comments[uid(23)]=[row(106,'comment')]
        value,state=self.check(opts)
        self.assertEqual({e['id'] for e in state['events']},{uid(105)})
        self.assertFalse(any('/pages/'+uid(3) in p for _,p,_ in self.api.calls))
        deep={**opts,'subpage_depth':2}
        self.baseline(deep)
        self.api.comments[uid(23)].append(row(107,'comment'))
        value,state=self.check(deep)
        self.assertIn(uid(107),{e['id'] for e in state['events']})

    def test_depth_one_content_only_does_not_scan_subpage_comments(self):
        opts={'subpage_depth':1,'include_subpage_content':True}
        self.baseline(opts)
        self.api.comments[uid(22)]=[row(108,'comment')]
        self.api.children[uid(2)][0]['paragraph']['rich_text']=[{'plain_text':'sub content'}]
        value,state=self.check(opts)
        self.assertEqual({e['kind'] for e in state['events']},{'block'})
        self.assertNotIn('comment:'+uid(108),state['snapshots'])

    def test_new_subpage_enrolled_automatically_within_depth(self):
        opts={'subpage_depth':1,'include_subpage_comments':True}
        self.baseline(opts)
        self.api.pages[uid(5)]=row(5)
        self.api.children[uid(1)].append(row(5,'child_page'))
        self.api.children[uid(5)]=[row(50,'paragraph')]
        self.api.comments[uid(50)]=[row(109,'comment')]
        value,state=self.check(opts)
        self.assertIn(uid(109),{e['id'] for e in state['events']})

    def test_cycle_and_duplicate_membership_are_visited_once(self):
        self.api.children[uid(2)].extend([row(1,'child_page'),row(22,'paragraph')])
        state=self.baseline({'subpage_depth':100,'include_subpage_comments':True})
        self.assertLess(len(self.api.calls),25)
        comments=[p for _,p,_ in self.api.calls if 'block_id='+uid(22) in p]
        self.assertEqual(len(comments),1)

    def test_database_existing_and_new_rows_get_recursive_comments(self):
        opts={'type':'notion_data_source','data_source_id':uid(90)}
        self.baseline(opts)
        self.api.pages[uid(5)]=row(5,edited=1500)
        self.api.children[uid(5)]=[row(50,'paragraph',edited=1500)]
        self.api.comments[uid(50)]=[row(110,'comment',edited=1501)]
        self.api.source.append(uid(5))
        value,state=self.check(opts)
        self.assertEqual(value['outcome'],'dirty')
        self.assertIn(uid(110),{e['id'] for e in state['events']})
        self.api.comments[uid(40)]=[row(111,'comment')]
        value,state=self.check({**opts,'last_checked_ts':value['advance_to']},now=2001)
        self.assertIn(uid(111),{e['id'] for e in state['events']})

    def test_single_database_alias_and_multiple_sources_refused(self):
        opts={'type':'notion_data_source','database_id':uid(91)}
        self.baseline(opts)
        def multiple(method,path,body=None,timeout=35):
            if '/databases/' in path:return {'data_sources':[{'id':uid(90)},{'id':uid(92)}]}
            return self.api(method,path,body,timeout)
        with self.assertRaises(notion.Misconfig):self.check({**opts,'watch_id':'watch-other'},api=multiple)

    def test_resumes_under_one_request_budget_across_process_restarts(self):
        opts={'max_requests':1}
        for _ in range(30):
            before=len(self.api.calls)
            value,state=self.check(opts,clear=False)
            self.assertLessEqual(len(self.api.calls)-before,1)
            if state['coverage']['status']=='complete':break
            self.assertEqual(value['outcome'],'clean')
            self.assertEqual(float(value['advance_to']),float(self.entry['last_checked_ts']))
        else:self.fail('bounded scan did not make progress')
        self.assertIn('block:'+uid(21),state['snapshots'])

    def test_page_size_one_pagination_covers_comments_and_rows(self):
        self.api.page_size=1
        self.api.comments[uid(10)].extend(row(i,'comment') for i in range(120,124))
        state=self.baseline()
        self.assertEqual(len([k for k in state['snapshots'] if k.startswith('comment:')]),5)
        self.assertTrue(any('start_cursor=' in p for _,p,_ in self.api.calls))

    def test_pending_changes_survive_retry_and_clear_only_after_ack(self):
        self.baseline()
        self.api.comments[uid(10)].append(row(130,'comment'))
        first,state=self.check(now=2001)
        second,state=self.check(now=2002)
        self.assertEqual(first['advance_to'],second['advance_to'])
        self.assertEqual(second['count'],1)
        acknowledged,state=self.check({'last_checked_ts':first['advance_to']},now=2003)
        self.assertEqual(acknowledged['outcome'],'clean')
        self.assertEqual(state['events'],[])

    def test_old_ack_does_not_erase_a_new_edit(self):
        self.baseline()
        self.api.comments[uid(10)][0]['rich_text']=[{'plain_text':'A'}]
        first,_=self.check(now=2001)
        self.api.comments[uid(10)][0]['rich_text']=[{'plain_text':'B'}]
        second,state=self.check(now=2002)
        self.assertGreater(float(second['advance_to']),float(first['advance_to']))
        third,state=self.check({'last_checked_ts':first['advance_to']},now=2003)
        self.assertEqual(third['count'],1)
        self.assertEqual(third['advance_to'],second['advance_to'])

    def test_partial_scan_can_deliver_durable_events_without_claiming_full_coverage(self):
        self.baseline()
        self.api.comments[uid(1)]=[row(140,'comment')]
        value,state=self.check({'max_requests':2},now=2001)
        self.assertEqual(value['outcome'],'dirty')
        self.assertTrue(value['complete'])  # inbox prefix covered; tree still scanning
        self.assertEqual(state['coverage']['status'],'scanning')
        self.assertGreater(state['coverage']['pending_tasks'],0)

    def test_interrupted_block_enrollment_retries_content_and_descendant_comments(self):
        self.baseline()
        self.api.children[uid(1)][1]['toggle']['rich_text']=[{'plain_text':'edited'}]
        self.api.comments[uid(21)]=[row(151,'comment')]
        original=tree.Collector.emit_change
        def bounded(collector,key,*args,**kwargs):
            if key=='block:'+uid(20):raise tree.Limited('pending change limit reached')
            return original(collector,key,*args,**kwargs)
        with mock.patch.object(tree.Collector,'emit_change',bounded):
            value,state=self.check()
        self.assertEqual(state['coverage']['status'],'limited')
        self.assertNotIn('node:'+uid(20),state['visits'])
        value,state=self.check()
        self.assertEqual(state['coverage']['status'],'complete')
        self.assertTrue({uid(20),uid(151)}.issubset({e['id'] for e in state['events']}))

    def test_limits_are_visible_and_leave_cursor_held(self):
        value,state=self.check({'max_entities':1})
        self.assertEqual(value['outcome'],'hold')
        self.assertEqual(state['coverage']['status'],'limited')
        self.assertIn('max_entities',state['coverage']['reason'])
        self.assertIsNone(state['coverage']['last_complete_scan'])

    def test_incomplete_source_never_claims_full_coverage(self):
        self.api.incomplete=True
        value,state=self.check({'type':'notion_data_source','data_source_id':uid(90)})
        self.assertEqual(value['outcome'],'hold')
        self.assertEqual(state['coverage']['status'],'limited')
        self.assertNotIn('advance_to',value)

    def test_repeated_cursor_fails_without_discarding_prior_events(self):
        self.api.page_size=1;self.api.repeated_cursor=True
        with self.assertRaises(RuntimeError):self.check()
        with contextlib.closing(Store(self.root)) as store:
            state=store.load(watch_key(self.entry,'notion_page',tree.options(self.entry,'notion_page')))
        self.assertEqual(state['coverage']['status'],'error')
        self.assertGreater(len(state['queue']),0)

    def test_overlapping_watches_share_api_cache_but_not_acknowledgements(self):
        self.baseline()
        before=len(self.api.calls)
        value,other=self.check({'watch_id':'watch-other'},clear=False)
        self.assertEqual(len(self.api.calls),before)
        self.assertGreater(other['coverage']['cache_hits_this_tick'],0)
        self.api.comments[uid(10)].append(row(141,'comment'))
        first,_=self.check(now=2001)
        second,_=self.check({'watch_id':'watch-other'},now=2001,clear=False)
        self.assertEqual(first['count'],second['count'])
        self.check({'last_checked_ts':first['advance_to']},now=2002)
        still_pending,_=self.check({'watch_id':'watch-other'},now=2002)
        self.assertEqual(still_pending['outcome'],'dirty')

    def test_invalid_options_before_api_or_store(self):
        for bad in [{'subpage_depth':-1},{'subpage_depth':True},{'include_subpage_comments':'false'},
                    {'scan_seconds':0},{'max_requests':51},{'max_entities':0},{'page_id':'--help'},
                    {'last_checked_ts':'nan'}]:
            with self.assertRaises((notion.Misconfig,tree.ConfigError)):
                self.check(bad)
        self.assertEqual(self.api.calls,[])

    def test_links_and_child_databases_do_not_expand_scope(self):
        self.api.children[uid(1)].extend([row(80,'link_to_page'),row(81,'child_database')])
        self.baseline()
        self.assertFalse(any('/pages/'+uid(80) in p or '/databases/'+uid(81) in p for _,p,_ in self.api.calls))

    def test_private_state_and_read_only_inspector(self):
        self.baseline()
        self.assertEqual(self.root.stat().st_mode&0o777,0o700)
        self.assertEqual((self.root/'collector.sqlite3').stat().st_mode&0o777,0o600)
        spec=importlib.util.spec_from_file_location('notion_state_inspector',TRIAGE/'surfaces/notion-state.py')
        inspector=importlib.util.module_from_spec(spec);spec.loader.exec_module(inspector)
        report=inspector.inspect(self.entry,self.root)
        self.assertEqual(report['coverage']['status'],'complete')
        self.assertEqual(report['changes'],[])
        absent=self.root/'absent'
        with self.assertRaises(ValueError):inspector.inspect(self.entry,absent)
        self.assertEqual(inspector.inspect(self.entry,absent,strict=False)['coverage']['status'],'not_started')
        with self.assertRaises(ValueError):inspector.inspect({**self.entry,'watch_id':'watch-mismatch'},self.root)
        with mock.patch.dict(os.environ,{'NOTION_KEYCHAIN_SERVICE':'different-credential'}):
            with self.assertRaises(ValueError):inspector.inspect(self.entry,self.root)
        self.assertFalse(absent.exists())

    def test_transient_preserves_progress_and_global_retry_after(self):
        self.pacing.stop()
        exc=notion.Transient('Notion HTTP 429 retry_after=120');exc.retry_after=120
        def fail(*args,**kwargs):raise exc
        with self.assertRaises(notion.Transient):self.check(api=fail)
        with contextlib.closing(Store(self.root)) as store:
            import time
            self.assertGreater(store.not_before(credential_key()),time.time()+115)
            state=store.load(watch_key(self.entry,'notion_page',tree.options(self.entry,'notion_page')))
        self.assertEqual(state['coverage']['status'],'retrying')
        self.assertGreater(len(state['queue']),0)


if __name__=='__main__':unittest.main()
