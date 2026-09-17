import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, cp, rm, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../frontend/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const root=fileURLToPath(new URL('../',import.meta.url));

test('versioned entry upgrades an existing profile without deleting its cookies',async t=>{
 const temporary=await mkdtemp(path.join(tmpdir(),'opencli-extension-upgrade-test-'));
 const extension=path.join(temporary,'extension'), profile=path.join(temporary,'profile');let context;
 t.after(async()=>{await context?.close();if(path.dirname(temporary)!==path.resolve(tmpdir()))throw Error('unsafe cleanup');await rm(temporary,{recursive:true,force:true});});
 await cp(path.join(root,'chrome/script-host'),extension,{recursive:true});
 await cp(path.join(root,'chrome/platform-login-bundle4'),extension,{recursive:true,force:true});
 const manifest=JSON.parse(await readFile(path.join(extension,'manifest.json'),'utf8'));
 await writeFile(path.join(extension,'manifest.json'),JSON.stringify({...manifest,version:'1.4.0',background:{service_worker:'background.js',type:'module'}}));
 await writeFile(path.join(extension,'background.js'),'globalThis.oldEntry = true;');
 const launch=()=>chromium.launchPersistentContext(profile,{channel:'chromium',headless:true,args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});
 context=await launch();let worker=context.serviceWorkers()[0]??await context.waitForEvent('serviceworker');
 assert.equal(await worker.evaluate(()=>globalThis.oldEntry),true);
 await context.addCookies([{name:'profile_preserved',value:'fixture',domain:'www.xiaohongshu.com',path:'/',expires:Math.floor(Date.now()/1000)+3600}]);
 await context.close();context=null;
 await cp(path.join(root,'chrome/platform-login-bundle4'),extension,{recursive:true,force:true});
 context=await launch();worker=context.serviceWorkers()[0]??await context.waitForEvent('serviceworker');
 await require('@playwright/test').expect.poll(()=>worker.evaluate(()=>globalThis.opencliScriptHost?.health().ok)).toBe(true);
 assert.equal(await worker.evaluate(()=>chrome.runtime.getManifest().version),'1.4.3');
 assert.ok(worker.url().endsWith('/background-entry-1.4.3.js'));
 assert.equal((await context.cookies('https://www.xiaohongshu.com')).find(c=>c.name==='profile_preserved')?.value,'fixture');
 const host=await context.newPage();await host.goto(`chrome-extension://${new URL(worker.url()).host}/host.html`);
 await require('@playwright/test').expect.poll(()=>host.evaluate(()=>globalThis.opencliScriptHost?.health().ok)).toBe(true);
 assert.equal(await host.locator('script').getAttribute('src'),'background-entry-1.4.3.js');
});

