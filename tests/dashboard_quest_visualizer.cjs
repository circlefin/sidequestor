const assert=require('node:assert/strict'),path=require('node:path');
const filename=path.resolve(__dirname,'../src/sidequestor/runtime/dashboard-quest-visualizer.js');
const api=require(filename);
const start=Date.UTC(2026,0,1),day=86400000;
const fixture={title:'Private project',coverage:{timeline:3,triggers:1,dispatches:1},watches:[{watch_id:'w1',type:'slack_thread',channel_id:'C123PRIVATE',thread_ts:'1760000000.000001',reason:'Private customer'}],events:[
 {event_id:'trigger',ts:start,event:'watch_fired',watch_id:'w1',watch_type:'slack_thread',dispatch_id:'run1',evidence_source:'triage run log'},
 {event_id:'worker',ts:start+1000,event:'dispatch_started',parent_event_ids:['trigger'],dispatch_id:'run1'},
 {event_id:'sent',ts:start+day*80,event:'message_sent',channel_id:'C123PRIVATE',thread_ts:'1760000000.000001',source_file_id:'FILE-PRIVATE',note:'Private message body',associated_dispatch_id:'run1'},
 {event_id:'reply',ts:start+day*81,event:'info_received',permalink:'https://example.slack.com/archives/C123PRIVATE/p1760000000000001',channel_id:'C123PRIVATE',thread_ts:'1760000000.000001',parent_event_id:'sent'},
 {event_id:'bad',ts:1e30,event:'note',note:'unknown time'}]};
let model=api.normalize(fixture),g=api.graph(model);
assert.ok(!g.nodes.some(n=>['Action','Worker'].includes(n.kind)));
assert.equal(model.edges.length,2);assert.equal(g.unpositioned,1);
assert.ok(g.nodes.some(n=>n.type==='Slack channel'));assert.ok(g.nodes.some(n=>n.type==='Slack thread'));assert.ok(g.nodes.some(n=>n.kind==='Resource'));
assert.ok(g.nodes.some(n=>n.id==='watch:w1'),'every watch has its own rule node');
let early=api.snapshot(g,start);assert.equal(early.completed,1);assert.ok(!early.recent.has('google:file:FILE-PRIVATE'));
let late=api.snapshot(g,start+day*81);assert.equal(late.completed,4);assert.ok(late.recent.has('google:file:FILE-PRIVATE'));
assert.equal(api.snapshot(g,start+day*80).recent.get('slack:channel:C123PRIVATE').event.activity,'action');
assert.equal(api.activity('info_logged'),'information');
const typed=api.graph(api.normalize({events:[
 {id:'a',event:'info_logged',ts:start,repo:'one/repo',pr:12,pr_title:'Fix issue',url:'https://github.com/one/repo/pull/12'},
 {id:'b',event:'info_logged',ts:start+1,repo:'two/repo',pr:12},
 {id:'c',event:'doc_cloned',ts:start+2,source_file_id:'source123',new_file_id:'target123',doc_id:'source123'},
 {id:'d',event:'info_logged',ts:start+3,jira:['ABC-123','XYZ-9'],file_id:'ambiguous123',approval_id:'approval1',item_id:'item1'},
 {id:'e',event:'info_logged',ts:start+4,channel_id:'C12345678',thread_ts:'1760000000.000001',permalink:'https://example.slack.com/archives/C12345678/p1760000000000001'},
 {id:'f',event:'info_logged',ts:start+5,note:'Nothing identified'}]}));
