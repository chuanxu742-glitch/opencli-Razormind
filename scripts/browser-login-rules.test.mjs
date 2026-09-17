import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import {webcrypto} from "node:crypto";
import vm from "node:vm";
const source=readFileSync(new URL("../chrome/script-host/packs/account-login/content.js",import.meta.url),"utf8");
const rule=JSON.parse(readFileSync(new URL("../chrome/script-host/packs/account-login/xiaohongshu-qr.json",import.meta.url),"utf8"));
const fixture=JSON.parse(readFileSync(new URL("../chrome/script-host/packs/account-login/rules.json",import.meta.url),"utf8"));
function context({origin="https://www.xiaohongshu.com",count=1,expired=false}={}) {
 const region={textContent:expired?"二维码已过期 点击刷新":"",matches:s=>s===".code-area"};
 const parent={parentElement:region,matches:s=>s===".qrcode"};
 const node={parentElement:parent,currentSrc:"data:image/png;base64,dGVzdA==",getBoundingClientRect:()=>({x:10,y:20,width:100,height:100})};
 const ctx=vm.createContext({crypto:webcrypto,URL,Date,rule,fixture,getComputedStyle:()=>({visibility:"visible",display:"block",opacity:"1"}),
  window:{location:{origin,href:origin+"/explore"}},document:{querySelectorAll:()=>Array(count).fill(node)},
  chrome:{runtime:{onMessage:{addListener(){}}}}});
 vm.runInContext(source,ctx);return ctx;
}
test("fixture remains selected only at its exact origin",()=>{
 const ctx=context({origin:"http://127.0.0.1:49906"});
 assert.equal(vm.runInContext("fixedRule({rule:fixture}).id",ctx),"controlled-login-fixture");
 assert.equal(vm.runInContext("fixedRule({rule})",ctx),null);
});
test("wrong domain and rule version cannot select platform handler",()=>{
 assert.equal(vm.runInContext("fixedRule({rule})",context({origin:"https://example.invalid"})),null);
 assert.equal(vm.runInContext("fixedRule({rule:{...rule,version:'evil'}})",context()),null);
});
for(const count of [0,1,2]) test(`QR uniqueness ${count} never authenticates`,async()=>{
 const ctx=context({count});const result=await vm.runInContext(`(async()=>{const probe=await targetProbe(rule);return observePlatform({session_id:'s',epoch:1},{tabId:1,frameId:0,documentId:probe.target.documentId,viewGeneration:probe.target.viewGeneration,origin:probe.target.origin},rule)})()`,ctx);
 assert.equal(result.result.evidence_kind,"unknown");assert.equal(result.result.state,count===1?"presenting":"unknown");
 assert.equal(Boolean(result.result.region_focus),count===1);assert.equal(result.result.external_identity,undefined);
});
test("expired QR is not projected or considered login success",async()=>{
 const ctx=context({expired:true});const result=await vm.runInContext(`(async()=>{const probe=await targetProbe(rule);return observePlatform({session_id:'s',epoch:1},{tabId:1,frameId:0,documentId:probe.target.documentId,viewGeneration:probe.target.viewGeneration,origin:probe.target.origin},rule)})()`,ctx);
 assert.equal(result.result.state,"unknown");assert.equal(result.result.region_focus,undefined);
});

const background=readFileSync(new URL("../chrome/script-host/background.js",import.meta.url),"utf8");
async function backgroundContext(tamper=false) {
 const ctx=vm.createContext({URL,Date,Map,Set,console,
  fetch:async path=>({ok:true,json:async()=>{
   const value=JSON.parse(readFileSync(new URL("../chrome/script-host/"+path,import.meta.url),"utf8"));
   if(tamper && path==="packs/account-login/xiaohongshu-qr.json") value.allowed_origins=["https://evil.invalid"];
   return value;
  }}),
  chrome:{runtime:{getURL:path=>path,onInstalled:{addListener(){}},onStartup:{addListener(){}},onMessage:{addListener(){}}},
   scripting:{getRegisteredContentScripts:async()=>[],registerContentScripts:async()=>{}}}});
 vm.runInContext(background,ctx);await vm.runInContext("initialization",ctx);return ctx;
}
test("background loads both exact local rules and retains fixture default",async()=>{
 const ctx=await backgroundContext();assert.equal(vm.runInContext("health().ok",ctx),true);
 assert.equal(vm.runInContext("packs.get('account-login').ruleManifest.id",ctx),"controlled-login-fixture");
 assert.equal(vm.runInContext("packs.get('account-login').ruleManifests['xiaohongshu-qr@0.1.0'].authentication_verified",ctx),false);
});
test("background rejects cross-origin platform rule mutation",async()=>{
 const ctx=await backgroundContext(true);assert.equal(vm.runInContext("health().ok",ctx),false);
});
