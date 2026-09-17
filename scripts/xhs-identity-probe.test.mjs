import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createXhsIdentityProbe} from '../chrome/platform-login-bundle4/xhs-identity.js';
const sender={tab:{id:3},documentId:'doc'};
test('creator private profile verifies only a separate session, with bounded fields and no identity',async()=>{
  for(const [body,status,expected] of [
    [{code:0,data:{name:'fixture',red_num:'12345'}},200,true],
    [{code:0,data:{name:'fixture',red_num:12345}},200,true],
    [{code:0,success:false,data:{name:'fixture',red_num:'12345'}},200,false],
    [{code:0,data:{name:'fixture',red_num:'12345'}},403,false],
    [{code:0,data:{name:'fixture'}},200,false],
    [{code:0,data:{name:' ',red_num:'12345'}},200,false],
    [{code:0,data:{name:'fixture',red_num:'x'.repeat(65)}},200,false],
  ]) {
    let clock=1000,calls=0;
    const probe=createXhsIdentityProbe({now:()=>clock,verify:async()=>{},fetcher:async url=>{
      calls++;return url.includes('edith.')?Response.json({code:-1},{status:406}):Response.json(body,{status});
    }});
    const result=await probe({},sender);clock+=30000;const cached=await probe({},sender);
    assert.equal(result.evidence_kind,'unknown');assert.equal(result.external_identity,null);
    assert.equal(result.browser_session_state,expected?'session_authenticated':'unknown');
    assert.equal(cached.observed_at,result.observed_at);assert.equal(calls,2);
    assert.ok(!JSON.stringify(result).includes('fixture'));assert.equal(result.identity_probe_diagnostic,undefined);
    assert.equal((await probe({target:{viewGeneration:9}},sender)).browser_session_state,'unknown');
  }
  let calls=0;
  const loggedOut=createXhsIdentityProbe({verify:async()=>{},fetcher:async()=>{calls++;return Response.json({code:0,data:{guest:true,name:'fixture',red_num:'123'}});}});
  const result=await loggedOut({},sender);
  assert.equal(result.evidence_kind,'invalid');assert.equal(result.browser_session_state,'unknown');assert.equal(calls,1);
});
test('only successful WWW non-guest stable identity authenticates with original cache timestamp',async()=>{
  const id='0123456789abcdef01234567'; let clock=1000,calls=0;
  const probe=createXhsIdentityProbe({verify:async()=>{},now:()=>clock,fetcher:async url=>{
    calls++; return Response.json(url.includes('edith.') ? {code:0,data:{guest:false,user_id:id}} : {code:0,data:{user_id:'other'}});
  }});
  const first=await probe({},sender);clock+=50000;const cached=await probe({},sender);
  assert.equal(first.evidence_kind,'valid');assert.equal(first.external_identity.subject,id);
  assert.equal(cached.observed_at,first.observed_at);assert.equal(calls,1);
  assert.equal(first.identity_probe_diagnostic,undefined);
  const changed=await probe({target:{viewGeneration:8}},sender);
  assert.equal(changed.evidence_kind,'unknown');assert.equal(changed.external_identity,null);assert.equal(calls,1);
  for(const [status,data] of [[403,{guest:false,user_id:id}],[200,{guest:true,user_id:id}],[200,{user_id:id}],[200,{guest:false,user_id:[id]}]]) {
    const invalid=createXhsIdentityProbe({verify:async()=>{},fetcher:async url=>Response.json({code:0,data:url.includes('edith.')?data:{guest:false,user_id:id}},{status:url.includes('edith.')?status:200})});
    assert.equal((await invalid({},sender)).evidence_kind,status===200 && data.guest===true?'invalid':'unknown');
  }
});
test('actual invoke-context verifier rejects rules and rechecks target after fetch',async()=>{
  const source=readFileSync(new URL('../chrome/platform-login-bundle4/background.js',import.meta.url),'utf8');
  const snippet=source.slice(source.indexOf('const probeXhsIdentity ='),source.indexOf('chrome.runtime.onMessage.addListener'));
  const target={tabId:3,frameId:0,documentId:'logical',origin:'https://www.xiaohongshu.com',viewGeneration:7};
  let fetches=0,generation=7;
  const ctx=vm.createContext({URL,initialization:Promise.resolve(),packs:new Map([['account-login',{ruleManifests:{'xiaohongshu-qr@0.2.0':{id:'xiaohongshu-qr'}}}]]),
    createXhsIdentityProbe:args=>createXhsIdentityProbe({...args,fetcher:async()=>{fetches++;generation++;return Response.json({});}}),
    verifyLoginTarget:async(id,current)=>{if(current.viewGeneration!==generation)throw Error('stale');},
    chrome:{tabs:{get:async()=>({url:target.origin+'/explore'})}}});
  vm.runInContext(snippet+';globalThis.probe=probeXhsIdentity;',ctx);
  const request={target,ruleId:'xiaohongshu-qr',ruleVersion:'0.2.0'};
  await assert.rejects(ctx.probe({...request,ruleId:'wrong'},sender));
  await assert.rejects(ctx.probe({...request,target:{...target,origin:'https://evil.invalid'}},sender));
  assert.equal(fetches,0);
  await assert.rejects(ctx.probe(request,sender),/stale/);assert.equal(fetches,1);
});
test('fixed credentialed probe redacts values and throttles concurrent and later calls',async()=>{
  let calls=0, clock=1000;
  const probe=createXhsIdentityProbe({now:()=>clock,verify:async()=>{},fetcher:async(url,options)=>{
    calls++; assert.equal(url,calls % 2 ? 'https://edith.xiaohongshu.com/api/sns/web/v2/user/me' : 'https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info');
    assert.equal(options.credentials,'include'); assert.equal(options.redirect,'error');
    return Response.json({code:0,success:true,data:{user_id:'SECRET_ID',name:'SECRET_NAME'}});
  }});
  const results=await Promise.all([probe({},sender),probe({},sender)]);
  assert.equal(calls,2); assert.equal(results[0].evidence_kind,'unknown');
  assert.equal(results[0].identity_probe_diagnostic,undefined);
  assert.ok(!JSON.stringify(results).includes('SECRET'));
  clock+=59999; await probe({},sender); assert.equal(calls,2);
  clock++; await probe({},sender); assert.equal(calls,4);
});
test('pre/post target verification rejects stale documents and never promotes an error',async()=>{
  let valid=true,calls=0;
  const probe=createXhsIdentityProbe({verify:async()=>{if(!valid)throw Error('stale');},fetcher:async()=>{
    calls++; valid=false; return Response.json({code:0,data:{user_id:'abc'}});
  }});
  await assert.rejects(probe({},sender),/stale/);
  await assert.rejects(probe({},sender),/stale/); assert.equal(calls,1);
  for(const status of [401,403,406,429,500]) {
    const failed=createXhsIdentityProbe({verify:async()=>{},fetcher:async()=>Response.json({code:-1},{status})});
    assert.equal((await failed({},sender)).evidence_kind,'unknown');
  }
});
test('network failures malformed and oversized bodies remain bounded unknown',async()=>{
  for(const fetcher of [async()=>{throw Error('SECRET');},async()=>new Response('bad',{headers:{'content-type':'application/json'}}),async()=>Response.json({data:{large:'s'.repeat(66000)}})]) {
    const result=await createXhsIdentityProbe({verify:async()=>{},fetcher})({},sender);
    assert.equal(result.evidence_kind,'unknown'); assert.ok(!JSON.stringify(result).includes('SECRET'));
    assert.ok(JSON.stringify(result).length<1000);
  }
});