assert.equal(typed.nodes.filter(n=>n.id==='github:item:one/repo:12').length,1);
assert.ok(typed.nodes.some(n=>n.id==='github:item:two/repo:12'));
assert.equal(typed.nodes.find(n=>n.id==='google:file:source123').type,'Google Doc');
assert.ok(typed.nodes.some(n=>n.id==='google:file:target123'));
assert.ok(typed.nodes.some(n=>n.id==='jira:ABC-123'));assert.ok(typed.nodes.some(n=>n.id==='jira:XYZ-9'));
assert.ok(!typed.nodes.some(n=>/ambiguous123|approval1|item1/.test(n.id)));
assert.equal(typed.events.at(-1).nodeIds.length,0,'unbound information remains an event, not a fabricated entity');
assert.equal(typed.nodes.find(n=>n.type==='Slack thread').url,'https://example.slack.com/archives/C12345678/p1760000000000001');
assert.equal(api.safeLink('https://user:password@example.com/'),null);
const batch=api.graph(api.normalize({events:[{id:'t1',ts:start,event:'watch_fired',watch_id:'w1',dispatch_id:'r'},{id:'t2',ts:start,event:'watch_fired',watch_id:'w2',dispatch_id:'r'},{id:'d',ts:start+1,event:'dispatch_started',dispatch_id:'r',parent_event_ids:['t1','t2']}]}));assert.equal(batch.links.length,0,'co-triggers do not cause each other');assert.equal(batch.nodes.length,2,'unresolved historical rules remain explicit watches without invented resource targets');
const careful=api.graph(api.normalize({watches:[{watch_id:'a',type:'slack_channel',channel_id:'C11111111'},{watch_id:'b',type:'slack_channel',channel_id:'C22222222'}],events:[
 {id:'ta',ts:start,event:'watch_fired',watch_id:'a',dispatch_id:'run'},
 {id:'tb',ts:start,event:'watch_fired',watch_id:'b',dispatch_id:'run'},
 {id:'receipt',ts:start+5,event:'dispatch_receipt',watch_id:'a',dispatch_id:'run'},
 {id:'other',ts:start+6,event:'info_received',channel_id:'C11111111',thread_ts:'1760000000.1',permalink:'https://example.slack.com/archives/C22222222/p1760000000000001'},
 {id:'same',ts:start+7,event:'info_logged',channel_id:'C11111111',thread_ts:'1760000000.100000'},
 {id:'sheet',ts:start+8,event:'info_logged',source_file_id:'sheet123',url:'https://docs.google.com/spreadsheets/d/sheet123/edit'},
 {id:'drive',ts:start+9,event:'info_logged',url:'https://drive.google.com/open?id=drive123'},
 {id:'mention',ts:start+10,event:'info_logged',note:'https://github.com/example/mentioned'},
 {id:'reserved',ts:start+11,event:'info_logged',url:'https://github.com/settings/tokens'}]}));
assert.deepEqual(careful.eventById.get('receipt').nodeIds,['watch:a']);
assert.equal(careful.nodes.filter(n=>n.type==='Slack thread').length,1,'thread timestamps canonicalize and other-channel URLs do not invent threads');
assert.equal(careful.nodes.find(n=>n.id==='google:file:sheet123').type,'Google Sheet');
assert.equal(careful.nodes.find(n=>n.id==='google:file:sheet123').url,'https://docs.google.com/spreadsheets/d/sheet123/edit');
assert.equal(careful.nodes.find(n=>n.id==='google:file:drive123').url,'https://drive.google.com/open?id=drive123');
assert.equal(careful.eventById.get('mention').nodeIds.length,0);assert.equal(careful.eventById.get('reserved').nodeIds.length,0);
const baseline=api.graph(api.normalize({watches:[
 {watch_id:'old',type:'slack_channel',channel_id:'C33333333',created_ts:(start-1000)/1000},
 {watch_id:'unknown',type:'slack_channel',channel_id:'C44444444'},
 {watch_id:'future',type:'slack_thread',channel_id:'C33333333',thread_ts:'1760000000.000001',created_ts:(start+day)/1000}],
 historical_watches:[{watch_id:'historic',historical:true,type:'slack_channel',channel_id:'C55555555',observed_at:new Date(start+day).toISOString()}],
 events:[{id:'creation',event:'created',ts:start},{id:'later',event:'note',ts:start+2*day}]}));
