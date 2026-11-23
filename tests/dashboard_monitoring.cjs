// Exercise inactive status as presentation only: no connector toggles or writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('src/sidequestor/runtime/dashboard.html','utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const lines=html.split('\n'),names=['watchRow','questGroup','loops','renderQuestList','reviewMessageCopy','appPrompt','appUrl'];
const definitions=names.map(n=>lines.find(l=>l.startsWith(`function ${n}(`)||l.includes(`}function ${n}(`)));
assert.ok(definitions.every(Boolean));
const list={htmlOnce:''},ctx={URL,URLSearchParams,state:{control:{quests:[],queued:[],attention:[]}},$:()=>list,watchLabel:w=>w.type,watchTarget:()=>'<private>',watchLink:()=>null,watchFilters:()=>'',notionCoverage:()=>'',watchAgeMarkup:()=>'',watchTime:()=>'',pill:(text)=>`[${text}]`,attentionFor:()=>[],queuedFor:()=>[],isRunning:()=>false,attentionRow:()=>'',ageMarkup:()=>'',ago:()=>'',esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')};
vm.createContext(ctx);vm.runInContext(definitions.join('\n'),ctx);
assert.equal(ctx.questGroup({id:'off',all_watches_inactive:true}),'Inactive');
assert.equal(ctx.questGroup({id:'normal'}),'Watching');
assert.equal(ctx.questGroup({id:'empty',all_watches_inactive:false}),'Watching');
assert.equal(ctx.questGroup({id:'off',all_watches_inactive:true,status:'blocked'}),'Needs a fix');
assert.equal(ctx.questGroup({id:'new',sidequestor_bootstrap:true,all_watches_inactive:true}),'Initialising');
const row=ctx.watchRow({type:'slack_thread',monitoring_enabled:false});
assert.ok(row.includes('[inactive]'));assert.ok(row.includes('saved position is retained'));assert.ok(!row.includes('<private>'));
assert.ok(!ctx.watchRow({type:'slack_thread'}).includes('[inactive]'));
assert.equal(ctx.loops({open_items:{threads:[{reason:'disabled',monitoring_enabled:false},{reason:'active'}]}}).map(x=>x[0]).join(','),'Inactive,Watching');
ctx.state.control.reactions_enabled=false;ctx.renderQuestList();assert.ok(list.htmlOnce.includes('Trigger emojis will not start work'));
ctx.state.control.reactions_enabled=true;ctx.renderQuestList();assert.ok(!list.htmlOnce.includes('Trigger emojis will not start work'));
ctx.state.control.quests=[{id:'off',title:'Off',all_watches_inactive:true}];ctx.renderQuestList();assert.ok(list.htmlOnce.includes('All stored watches are inactive'));
const slackCopy=ctx.reviewMessageCopy({action_type:'slack_message'}).hint;
assert.ok(slackCopy.includes('Slack must also be connected'));assert.ok(slackCopy.includes('inspect Slack before retrying'));
assert.ok(!ctx.reviewMessageCopy({action_type:'email_reply'}).hint.includes('Slack'));
// Onboarding's handoff recipe must keep using the shipped dashboard app routes.
const onboarding=fs.readFileSync('src/sidequestor/runtime/yaas-triage/skills/yaas-onboarding/SKILL.md','utf8');
for(const [app,label] of [['codex','Codex'],['claude','Claude']]){
 const template=onboarding.split('\n').find(l=>l.startsWith(`- ${label}: `)).split('`')[1];
 const path='/tmp/Project & 工作区',prompt='Review setup & explain gaps';
 const actual=new URL(ctx.appUrl(app,null,path,prompt));
 const documented=new URL(template.replace('<encoded-path>',encodeURIComponent(path)).replace('<encoded-prompt>',encodeURIComponent(prompt)));
 assert.equal(documented.protocol,actual.protocol);assert.equal(documented.host,actual.host);assert.equal(documented.pathname,actual.pathname);
 assert.deepEqual([...documented.searchParams],[...actual.searchParams]);assert.equal(documented.searchParams.has('submit'),false);
}
console.log('Monitoring presentation checks passed.');