test('real extension isolated world fetches packaged catalog and rejects forged rule', async t=>{
 const temporary=await mkdtemp(path.join(tmpdir(),'opencli-extension-catalog-test-'));
 let context;
 t.after(async()=>{
  await context?.close();
  if(path.dirname(temporary)!==path.resolve(tmpdir()) || !path.basename(temporary).startsWith('opencli-extension-catalog-test-')) throw Error('unsafe cleanup');
  await rm(temporary,{recursive:true,force:true});
 });
 const extension=path.join(temporary,'extension');
 await cp(path.join(root,'chrome/script-host'),extension,{recursive:true});
 await cp(path.join(root,'chrome/platform-login-bundle4'),extension,{recursive:true,force:true});
 context=await chromium.launchPersistentContext(path.join(temporary,'profile'),{
  channel:'chromium',headless:true,
  args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`],
 });
 const worker=context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
 await require('@playwright/test').expect.poll(()=>worker.evaluate(()=>globalThis.opencliScriptHost?.health().ok)).toBe(true);
 assert.equal(await worker.evaluate(()=>globalThis.opencliScriptHost.health().ok),true);
 // Only the website HTML is intercepted. The actual extension URL fetch and
 // Chrome isolated-world permissions are exercised without a fetch stub.
 await context.route('https://github.com/login',route=>route.fulfill({contentType:'text/html',body:'<input aria-label="Login">'}));
 const page=await context.newPage();
 page.on('pageerror',error=>console.error('extension page error',error.message));
 await page.goto('https://github.com/login');
 const selected=await worker.evaluate(async()=>{
  const [tab]=await chrome.tabs.query({url:'https://github.com/login'});
  return globalThis.opencliScriptHost.discoverLoginTarget({rule_id:'official-github',rule_version:'0.1.0',origin:'https://github.com',tab_id:tab.id});
 });
 assert.equal(selected.origin,'https://github.com');
 const rule=JSON.parse(await readFile(path.join(extension,'packs/account-login/official-github.json'),'utf8'));
 const probe=await worker.evaluate(async rule=>{
  const [tab]=await chrome.tabs.query({url:'https://github.com/login'});
  return chrome.tabs.sendMessage(tab.id,{type:'opencli-script-host.login-target',pack:'account-login',version:'1.0.0',rule});
 },rule);
 assert.equal(probe.ok,true,JSON.stringify(probe));
 assert.equal(probe.target.origin,'https://github.com');
 const rejected=await worker.evaluate(async rule=>{
  const [tab]=await chrome.tabs.query({url:'https://github.com/login'});
  return chrome.tabs.sendMessage(tab.id,{type:'opencli-script-host.login-target',pack:'account-login',version:'1.0.0',rule:{...rule,login_url:'/forged'}});
 },rule);
 assert.equal(rejected.ok,false);
 assert.equal(rejected.error_code,'login_rule_unknown');
 const extensionId=new URL(worker.url()).host;
 const readable=await page.evaluate(async id=>{
  const response=await fetch(`chrome-extension://${id}/platform-catalog.json`);
  return response.ok && (await response.json()).schema_version===1;
 },extensionId);
 assert.equal(readable,true);
 const privateReadable=await page.evaluate(async id=>{
  try{return (await fetch(`chrome-extension://${id}/packs/account-login/official-github.json`)).ok;}catch{return false;}
 },extensionId);
 assert.equal(privateReadable,false);
 await context.route('https://example.org/',route=>route.fulfill({contentType:'text/html',body:'unrelated site'}));
 await page.goto('https://example.org/');
 const unrelatedReadable=await page.evaluate(async id=>{
  try{return (await fetch(`chrome-extension://${id}/platform-catalog.json`)).ok;}catch{return false;}
 },extensionId);
 assert.equal(unrelatedReadable,false);
 // Deterministically emulate a page that completed before dynamic registration:
 // subsequent discovery must attach the packaged script without a page reload.
 await worker.evaluate(()=>chrome.scripting.unregisterContentScripts({ids:['opencli-pack-account-login']}));
 for(const id of ['official-gemini','official-notebooklm','xiaohongshu-qr','official-rednote']) {
  const rule=JSON.parse(await readFile(path.join(extension,`packs/account-login/${id}.json`),'utf8'));
  const origin=rule.allowed_origins[0], url=new URL(rule.login_url,origin).href;
  await context.route(url,route=>route.fulfill({contentType:'text/html',body:'<input aria-label="Login">'}));
  await page.goto(url);
  const discovered=await worker.evaluate(async({rule,origin,url})=>{
   const [tab]=await chrome.tabs.query({url});
   const noReceiver=await chrome.tabs.sendMessage(tab.id,{type:'opencli-script-host.login-target',pack:'account-login',version:'1.0.0',rule});
   if(noReceiver !== undefined) throw new Error('missing account-login unexpectedly answered');
   return globalThis.opencliScriptHost.discoverLoginTarget({rule_id:rule.id,rule_version:rule.version,origin,tab_id:tab.id});
  },{rule,origin,url});
  const mapped=await worker.evaluate(async value=>{
   const target={tabId:value.tab_id,frameId:value.frame_id,documentId:value.document_id,origin:value.origin,viewGeneration:value.view_generation};
   const mapped=await globalThis.opencliScriptHost.verifyLoginTarget(target);
   let wrongDocumentRejected=false;
   try {await globalThis.opencliScriptHost.verifyLoginTarget({...target,documentId:'other-document'});}catch {wrongDocumentRejected=true;}
   return {ok:mapped.ok,wrongDocumentRejected};
  },discovered);
  assert.deepEqual(mapped,{ok:true,wrongDocumentRejected:true},id);
  if(id==='xiaohongshu-qr') {
   const host=await context.newPage();
   await host.goto(`chrome-extension://${extensionId}/host.html`);
   await require('@playwright/test').expect.poll(()=>host.evaluate(()=>globalThis.opencliScriptHost?.health().ok)).toBe(true);
   for (const surface of [worker,host]) {
   const checked=await surface.evaluate(async ({discovered,rule})=>{
    const nativeFetch=globalThis.fetch; let calls=0;
    globalThis.fetch=async (...args)=>{
     if(['https://edith.xiaohongshu.com/api/sns/web/v2/user/me','https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info'].includes(args[0])) {
      calls++; return Response.json({code:0,success:true,data:{name:'SECRET_NAME',red_num:'SECRET_NUMBER'}});
     }
     return nativeFetch(...args);
    };
    try {
     const target={tab_id:discovered.tab_id,frame_id:0,document_id:discovered.document_id,origin:discovered.origin,view_generation:discovered.view_generation};
     const message={pack:'account-login',action:'login.observe',tabId:target.tab_id,args:{session_id:'test',epoch:1,rule_id:rule.id,rule_version:rule.version,target}};
     const first=(await globalThis.opencliScriptHost.invoke(message)).result;
     const second=(await globalThis.opencliScriptHost.invoke(message)).result;
     return {first,second,calls};
    } finally {globalThis.fetch=nativeFetch;}
   },{discovered,rule});
   assert.equal(checked.first.result.evidence_kind,'unknown');
   assert.equal(checked.first.result.browser_session_state,'session_authenticated');
   assert.equal(checked.first.result.external_identity,undefined);
   assert.equal(checked.first.result.identity_probe_diagnostic,undefined);
   assert.equal(checked.calls,2);
   assert.ok(!JSON.stringify(checked).includes('SECRET_'));
   }
   await host.close();
   await worker.evaluate(()=>chrome.scripting.unregisterContentScripts({ids:['opencli-pack-account-login']}));
  }
 }
});