const initial=api.snapshot(baseline,start);
assert.equal(initial.watched.get('watch:old'),'active');
assert.equal(initial.watched.get('watch:unknown'),'current');
assert.equal(initial.watched.get('watch:future'),'later');
assert.equal(initial.watched.get('watch:historic'),'unknown');
assert.equal(initial.recent.size,0,'configuration must not fabricate activity');
assert.equal(api.snapshot(baseline,start+2*day).watched.get('watch:historic'),'historical');
assert.equal(baseline.nodes.filter(n=>n.kind==='Watch').length,4);
// Every shipped checker has a visible, categorized rule with retained criteria.
const fs=require('node:fs'),checkerDir=path.resolve(__dirname,'../src/sidequestor/runtime/yaas-triage/checkers');
const shipped=fs.readdirSync(checkerDir).filter(f=>f.endsWith('.watch.json')).map(f=>[f.replace('.watch.json',''),JSON.parse(fs.readFileSync(path.join(checkerDir,f),'utf8'))]);
const allWatchFixture=shipped.map(([type,shape],i)=>({...shape.checker_example,watch_id:'schema-'+i}));
const allWatchGraph=api.graph(api.normalize({watches:allWatchFixture,events:[]}));
assert.equal(allWatchGraph.nodes.filter(n=>n.kind==='Watch').length,shipped.length);
assert.ok(allWatchGraph.nodes.filter(n=>n.kind==='Watch').every(n=>n.supported),'all bundled checker types must have a recognized category');
for(const w of allWatchFixture){const n=allWatchGraph.nodes.find(n=>n.id==='watch:'+w.watch_id);assert.equal(n.category,api.watchCategory(w));for(const k of ['query','jql','cron','tz'])if(w[k])assert.equal(n.detail[k],String(w[k]))}
assert.equal(api.watchCategory({type:'schedule',cron:'0 9 * * *'}),'cron');assert.equal(api.watchCategory({type:'schedule',next_fire_ts:123}),'scheduled');
const services=api.graph(api.normalize({watches:[{watch_id:'mail',type:'email',query:'from:person@example.test'}],events:[
 {id:'mail-1',ts:start,event:'email_received',surface:'gmail',thread_id:'thread123',message_id:'message1',subject:'Release planning',link_url:'https://mail.google.com/mail/u/1/#all/thread123'},
 {id:'mail-2',ts:start+1,event:'email_sent',surface:'gmail',thread_id:'thread123',message_id:'message2'},
 {id:'review',ts:start+2,event:'draft_posted',approval_id:'review-1',surface:'gmail',thread_id:'thread123'},
 {id:'execute',ts:start+3,event:'executed',approval_id:'review-1'},
 {id:'telegram',ts:start+4,event:'message_sent',provider:'telegram',chat_id:'-100123456789',channel_id:'-100123456789',peer_title:'Team chat',link_url:'https://t.me/c/123456789/10'},
 {id:'notion',ts:start+5,event:'info_logged',provider:'notion',page_id:'12345678-1234-1234-1234-123456789abc',page_title:'Launch notes',url:'https://www.notion.so/Launch-notes-12345678123412341234123456789abc'},
 {id:'x',ts:start+6,event:'info_received',provider:'x',post_id:'123456789',url:'https://x.com/example/status/123456789'},
 {id:'ambiguous',ts:start+7,event:'info_logged',thread_id:'unknown-provider',message_id:'unknown-message'},
 {id:'coda',ts:start+8,event:'updated',surface:'coda',doc_id:'coda123',url:'https://coda.io/d/Plan_dcoda123'}]}));
