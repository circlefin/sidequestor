const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('src/sidequestor/runtime/theme-branding.js', 'utf8');
const api = require('../src/sidequestor/runtime/theme-branding.js');
assert.equal(api.assetUrl('/theme/assets/flower.gif'), '/theme/assets/flower.gif');
assert.equal(api.assetUrl('/theme/assets/my%20crest.PNG'), '/theme/assets/my%20crest.PNG');
for (const value of ['https://example.com/a.gif', '/theme/assets/../a.png', '/theme/assets/%2e%2e/a.gif',
  '/theme/assets/a.svg', '/theme/assets/a.png?x=1', '/theme/assets/a%2fb.gif', '/theme/assets/.secret.png']) assert.equal(api.assetUrl(value), null);
assert.equal(api.chooseImage(['a','b'], () => .9), 'b');
assert.equal(api.chooseImage(['a','b'], () => .1), 'a');
const cars = [{x: -20, width: 10}, {x: 30, width: 25}];
assert.equal(api.nextCarPosition(cars, 0, 'left', 5, 100), 105);
assert.equal(api.nextCarPosition(cars, 0, 'right', 5, 100), -15);

(async () => {
  const listeners = new Map(), frames = new Map(); let serial = 0;
  const classes = new Set(['active']);
  const stream = {clientWidth: 160, innerHTML: '<original>', children: [], style: {setProperty(){}},
    classList: {contains: x => classes.has(x), add:x=>classes.add(x), remove:x=>classes.delete(x)},
    replaceChildren(){ this.children=[]; }, append(x){this.children.push(x);} };
  const logoClasses = new Set();
  const logo = {classList:{add:x=>logoClasses.add(x),remove:x=>logoClasses.delete(x)},src:'default.png', getAttribute(){return this.src;}, addEventListener(name,fn){this[name]=fn;}};
  const reduced = {matches:false, addEventListener(name,fn){this.changed=fn;}};
  const document = {hidden:false, querySelector: selector => selector === '#motion-stream' ? stream : logo,
    addEventListener(name,fn){listeners.set(name,fn);},
    createElement(){return {naturalWidth:20,naturalHeight:10,style:{}, handlers:{},setAttribute(){},
      getAttribute(name){return this[name];},remove(){stream.children=stream.children.filter(child=>child!==this);},addEventListener(name,fn){this.handlers[name]=fn;}};} };
  let observer;
  const window = {document, matchMedia:()=>reduced, addEventListener(){},
    ResizeObserver:class {constructor(fn){this.fn=fn;observer=this;}observe(){this.observed=true;}disconnect(){this.observed=false;}},
    requestAnimationFrame(fn){frames.set(++serial,fn);return serial;},cancelAnimationFrame(id){frames.delete(id);},
    fetch:async()=>({ok:true,json:async()=>({logo:'/theme/assets/crest.png',activity:{images:['/theme/assets/a.gif','/theme/assets/b.png'],height:30,gap:8,speed:40,direction:'right'}})})};
  vm.runInNewContext(script,{window,console});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(logo.src,'/theme/assets/crest.png');
  assert.ok(logoClasses.has('sq-custom-logo'));
  assert.ok(classes.has('sq-activity-train'));
  assert.ok(stream.children.length>=4 && stream.children.length<=24);
  assert.ok(stream.children.every(x=>['/theme/assets/a.gif','/theme/assets/b.png'].includes(x.src)));
  assert.equal(frames.size,1);
  for(const child of stream.children) child.handlers.load();
  assert.equal(stream.children[0].style.width,'60px'); // fixed height, preserved aspect ratio
  function frame(time){const [id,fn]=frames.entries().next().value;frames.delete(id);fn(time);}
  frame(0);frame(50);
  assert.match(stream.children[0].style.transform,/translateX\(2px\)/);
  const initialCount=stream.children.length;
  stream.clientWidth=1200;observer.fn();
  assert.ok(stream.children.length>initialCount && stream.children.length<=24);
  stream.clientWidth=80;observer.fn();
  assert.equal(stream.children.length,5);
  assert.equal(frames.size,1);
  reduced.matches=true; reduced.changed();
  assert.equal(frames.size,0); // GIF src/playback was never replaced to stop conveyor movement.
  assert.ok(stream.children[0].src.endsWith('.gif') || stream.children[0].src.endsWith('.png'));
  reduced.matches=false;reduced.changed();assert.equal(frames.size,1);
  document.hidden=true;listeners.get('visibilitychange')();assert.equal(frames.size,0);
  document.hidden=false;listeners.get('visibilitychange')();assert.equal(frames.size,1);
  window.SidequestorBranding.setMoving(false);assert.equal(frames.size,0);
  logo.error();assert.equal(logo.src,'default.png');
  assert.ok(!logoClasses.has('sq-custom-logo'));
  // Exhaust invalid assets and restore the built-in component without touching status text.
  for(const child of [...stream.children]) { if(classes.has('sq-activity-train')) { child.src='/theme/assets/a.gif';child.handlers.error(); child.src='/theme/assets/b.png';child.handlers.error(); } }
  assert.ok(!classes.has('sq-activity-train'));
  assert.equal(stream.innerHTML,'<original>');
  assert.equal(observer.observed,false);
  console.log('Theme branding behavior passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
