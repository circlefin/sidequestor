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
const names = ['mdInline', 'markdown', 'reviewPreview', 'setReviewMode', 'approvalCard', 'reviewDestination', 'reviewMessageCopy', 'reviewAlert', 'conversation', 'actionNoun', 'pill', 'actionLabel', 'setValue', 'captureUi', 'captureFocus', 'restoreUi', 'approvalAction'];
const context = {assert, URL, console};
vm.createContext(context);
vm.runInContext(definitions.concat(names.map(declaration)).join('\n') + String.raw`
(async () => {
  globalThis.location={href:'https://dashboard.example/'};
  globalThis.watchTime=()=>'';
  globalThis.restoreFocus=()=>{};
  globalThis.CSS={escape:x=>x};
  const original='## Proposal\n\n**Read this**\n\n- First\n- Second';
  const spec={id:'a',quest_id:'q',action_type:'file_edit',message_text:original,context:'## Why\n\nA reason',available_actions:['review','revise']};
  const markup=approvalCard(spec);
  assert.ok(markup.includes('data-review-mode="preview"'));
  assert.ok(markup.includes('<h2>Proposal</h2>'));
  assert.ok(markup.includes('<h2>Why</h2>'));
  assert.ok(markup.includes('source" hidden'));
  assert.ok(approvalCard({...spec,available_actions:[]}).includes('View source'));
  assert.ok(approvalCard({...spec,action_type:'slack_message'}).includes('sent using the source text'));
  assert.ok(reviewPreview('').includes('No proposed content.'));
  assert.ok(reviewPreview('plain text').includes('<p>plain text</p>'));
  assert.ok(!reviewPreview('<img src=x onerror=alert(1)>').includes('<img'));
  assert.ok(!reviewPreview('[bad](javascript:alert(1))').includes('href='));
  assert.ok(reviewPreview('*Slack bold* and ~removed~ <https://example.com|source>').includes('<strong>Slack bold</strong>'));
  assert.ok(reviewPreview('*Slack bold* and ~removed~ <https://example.com|source>').includes('<del>removed</del>'));
  assert.ok(reviewPreview('~~~\n  indented <script>\n~~~').includes('  indented &lt;script&gt;'));

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