assert.equal(services.nodes.filter(n=>n.type==='Email conversation').length,1,'email replies share the conversation');
assert.equal(services.nodes.find(n=>n.type==='Email conversation').url,'https://mail.google.com/mail/u/1/#all/thread123');
assert.equal(services.nodes.find(n=>n.type==='Email conversation').label,'Release planning');
assert.equal(services.eventById.get('execute').subjectAssociation,true);assert.ok(services.eventById.get('execute').nodeIds.some(id=>id.startsWith('gmail:')));
assert.ok(!services.links.some(l=>l.type==='recorded'),'review-ID subject matching must not invent causal edges');
assert.ok(services.nodes.some(n=>n.type==='Telegram chat'&&n.label==='Team chat'));assert.ok(!services.nodes.some(n=>n.id.startsWith('slack:')));
assert.equal(services.nodes.filter(n=>n.type==='Notion page').length,1);assert.equal(services.nodes.find(n=>n.type==='Notion page').label,'Launch notes');
assert.equal(services.nodes.filter(n=>n.type==='X post').length,1);assert.equal(services.eventById.get('ambiguous').nodeIds.length,0);
assert.equal(services.nodes.filter(n=>n.type==='Coda document').length,1);assert.ok(!services.nodes.some(n=>n.id==='google:file:coda123'));
const aliases=api.graph(api.normalize({events:[
 {id:'gmail-alias',ts:start,event:'info_received',gmail_id:'native-gmail-1',subject:'Native mail ID',message_url:'https://mail.google.com/mail/u/0/#all/native-gmail-1'},
 {id:'jira-alias',ts:start,event:'info_logged',ticket:'ABC-123',ticket_url:'https://example.atlassian.net/browse/ABC-123'},
 {id:'nested',ts:start,event:'executed',resource:{surface:'gmail',thread_id:'nested-thread',subject:'Nested email'}}]}));
assert.ok(aliases.nodes.some(n=>n.type==='Email message'&&n.url));assert.ok(aliases.nodes.some(n=>n.id==='jira:ABC-123'));assert.ok(aliases.nodes.some(n=>n.type==='Email conversation'&&n.label==='Nested email'));
const retired=api.graph(api.normalize({historical_watches:[{watch_id:'r',historical:true,type:'slack_channel',channel_id:'C77777777',created_ts:(start-1)/1000,observed_at:new Date(start).toISOString()}],events:[{id:'retire',ts:start+day,event:'watch_retired',watch_id:'r',watch_type:'slack_channel'}]}));
assert.equal(api.snapshot(retired,start).watched.get('watch:r'),'historical','snapshot evidence must not assert continued activity');assert.equal(api.snapshot(retired,start+day).watched.get('watch:r'),'retired');
const futureCause=api.graph(api.normalize({events:[{id:'future',ts:start+1,event:'info_logged',doc_id:'future123'},{id:'past',ts:start,event:'executed',doc_id:'past123',parent_event_id:'future'}]}));assert.equal(futureCause.links.length,0);
const unknown=api.graph(api.normalize({watches:[{watch_id:'custom',type:'some_future_checker',reason:'custom check'}],events:[{ts:start,event:'custom',note:'unbound'}]}));assert.deepEqual(unknown.coverage.unsupported,['some_future_checker']);assert.equal(unknown.coverage.unlinked,1);
const busy=api.graph(api.normalize({watches:Array.from({length:300},(_,i)=>({watch_id:'busy'+i,type:allWatchFixture[i%allWatchFixture.length].type,reason:'Rule '+i})),events:Array.from({length:600},(_,i)=>({id:'busy-e'+i,ts:start+i*day,event:'info_logged',doc_id:'busy-doc'+i}))}));
assert.ok(busy.nodes.length<=api.LIMIT+Object.keys(api.CATEGORIES).length);assert.ok(busy.nodes.filter(n=>n.kind==='Resource').length>=80);assert.ok(busy.nodes.some(n=>n.category==='email'));assert.equal(new Set(busy.nodes.map(n=>n.x+':'+n.y)).size,busy.nodes.length);
const guarded=api.graph(api.normalize({events:[
 {id:'provider',ts:start,event:'email_sent',type:'reply',threadId:'recognized-from-event'},
 {id:'nested-provider',ts:start,event:'email_sent',type:'reply',resource:{thread_id:'inherited-from-event'}},
 {id:'mismatch-mail',ts:start+1,event:'info_logged',provider:'gmail',thread_id:'api-thread',url:'https://mail.google.com/mail/u/0/#all/different-web-thread'},
 {id:'mismatch-telegram',ts:start+2,event:'info_logged',provider:'telegram',peer:'@firstchat',chat_title:'First chat',url:'https://t.me/secondchat'},
 {id:'mismatch-x',ts:start+3,event:'info_logged',provider:'x',user_id:'42',username:'firstuser',url:'https://x.com/seconduser'},
 {id:'not-jira',ts:start+4,event:'info_logged',jira_key:'ABC-12',url:'https://not-jira.example/browse/ABC-12'},
 {id:'receipt-blocked',ts:start+5,event:'dispatch_receipt',outcome:'blocked'}]}));
