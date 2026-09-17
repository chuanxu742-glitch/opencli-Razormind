import {createXhsIdentityProbe} from '../chrome/platform-login-bundle4/xhs-identity.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, existsSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';
const base = new URL('../chrome/platform-login-bundle4/', import.meta.url);
const source = readFileSync(new URL('packs/account-login/content.js', base), 'utf8');
function context(platform, payload, {origin, navigate=false}={}) {
  const rule=JSON.parse(readFileSync(new URL(`packs/account-login/${platform}-qr.json`,base),'utf8'));
  origin ??= platform==='bilibili'?'https://www.bilibili.com':'https://creator.douyin.com';
  const ctx=vm.createContext({crypto:webcrypto,URL,Date,AbortSignal,rule,
    window:{location:{origin,href:origin+'/'}},document:{querySelectorAll:()=>[]},
    chrome:{runtime:{onMessage:{addListener(){}}}},
    fetch:async (url,options)=>{
      assert.equal(options.credentials,'include');assert.equal(options.redirect,'error');
      assert.equal(url,platform==='bilibili'?'https://api.bilibili.com/x/web-interface/nav':'https://creator.douyin.com/web/api/media/user/info/?aid=1128');
      if(navigate) vm.runInContext("window.location.origin='https://evil.invalid'",ctx);
      return {ok:true,headers:{get:()=> 'application/json'},json:async()=>payload};
    }});
  vm.runInContext(source,ctx);return ctx;
}
async function observe(ctx) {
  return vm.runInContext(`(async()=>{const p=await targetProbe(rule);return observePlatform({session_id:'s',epoch:1},{tabId:1,frameId:0,documentId:p.target.documentId,viewGeneration:p.target.viewGeneration,origin:p.target.origin},rule)})()`,ctx);
}
for(const [platform,good] of [['bilibili',{code:0,data:{isLogin:true,mid:123}}],['douyin',{status_code:0,user_info:{uid:'123'}}]]) {
  test(`${platform} authenticates only official API stable subject`,async()=>{
    const result=await observe(context(platform,good));
    assert.equal(result.result.evidence_kind,'valid');assert.equal(result.result.external_identity.subject,'123');
    assert.equal(result.result.region_focus,undefined);
  });
  for(const bad of [{},{code:0,data:{isLogin:false,mid:123}},{status_code:1,user_info:{uid:'123'}},{status_code:0,user_info:{uid:''}}])
    test(`${platform} rejects malformed or failed identity ${JSON.stringify(bad)}`,async()=>{
      assert.equal((await observe(context(platform,bad))).result.evidence_kind,'unknown');
    });
  test(`${platform} navigation during identity read fails closed`,async()=>{
    const result=await observe(context(platform,good,{navigate:true}));
    assert.equal(result.ok,false);
  });
}
test('bundle3 has no bundle4 rules',()=>{
  for(const platform of ['bilibili','douyin']) assert.equal(existsSync(new URL(`../chrome/script-host/packs/account-login/${platform}-qr.json`,import.meta.url)),false);
});

for (const [guest, uid, valid] of [[false,'0123456789abcdef01234567',false],[true,'0123456789abcdef01234567',false],[undefined,'0123456789abcdef01234567',false],[false,'',false]]) {
  test(`XHS unsigned identity cannot authenticate during creator diagnostic: ${guest}/${uid}`,async()=>{
    const rule=JSON.parse(readFileSync(new URL('packs/account-login/xiaohongshu-qr.json',base),'utf8'));
    const ctx=vm.createContext({crypto:webcrypto,URL,Date,AbortSignal,rule,
      window:{location:{origin:'https://www.xiaohongshu.com',href:'https://www.xiaohongshu.com/explore'}},
      document:{querySelectorAll:()=>[]},chrome:{runtime:{onMessage:{addListener(){}}}},
      fetch:async url=>{assert.equal(url,'https://edith.xiaohongshu.com/api/sns/web/v2/user/me');
        return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({code:0,data:{guest,user_id:uid}})};}});
    vm.runInContext(source,ctx);
    const result=await observe(ctx);
    assert.equal(result.result.evidence_kind,valid?'valid':'unknown');
  });
}

test('bundle4 public discovery preserves selected tab across allowed Bili redirect',async()=>{
  const ctx=vm.createContext({createXhsIdentityProbe,URL,Date,Map,Set,console,crypto:webcrypto,TextEncoder,
    fetch:async path=>({ok:true,json:async()=>{
      let file=new URL(path,base);
      if(!existsSync(file)) file=new URL('../chrome/script-host/'+path,import.meta.url);
      return JSON.parse(readFileSync(file,'utf8'));
    }}),
    chrome:{runtime:{getURL:path=>path,onInstalled:{addListener(){}},onStartup:{addListener(){}},onMessage:{addListener(){}}},
      scripting:{getRegisteredContentScripts:async()=>[],registerContentScripts:async()=>{}},
      tabs:{query:async()=>[{id:12,url:'https://www.bilibili.com/'},{id:13,url:'https://www.bilibili.com/'}],
        sendMessage:async()=>({ok:true,target:{documentId:'new-doc',origin:'https://www.bilibili.com',viewGeneration:5,qrGeneration:5}})}}});
  vm.runInContext(readFileSync(new URL('background.js',base),'utf8').replace(/^import .*;\r?\n/, ''),ctx);
  await vm.runInContext('initialization',ctx);
  assert.equal(vm.runInContext('globalThis.opencliScriptHost.health().ok',ctx),true);
  const result=await vm.runInContext("globalThis.opencliScriptHost.discoverLoginTarget({rule_id:'bilibili-qr',rule_version:'0.1.0',origin:'https://passport.bilibili.com',tab_id:12})",ctx);
  assert.equal(result.tab_id,12);assert.equal(result.document_id,'new-doc');
  await assert.rejects(vm.runInContext("globalThis.opencliScriptHost.discoverLoginTarget({rule_id:'bilibili-qr',rule_version:'0.1.0',origin:'https://www.bilibili.com'})",ctx),/ambiguous/);
});
