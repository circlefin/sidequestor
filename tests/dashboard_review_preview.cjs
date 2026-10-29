// Exercise the shipped dashboard functions without adding a DOM dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('src/sidequestor/runtime/dashboard.html', 'utf8');
function declaration(name) {
  const lines = html.split('\n');
  const index = lines.findIndex(line => line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`));
  assert.ok(index >= 0, name);
  if (lines[index].endsWith('}')) return lines[index];
  const end = lines.findIndex((line, i) => i > index && line === '}');
  return lines.slice(index, end + 1).join('\n');
}
const definitions = ['esc', 'ago', 'safeUrl'].map(name => html.split('\n').find(line => line.startsWith(`const ${name}=`)));
const names = ['mdInline', 'markdown', 'reviewPreview', 'setReviewMode', 'approvalCard', 'targetText', 'reviewDestination', 'reviewMessageCopy', 'reviewAlert', 'conversation', 'actionNoun', 'pill', 'actionLabel', 'setValue', 'captureUi', 'captureFocus', 'restoreUi', 'approvalAction', 'updateCheckMessage', 'renderSubhead', 'briefingPrompt', 'briefingEmptyState', 'createBriefingQuest', 'renderBriefings'];
const context = {assert, URL, console};
vm.createContext(context);
vm.runInContext(definitions.concat(names.map(declaration)).join('\n') + String.raw`
(async () => {
  globalThis.location={href:'https://dashboard.example/'};
  assert.ok(updateCheckMessage({status:'available',update:{latest:'0.1.54'}}).includes('0.1.54'));
  assert.ok(updateCheckMessage({status:'current'}).includes('latest stable'));
  assert.ok(updateCheckMessage({status:'rate_limited',retry_after:42}).includes('42s'));
  assert.ok(updateCheckMessage({status:'failed'}).includes('failed'));
  globalThis.watchTime=()=>'';
  globalThis.restoreFocus=()=>{};
  globalThis.CSS={escape:x=>x};
  const subhead={innerHTML:''};
  globalThis.$=()=>subhead;
  globalThis.state={control:{attention:[{id:'worker-readiness:slack',detail:'Claude Slack MCP needs reconnection. Run sq setup.'}]}};
  renderSubhead('live',null,{});
  assert.ok(subhead.innerHTML.includes('run <code>sq --workspace . setup</code> from this workspace’s folder'));
  state.control.workspace={path:"/Users/me/new workspace's folder"};
  renderSubhead('live',null,{});
  assert.ok(subhead.innerHTML.includes(esc("sq --workspace '/Users/me/new workspace'\"'\"'s folder' setup")));
  assert.ok(subhead.innerHTML.includes('from any folder'));
  const original='## Proposal\n\n**Read this**\n\n- First\n- Second';
  const spec={id:'a',quest_id:'q',action_type:'file_edit',message_text:original,context:'## Why\n\nA reason',available_actions:['review','revise']};
  const markup=approvalCard(spec);
  for(const [mode,label] of Object.entries({headless:'Agent repair proposed',interactive:'Setup or sign-in needed',uncertain:'Cause needs investigation'})){
    assert.ok(approvalCard({...spec,action_type:'remote_request',target:{action:'health_repair',repair_mode:mode}}).includes(label));
  }
  assert.ok(approvalCard({...spec,action_type:'remote_request',target:{action:'health_repair',repair_mode:'uncertain'}}).includes('Approval queues an agent check'));
  assert.ok(approvalCard({...spec,action_type:'remote_request',target:{action:'sidequestor_upgrade',version:'9.0'}}).includes('Independent upgrade after approval'));
  assert.ok(markup.includes('data-review-mode="preview"'));
  assert.ok(markup.includes('<h2>Proposal</h2>'));
  assert.ok(markup.includes('<h2>Why</h2>'));
  assert.ok(markup.includes('source" hidden'));
  assert.ok(approvalCard({...spec,available_actions:[]}).includes('View source'));
  assert.ok(approvalCard({...spec,action_type:'slack_message'}).includes('sent using the source text'));
  // The approval pins one file's bytes, so the reviewer must see which file it is.
  const upload=approvalCard({id:'u',quest_id:'q',action_type:'remote_request',message_text:'Q3 numbers',available_actions:['review'],
    target:{surface:'slack',action:'file_upload',channel_id:'C1',thread_ts:'1.2',file:'/Users/me/q3<b>.pdf',filename:'q3<b>.pdf',size:2048,sha256:'ab'}});
  assert.ok(upload.includes('q3&lt;b&gt;.pdf (2048 bytes)'), upload);
  assert.ok(upload.includes('/Users/me/q3&lt;b&gt;.pdf'), upload);
  assert.ok(upload.includes('C1 · thread 1.2'), upload);
  // A reply approval shows the resolved To/Cc audience, including original recipients.
  const reply=approvalCard({id:'r',quest_id:'q',action_type:'remote_request',message_text:'Deck attached',available_actions:['review'],
    target:{surface:'gmail',action:'reply_all',message_id:'m-9',cc:['new@example.com'],recipients:{to:['alice@example.com'],cc:['bob@example.com','new@example.com']},remove:['old@example.com'],attachments:[{path:'/Users/me/deck.pdf',size:9,sha256:'ab'}],html:false}});
  assert.ok(reply.includes('Gmail reply-all'), reply);
  assert.ok(reply.includes('message m-9 · to alice@example.com · cc bob@example.com · cc new@example.com · remove old@example.com · attach /Users/me/deck.pdf (9 bytes)'), reply);
  assert.ok(reviewPreview('').includes('No proposed content.'));
  assert.ok(reviewPreview('plain text').includes('<p>plain text</p>'));
  assert.ok(!reviewPreview('<img src=x onerror=alert(1)>').includes('<img'));
  assert.ok(!reviewPreview('[bad](javascript:alert(1))').includes('href='));
  assert.ok(reviewPreview('*Slack bold* and ~removed~ <https://example.com|source>').includes('<strong>Slack bold</strong>'));
  assert.ok(reviewPreview('*Slack bold* and ~removed~ <https://example.com|source>').includes('<del>removed</del>'));
  assert.ok(reviewPreview('~~~\n  indented <script>\n~~~').includes('  indented &lt;script&gt;'));
  const sourceUrl='https://github.com/smathison_crcl/solutions-engineering/issues/346';
  const issueRepo='smathison_crcl/solutions-engineering';
  const screenshotCard=approvalCard({...spec,target:{repo:issueRepo,issue:346},
    context:'Issue #346 ('+sourceUrl+') was filed.',message_text:'Review #346 separately from #345.',
    risk_reason:'See '+sourceUrl});
  assert.ok(screenshotCard.includes('href="'+sourceUrl+'"'));
  assert.ok(screenshotCard.includes('href="https://github.com/'+issueRepo+'/issues/345"'));
  assert.ok(reviewPreview('[Issue #346]('+sourceUrl+')').includes('>Issue #346</a>'));
  assert.ok(!reviewPreview('No known repository: #345').includes('href='));
  const backtick=String.fromCharCode(96);
  assert.ok(!reviewPreview(backtick+sourceUrl+backtick).includes('href='));
  assert.ok(!reviewPreview(backtick+'#345'+backtick,issueRepo).includes('href='));
  assert.ok(reviewPreview('See ('+sourceUrl+').').includes('>'+sourceUrl+'</a>).'));
  assert.ok(reviewPreview('See https://example.com/page_(section).').includes('href="https://example.com/page_(section)"'));
  const query=reviewPreview('https://example.com/?a=1&b=2');
  assert.ok(query.includes('href="https://example.com/?a=1&amp;b=2"'));
  assert.ok(!query.includes('&amp;amp;'));
  assert.ok(reviewPreview('[Source](https://example.com/?a=1&b=2)').includes('href="https://example.com/?a=1&amp;b=2"'));
  // Briefings use markdown() directly: raw link labels and URLs escape exactly once.
  for(const [label,escaped] of [["Ant's",'Ant&#39;s'],['R&D','R&amp;D'],['"quoted"','&quot;quoted&quot;'],['&amp;','&amp;amp;'],['<img src=x onerror=alert(1)>','&lt;img src=x onerror=alert(1)&gt;']]){
    const source='['+label+'](https://example.com/)';
    const anchor='<a href="https://example.com/" target="_blank" rel="noopener">'+escaped+'</a>';
    assert.equal(mdInline(source),anchor);
    assert.equal(markdown(source),'<p>'+anchor+'</p>');
  }
  assert.equal(mdInline("Ant's"),'Ant&#39;s');
  assert.equal(mdInline('[Query](https://example.com/?q=one&two=2)'),'<a href="https://example.com/?q=one&amp;two=2" target="_blank" rel="noopener">Query</a>');
  assert.ok(!markdown('[bad](javascript:alert(1))').includes('href='));
  assert.ok(!markdown('[bad](data:text/html,<svg/onload=alert(1)>)').includes('href='));
  assert.equal(mdInline(backtick+"[Ant's](https://example.com/)"+backtick),'<code>[Ant&#39;s](https://example.com/)</code>');
  assert.ok(!reviewPreview('<img onerror=alert(1)> [bad](javascript:alert(1))').includes('<img'));
  assert.ok(!reviewPreview('[bad](javascript:alert(1))').includes('href='));
  assert.equal((reviewPreview('[Source]('+sourceUrl+')').match(/<a /g)||[]).length,1);
  assert.ok(!reviewPreview('#345','../../evil').includes('href='));

  const onboarding=briefingEmptyState();
  assert.ok(onboarding.includes('Your first briefing starts with a quest.'));
  assert.ok(onboarding.includes('data-briefing-create="daily"'));
  assert.ok(onboarding.includes('data-briefing-create="once"'));
  assert.ok(briefingPrompt('daily').includes('Confirm my sources and timezone'));
  assert.ok(briefingPrompt('once').includes('state/briefs/'));
  assert.ok(briefingPrompt('once').includes('do not post or email'));
  const briefingElements={};let dialogOpened=false,focused=false;
  globalThis.$=s=>briefingElements[s]||(briefingElements[s]={value:'',textContent:'',showModal(){dialogOpened=true},focus(){focused=true}});
  createBriefingQuest('daily');
  assert.ok(dialogOpened&&focused);
  assert.equal(briefingElements['#quest-prompt'].value,briefingPrompt('daily'));
  assert.equal(briefingElements['#quest-priority'].value,'normal');
  globalThis.state={briefs:[],briefsLoading:false,briefsError:''};
  renderBriefings();
  assert.equal(briefingElements['#brief-reader'].htmlOnce,onboarding);
  state.briefsError='Request failed';renderBriefings();
  assert.ok(!briefingElements['#brief-reader'].htmlOnce.includes('Create a morning'));

  function makeCard(readOnly=false) {
    const field={value:original,hidden:true,readOnly,scrollTop:0};
    const preview={hidden:false,htmlOnce:''};
    const toggle={textContent:'',setAttribute(k,v){this[k]=v}};
    const note={value:'',scrollTop:0};
    const status={};
    return {dataset:{card:'a',reviewMode:'preview'},field,preview,toggle,note,
      querySelector(s){return {'[data-message]':field,'[data-message-preview]':preview,'[data-review-mode]':toggle,'[data-note]':note,'[data-card-status]':status}[s]||null}};
  }
  let card=makeCard();
  setReviewMode(card,true);
  assert.equal(card.field.hidden,false);
  assert.equal(card.preview.hidden,true);
  assert.equal(card.toggle['aria-pressed'],'true');
  const edited='## Updated\n\n- Unsaved';
  card.field.value=edited;
  setReviewMode(card,false);
  assert.ok(card.preview.htmlOnce.includes('<h2>Updated</h2>'));
  assert.equal(card.field.value,edited);
  assert.equal(card.toggle.textContent,'Edit');
  card.dataset.reviewRepo=issueRepo;card.field.value='Review #345 and '+sourceUrl;
  setReviewMode(card,false);
  assert.ok(card.preview.htmlOnce.includes('href="https://github.com/'+issueRepo+'/issues/345"'));
  assert.equal(card.field.value,'Review #345 and '+sourceUrl); // Render only; never rewrite the approved body.
  card.field.value=edited;
  const readonly=makeCard(true);
  setReviewMode(readonly,false);
  assert.equal(readonly.toggle.textContent,'View source');
  assert.equal(readonly.field.readOnly,true);

  // Simulate a poll rebuilding the DOM from the stale server draft.
  globalThis.state={selected:null};
  globalThis.document={activeElement:null};
  const surfaces={};
  globalThis.$=s=>surfaces[s]||(surfaces[s]={scrollTop:0,classList:{contains:()=>false}});
  globalThis.$$=s=>s==='[data-card]' || s==='[data-card="a"]'?[card]:[];
  for(const source of [false,true]) {
    setReviewMode(card,source);
    card.field.scrollTop=48;
    card.note.value='Keep this note';
    const saved=captureUi();
    card=makeCard();
    restoreUi(saved);
    assert.equal(card.field.value,edited);
    assert.equal(card.note.value,'Keep this note');
    assert.equal(card.field.scrollTop,48);
    assert.equal(card.dataset.reviewMode,source?'source':'preview');
    assert.ok(card.preview.htmlOnce.includes('<h2>Updated</h2>'));
  }
  let sent;
  globalThis.post=async (path,payload)=>{sent={path,payload};return {}};
  globalThis.closeReviewDialog=()=>{};
  globalThis.toast=()=>{};
  globalThis.poll=async()=>{};
  card.note.value='';
  card.field.value='  '+edited+'  ';
  setReviewMode(card,false);
  await approvalAction({closest:()=>card,dataset:{approvalAction:'review'}});
  assert.equal(sent.path,'/api/review/a');
  assert.equal(sent.payload.message_text,edited); // Existing trim behavior.
  assert.ok(!sent.payload.message_text.includes('<h2>'));
  card.field.value='  ';
  await approvalAction({closest:()=>card,dataset:{approvalAction:'review'}});
  assert.equal(Object.hasOwn(sent.payload,'message_text'),false);
})().catch(error=>{console.error(error);globalThis.failed=true});
`, context);
// Async approvalAction runs through a few resolved promises.
setImmediate(() => { if (context.failed) process.exitCode = 1; });