assert.ok(guarded.nodes.some(n=>n.type==='Email conversation'&&n.detail.identifier==='recognized-from-event'));assert.ok(guarded.nodes.some(n=>n.type==='Email conversation'&&n.detail.identifier==='inherited-from-event'));
assert.equal(guarded.nodes.find(n=>n.type==='Email conversation'&&n.detail.identifier==='api-thread').url,null);assert.ok(guarded.nodes.some(n=>n.type==='Email web conversation'&&n.url));
assert.equal(guarded.nodes.find(n=>n.id.includes('chat:@firstchat')).url,'https://t.me/firstchat');assert.equal(guarded.nodes.find(n=>n.id.includes('chat:@secondchat')).label,'Telegram · @secondchat');
assert.equal(guarded.nodes.find(n=>n.id==='x:user:42').url,null);assert.equal(guarded.nodes.find(n=>n.id==='x:user:seconduser').url,'https://x.com/seconduser');
assert.equal(guarded.nodes.find(n=>n.id==='jira:ABC-12').url,null);assert.equal(guarded.eventById.get('receipt-blocked').activity,'attention');
const moving=api.graph(api.normalize({watches:[{watch_id:'move',type:'slack_channel',channel_id:'CNEW12345',created_ts:(start-1)/1000}],historical_watches:[
 {watch_id:'move',type:'slack_channel',channel_id:'COLD12345',historical:true,observed_at:new Date(start).toISOString()},
 {watch_id:'move',type:'slack_channel',channel_id:'CNEW12345',historical:true,observed_at:new Date(start+day).toISOString()}],events:[
 {id:'old-fire',ts:start+1,event:'watch_fired',watch_id:'move'},
 {id:'new-fire',ts:start+day+1,event:'watch_fired',watch_id:'move'}]}));
