// Exercise shipped launch routing without opening apps or submitting prompts.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('src/sidequestor/runtime/dashboard.html','utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const names=['technicalReview','troubleshootingPrompt','troubleshootReview','sendPermissionExplanation','sendPermissionControl','toggleSendPermission','queuedFor','appPrompt','appUrl','launchPreference','renderLaunchButtons','openConfiguredApp','settingsRows','renderSettings','loadSettings','openSettings','creationAppPrompt','openCreationApp'];
const definitions=names.map(name=>html.split('\n').find(line=>line.startsWith(`function ${name}(`)||line.startsWith(`async function ${name}(`)));
assert.ok(definitions.every(Boolean));
const timers=[],messages=[],posts=[],buttons={'#open-terminal':{},'#open-workspace-app':{},'#settings-edit':{},'#settings-filter':{value:''},'#settings-path':{},'#settings-status':{},'#settings-content':{},'#refresh-settings':{},'#settings-dialog':{open:false,showModal(){this.open=true}},'#open-creation-terminal':{},'#quest-prompt':{value:''},'#quest-title':{value:''},'#quest-priority':{value:'normal'},'#quest-status':{},'#quest-dialog':{open:true,close(){this.open=false}}};let terminals=0;
const ctx={URL,URLSearchParams,setTimeout:callback=>timers.push(callback),state:{selected:'quest-demo',control:{workspace:{path:'/tmp/A project 工作区 & notes'}}},window:{location:{href:'http://localhost:8877/'}},toast:m=>messages.push(m),$:id=>buttons[id],post:async path=>{posts.push(path);return {status:'launched',app:'Terminal'}},openTerminal:async()=>terminals++};
vm.createContext(ctx);vm.runInContext(html.split('\n').find(line=>line.startsWith('const esc='))+'\nlet settingsSnapshot=null;\n'+definitions.join('\n'),ctx);
(async()=>{
assert.equal(ctx.launchPreference(),'');
ctx.renderLaunchButtons();assert.equal(buttons['#open-terminal'].textOnce,'Choose app');
for(const [app,protocol,host,pathKey,promptKey] of [['codex','codex:','new','path','prompt'],['claude','claude:','code','folder','q']]){
 const path='/tmp/A project 工作区 & notes #1',url=new URL(ctx.appUrl(app,'quest-demo',path));
 assert.equal(url.protocol,protocol);assert.equal(url.hostname,host);assert.equal(url.searchParams.get(pathKey),path);
 assert.equal(url.searchParams.get(promptKey),'Within this Sidequestor workspace, for quest "quest-demo", I\'d like to');
 assert.equal(new URL(ctx.appUrl(app,null,path)).searchParams.get(promptKey),"Within this Sidequestor workspace, I'd like to");
 assert.equal(url.searchParams.get('submit'),null);
 for(const id of [undefined,'','../other','quest/x','quest?x','quest\nother',42])assert.throws(()=>ctx.appUrl(app,id,path));
 for(const badPath of [undefined,null,'','relative/path','https://example.com','/tmp/a\nother','/tmp/a\u0000other'])assert.throws(()=>ctx.appUrl(app,'quest-demo',badPath));
 ctx.state.control.workspace.open_in=app;ctx.renderLaunchButtons();assert.ok(buttons['#open-terminal'].textOnce.includes(app==='claude'?'Claude':'Codex'));
 await ctx.openConfiguredApp();assert.equal(new URL(ctx.window.location.href).searchParams.get(promptKey),'Within this Sidequestor workspace, for quest "quest-demo", I\'d like to');
 ctx.state.selected=null;await ctx.openConfiguredApp(true);assert.equal(new URL(ctx.window.location.href).searchParams.get(promptKey),"Within this Sidequestor workspace, I'd like to");
 const previous=ctx.window.location.href;await ctx.openConfiguredApp();assert.equal(ctx.window.location.href,previous);assert.ok(messages.at(-1).includes('Select a quest'));
 ctx.state.selected='quest-demo';
}
ctx.state.control.workspace.open_in='cursor';ctx.renderLaunchButtons();assert.equal(buttons['#open-terminal'].textOnce,'Open in Cursor');
const cursorUrl=new URL(ctx.appUrl('cursor','quest-demo','/tmp/A project 工作区 & notes #1'));
assert.equal(cursorUrl.protocol,'cursor:');assert.equal(cursorUrl.hostname,'file');assert.equal(decodeURIComponent(cursorUrl.pathname),'/tmp/A project 工作区 & notes #1');assert.equal(cursorUrl.search,'');
await ctx.openConfiguredApp();assert.ok(ctx.window.location.href.startsWith('cursor://file/'));
await ctx.openConfiguredApp(true,true);assert.equal(new URL(ctx.window.location.href).search,'');
for(const [app,key] of [['codex','prompt'],['claude','q']]){ctx.state.control.workspace.open_in=app;await ctx.openConfiguredApp(true,true);assert.equal(new URL(ctx.window.location.href).searchParams.get(key),"I'd like to edit this workspace's .env settings.")}
ctx.state.control.workspace.open_in='terminal';ctx.renderLaunchButtons();assert.equal(buttons['#open-terminal'].textOnce,'Open in terminal');
await ctx.openConfiguredApp();assert.equal(terminals,1);assert.equal(posts.length,0);
await ctx.openConfiguredApp(true);assert.deepEqual(posts,['/api/workspace/terminal']);
assert.equal(buttons['#open-workspace-app'].disabled,true);await ctx.openConfiguredApp(true);assert.equal(posts.length,1);timers.shift()();assert.equal(buttons['#open-workspace-app'].disabled,false);
posts.length=0;
assert.equal(ctx.creationAppPrompt(),"I'd like to create a new quest in this Sidequestor workspace for .... ");
buttons['#quest-prompt'].value='Watch our release & prepare a daily update 工作区';buttons['#quest-title'].value='Launch notes';buttons['#quest-priority'].value='high';
for(const [app,promptKey] of [['codex','prompt'],['claude','q'],['cursor',null]]){
 ctx.state.control.workspace.open_in=app;ctx.renderLaunchButtons();assert.ok(buttons['#open-creation-terminal'].textOnce.includes(app==='claude'?'Claude':app==='cursor'?'Cursor':'Codex'));
 await ctx.openCreationApp();const url=new URL(ctx.window.location.href);
 if(promptKey){assert.equal(url.searchParams.get(promptKey),"I'd like to create a new quest in this Sidequestor workspace for Watch our release & prepare a daily update 工作区\nTitle: Launch notes\nPriority: high");assert.equal(url.searchParams.get('submit'),null)}else{assert.equal(url.protocol,'cursor:');assert.equal(url.search,'')}
 assert.equal(posts.length,0);assert.equal(buttons['#quest-dialog'].open,true);assert.equal(buttons['#quest-prompt'].value,'Watch our release & prepare a daily update 工作区');timers.shift()();
}
ctx.state.control.workspace.open_in='terminal';ctx.renderLaunchButtons();assert.equal(buttons['#open-creation-terminal'].textOnce,'Create interactively in Terminal instead');await ctx.openCreationApp();assert.deepEqual(posts,['/api/quests/terminal']);assert.equal(buttons['#quest-dialog'].open,false);await ctx.openCreationApp();assert.equal(posts.length,1);timers.shift()();
for(const id of ['open-terminal','open-workspace-app'])assert.equal(html.split(`id="${id}"`).length-1,1);
for(const id of ['open-codex-app','open-claude-app'])assert.ok(!html.includes(`id="${id}"`));
assert.ok(html.includes('.composer.open+.quest-launchers{display:none}'));
assert.ok(html.includes("if(e.target.closest('#open-terminal')){await openConfiguredApp();return}"));
// Technical issue links carry local investigation instructions, not execution.
const technical={id:'repair-one',quest_id:'quest-demo',target:{action:'health_repair',system_key:'health:one'},message_text:'Investigate failed checks'};
assert.equal(ctx.technicalReview(technical),true);assert.equal(ctx.technicalReview({target:{}}),false);
const investigation=ctx.troubleshootingPrompt(technical);
for(const text of ['repair-one','logs/','state/triage/','recheck the original failing condition','verified-recovery reconciliation','Do not edit installed package source'])assert.ok(investigation.includes(text),text);
assert.ok(!investigation.includes('Write a context file'));
ctx.state.control.attention=[{approval:technical}];ctx.state.control.queued=[];
ctx.state.control.workspace.open_in='claude';await ctx.troubleshootReview('repair-one');
assert.equal(new URL(ctx.window.location.href).searchParams.get('q'),investigation);
assert.ok(!new URL(ctx.window.location.href).searchParams.has('submit'));
// Explicit permission confirmation sends a compare-and-set, without queuing drafts.
const policyCalls=[];ctx.post=async(path,payload)=>{policyCalls.push({path,payload});return {ok:true}};ctx.poll=async()=>{};
ctx.state.selected='quest-demo';ctx.state.detail={id:'quest-demo',meta:{allow_send:false,allow_send_channels:['C123']}};
const warnings=[];ctx.window.confirm=text=>{warnings.push(text);return false};
await ctx.toggleSendPermission({});assert.equal(policyCalls.length,0);
ctx.window.confirm=text=>{warnings.push(text);return true};const policyButton={};await ctx.toggleSendPermission(policyButton);
assert.equal(policyCalls.length,1);assert.equal(policyCalls[0].path,'/api/quest/quest-demo/send-permission');
assert.equal(policyCalls[0].payload.allow_send,true);assert.equal(policyCalls[0].payload.expected_allow_send,false);assert.equal(policyCalls[0].payload.confirm_send,true);
assert.ok(warnings.at(-1).includes('pending or future work'));assert.ok(warnings.at(-1).includes('C123'));assert.ok(warnings.at(-1).includes('does not cancel approved or in-flight work'));
assert.equal(policyButton.disabled,false);
const example={key:'SIDEQUESTOR_OPEN_IN',value:'<img src=x onerror=alert(1)>',default:'codex',description:'Launch <app>',source:'dotenv',in_dotenv:true};
ctx.testSettings={env_path:'/tmp/.env',overrides:[example],defaults:[{...example,key:'SIDEQUESTOR_AGENT',in_dotenv:false,description:'Worker harness'}]};
vm.runInContext('settingsSnapshot=testSettings;renderSettings()',ctx);
assert.ok(buttons['#settings-content'].innerHTML.indexOf('Your .env overrides')<buttons['#settings-content'].innerHTML.indexOf('Other settings and defaults'));
assert.ok(!buttons['#settings-content'].innerHTML.includes('<img'));
assert.ok(buttons['#settings-content'].innerHTML.includes('&lt;img'));
buttons['#settings-filter'].value='worker harness';ctx.renderSettings();assert.ok(!buttons['#settings-content'].innerHTML.includes('SIDEQUESTOR_OPEN_IN'));assert.ok(buttons['#settings-content'].innerHTML.includes('SIDEQUESTOR_AGENT'));
ctx.fetch=async()=>{throw Error('offline')};await ctx.loadSettings();assert.ok(buttons['#settings-status'].textContent.includes('Could not load settings'));assert.equal(buttons['#refresh-settings'].disabled,false);const failedStatus=buttons['#settings-status'].textContent;ctx.renderSettings();assert.equal(buttons['#settings-status'].textContent,failedStatus);assert.equal(vm.runInContext('settingsSnapshot',ctx),null);
ctx.fetch=async()=>({ok:false,status:403,json:async()=>{throw SyntaxError('HTML error body')}});await ctx.loadSettings();assert.equal(buttons['#settings-status'].textContent,'Could not load settings: 403');
ctx.fetch=async()=>({ok:false,status:500,json:async()=>({error:'Could not read the packaged settings catalog.'})});await ctx.loadSettings();assert.equal(buttons['#settings-status'].textContent,'Could not load settings: Could not read the packaged settings catalog.');
ctx.fetch=async()=>({ok:true,json:async()=>ctx.testSettings});await ctx.openSettings();assert.equal(buttons['#settings-dialog'].open,true);assert.equal(vm.runInContext('settingsSnapshot',ctx),ctx.testSettings);
assert.equal(html.split('id="open-settings"').length-1,1);assert.ok(html.includes('id="settings-dialog"'));assert.ok(html.includes("await openConfiguredApp(true,true)"));
console.log('App launch routing checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