assert.deepEqual([...api.configurationTargets(moving.nodes.find(n=>n.id==='watch:move'),start)],['slack:channel:COLD12345']);
assert.ok(moving.eventById.get('old-fire').nodeIds.includes('slack:channel:COLD12345'));assert.ok(!moving.eventById.get('old-fire').nodeIds.includes('slack:channel:CNEW12345'));
assert.ok(moving.eventById.get('new-fire').nodeIds.includes('slack:channel:CNEW12345'));assert.ok(!moving.eventById.get('new-fire').nodeIds.includes('slack:channel:COLD12345'));
const multi=api.graph(api.normalize({events:[{id:'many',ts:start,event:'cloned',source_file_id:'source-many',target_file_id:'target-many'},{id:'after-many',ts:start+1,event:'executed',doc_id:'after-many-doc',parent_event_id:'many'}]}));assert.ok(!multi.links.some(l=>l.type==='recorded'));assert.equal(multi.coverage.ambiguousCausal,1);
const zero=api.graph(api.normalize({watches:[{watch_id:'zero',type:'email',query:'test',created_ts:'0'}]}));assert.equal(api.watchState(zero.nodes[0],start),'current');
assert.ok(busy.nodes.length<=api.LIMIT,'overflow groups must fit within the same node limit');
const priority=api.graph(api.normalize({watches:[{watch_id:'keep-idle',type:'slack_channel',channel_id:'CKEEP1234'}],events:Array.from({length:240},(_,i)=>({id:'priority'+i,ts:start+i,event:'watch_fired',watch_id:'historical'+i,watch_type:'slack_channel',doc_id:'priority-doc'+i}))}));
assert.ok(priority.nodes.some(n=>n.id==='watch:keep-idle'),'current idle rules remain visible ahead of historical rules');assert.ok(priority.nodes.some(n=>n.id==='slack:channel:CKEEP1234'),'current watch targets survive grouping');
const positions=g.nodes.map(n=>[n.id,n.x,n.y]);api.snapshot(g,start);assert.deepEqual(positions,g.nodes.map(n=>[n.id,n.x,n.y]));
for(const mode of ['story','calendar'])for(const p of [0,.1,.5,.99,1])assert.ok(Math.abs(api.progressAt(g.times,api.timeAt(g.times,p,mode),mode)-p)<1e-6);
assert.notEqual(api.timeAt(g.times,.5,'story'),api.timeAt(g.times,.5,'calendar'));
assert.equal(api.instant('2026-01-01T12:00:00'),Date.UTC(2026,0,1,12));
assert.equal(api.instant(1e30),null);assert.equal(api.instant(true),null);assert.equal(api.safeLink('javascript:alert(1)'),null);
assert.equal(api.safeLink('https://example.com/a'),'https://example.com/a');
assert.equal(api.project(model,{start:'2026-01-01'}).undatedExcluded,1);
assert.equal(api.normalize({events:[{id:'same',event:'a'},{id:'same',event:'b'},{id:'c',parent_event_id:'same'}]}).edges.length,0);
const large=api.graph(api.normalize({events:Array.from({length:800},(_,i)=>({event_id:'e'+i,ts:start+i*day,event:'message_sent',doc_id:'document'+i,channel_id:'C'+i,thread_ts:String(i)}))}));
assert.equal(new Set(large.nodes.map(n=>n.x.toFixed(8)+':'+n.y.toFixed(8))).size,large.nodes.length,'dense entities must not stack at identical points');
assert.ok(large.nodes.length<=api.LIMIT);assert.ok(large.grouped>0);assert.ok(large.links.length<=1200);assert.ok(large.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
assert.equal(api.snapshot(large,start+800*day).completed,800);
const empty=api.graph(api.normalize({watches:fixture.watches,events:[]}));assert.equal(api.timeAt(empty.times,1),null);assert.ok(empty.nodes.length>=2);

class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.style={};this.listeners={};this.value='';this.open=false;this.hidden=false;this.text='';this.classes=new Set;this.classList={add:(...s)=>s.forEach(v=>this.classes.add(v)),remove:(...s)=>s.forEach(v=>this.classes.delete(v)),toggle:(s,on)=>{const value=on===undefined?!this.classes.has(s):on;value?this.classes.add(s):this.classes.delete(s);return value},contains:s=>this.classes.has(s)}}
 set className(v){this.classes=new Set(v.split(' '))}get className(){return [...this.classes].join(' ')}
 set textContent(v){this.text=String(v);this.children=[]}get textContent(){return this.text+this.children.map(c=>c.textContent).join('')}
 append(...items){items.forEach(item=>{item.parent=this;this.children.push(item)})}replaceChildren(...items){this.text='';this.children=[];this.append(...items)}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=v}getAttribute(k){return this.attrs[k]}
 addEventListener(k,f){(this.listeners[k]??=[]).push(f)}dispatch(k,extra={}){for(const f of this.listeners[k]||[])f({target:this,preventDefault(){},stopPropagation(){},...extra})}
 showModal(){this.open=true}close(){this.open=false;this.dispatch('close')}
}
const body=new Element('body'),doc={body,hidden:false,createElement:t=>new Element(t),createElementNS:(ns,t)=>new Element(t),addEventListener(){}};
const frames=new Map;let nextFrame=0,requests=[];
global.document=doc;global.fetch=async url=>{requests.push(url);return{ok:true,json:async()=>fixture}};
global.requestAnimationFrame=fn=>{frames.set(++nextFrame,fn);return nextFrame};global.cancelAnimationFrame=id=>frames.delete(id);global.matchMedia=()=>({matches:false});
function all(root){return[root,...root.children.flatMap(all)]}function find(root,p){return all(root).find(p)}
(async()=>{
 await api.open('quest-test');const dialog=body.children[0];assert.ok(!dialog.textContent.includes('WATCH POINTS'));assert.ok(dialog.textContent.includes('WATCHES'));assert.ok(dialog.textContent.includes('RESOURCES'));assert.ok(!dialog.textContent.includes('CHANNELS & THREADS'));assert.ok(dialog.open);assert.equal(requests.length,1);assert.ok(requests[0].endsWith('/visualization'));
 const firstDot=find(dialog,e=>e.tagName==='g'&&e.attrs.role==='button'&&e.classList.contains('qv-kind-resource'));firstDot.dispatch('click');const anchor=find(dialog,e=>e.tagName==='a');assert.ok(anchor&&anchor.href.startsWith('https://'));find(dialog,e=>e.tagName==='button'&&e.textContent==='Close details').dispatch('click');
 const slider=find(dialog,e=>e.tagName==='input'&&e.type==='range');assert.ok(slider);slider.value='0';slider.dispatch('input');
 const clock=find(dialog,e=>e.tagName==='time');assert.ok(clock.textContent.includes('2026'));
 const play=find(dialog,e=>e.tagName==='button'&&e.textContent==='▶ Play');play.dispatch('click');assert.equal(play.textContent,'Ⅱ Pause');assert.equal(frames.size,1);
 for(let i=0;i<3;i++){const pair=frames.entries().next().value;if(pair){frames.delete(pair[0]);pair[1](i*1000)}}
 const privacy=find(dialog,e=>e.tagName==='button'&&e.textContent==='Hide private details');privacy.dispatch('click');assert.ok(!dialog.textContent.includes('Private project'));assert.ok(!dialog.textContent.includes('C123PRIVATE'));assert.ok(!dialog.textContent.includes('FILE-PRIVATE'));assert.ok(!dialog.textContent.includes('Private message body'));
 const dot=find(dialog,e=>e.tagName==='g'&&e.attrs.role==='button');dot.dispatch('click');assert.equal(frames.size,0,'pinning pauses playback so details remain usable');assert.ok(dialog.textContent.includes('Private names, identifiers'));
 assert.ok(!dialog.textContent.includes('Private customer'));const tip=find(dialog,e=>e.tagName==='aside');const pinnedText=tip.textContent;const nextDot=all(dialog).filter(e=>e.tagName==='g'&&e.attrs.role==='button')[1];nextDot.dispatch('focus');assert.equal(tip.textContent,pinnedText,'focus must not replace pinned details');
 dialog.close();assert.equal(frames.size,0);assert.equal(play.textContent,'▶ Play');
 await api.open('quest-test');assert.ok(!dialog.textContent.includes('Private project'),'private display preference survives quest opens');assert.ok(!dialog.textContent.includes('C123PRIVATE'));assert.equal(requests.length,2);assert.equal(frames.size,0);
 global.fetch=async()=>({ok:true,json:async()=>({watches:[
  {watch_id:'idle',type:'email',query:'from:person@example.test'},
  {watch_id:'later',type:'schedule',next_fire_ts:(start+day)/1000,created_ts:(start+day)/1000},
  {watch_id:'retired',type:'health_issue',issue_id:'health-id',retired_at:(start-1)/1000}],
  events:[{id:'baseline',ts:start,event:'created'},{id:'end',ts:start+2*day,event:'note'}]})});
 await api.open('baseline-display');const baselineSlider=find(dialog,e=>e.tagName==='input'&&e.type==='range');baselineSlider.value='0';baselineSlider.dispatch('input');
 const watchDots=all(dialog).filter(e=>e.tagName==='g'&&e.classList.contains('qv-kind-watch'));
 assert.equal(watchDots.find(e=>e.attrs['data-category']==='email').style.opacity,'0.60');
 assert.equal(watchDots.find(e=>e.attrs['data-category']==='scheduled').style.opacity,'0.15');
 assert.equal(watchDots.find(e=>e.attrs['data-category']==='health').style.opacity,'0.25');
 assert.ok(find(dialog,e=>e.tagName==='details'&&e.classList.contains('qv-coverage')));dialog.close();
 console.log('Quest replay graph, scrubber, privacy and lifecycle checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
