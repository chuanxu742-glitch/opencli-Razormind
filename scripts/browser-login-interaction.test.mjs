import assert from 'node:assert/strict';
import test, {after, before} from 'node:test';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const base = new URL('../chrome/platform-login-bundle4/', import.meta.url);
const source = readFileSync(new URL('packs/account-login/content.js', base), 'utf8');
const catalog = JSON.parse(readFileSync(new URL('platform-catalog.json', base), 'utf8'));
let browser;
before(async()=> {browser=await chromium.launch({headless:true});});
after(async()=> {await browser?.close();});
async function setup(t, id='bilibili-qr', html='') {
 const rule=JSON.parse(readFileSync(new URL(`packs/account-login/${id}.json`,base),'utf8'));
 const page=await browser.newPage(); t.after(()=>page.close());
 const origin=rule.allowed_origins[0];
 await page.route('**/*', route=>route.fulfill({contentType:'text/html; charset=utf-8',body:html}));
 await page.goto(origin+rule.login_url);
 await page.evaluate(catalog=>{
   window.chrome={runtime:{getURL:path=>'chrome-extension://fixed/'+path,onMessage:{addListener(){}}}};
   window.fetch=async url=>({ok:true,headers:{get:()=> 'application/json'},json:async()=>String(url).startsWith('chrome-extension:')?catalog:{}});
 },catalog);
 await page.addScriptTag({content:source});
 await page.evaluate(rule=>window.testRule=rule,rule);
 return page;
}
const qr='<div class="login-scan"><div class="login-scan__qrcode"><img alt="Scan me!" style="width:120px;height:120px" src="data:image/png;base64,AAAA"></div>LABEL</div>';
const platformMarkup = {
 'bilibili-qr': qr,
 'xiaohongshu-qr': '<div class="code-area"><div class="qrcode"><img class="qrcode-img" style="width:120px;height:120px" src="data:image/png;base64,AAAA"></div>LABEL</div>',
 'douyin-qr': '<div class="J2iCN0Aj"><div class="XI37I0dP"><img aria-label="二维码" style="width:120px;height:120px" src="data:image/png;base64,AAAA"></div>LABEL</div>',
};
// Build the exact target shape accepted by the production message boundary.
async function call(page, action='login.observe', args={}) {
 return page.evaluate(async({action,args})=>{
  await verifyOfficialRule(testRule);
  const p=await targetProbe(testRule), x=p.target;
  return invoke({type:'opencli-script-host.invoke',pack:'account-login',version:'1.0.0',action,rule:testRule,args:{session_id:'s',epoch:1,...args},target:{tabId:1,frameId:0,documentId:x.documentId,viewGeneration:x.viewGeneration,origin:x.origin}});
 },{action,args});
}
for(const [platform,markup] of Object.entries(platformMarkup))
for(const [label,expected] of [['二维码已过期','refreshing'],['二维码已过期 已扫码 请在手机确认','verifying'],['<span style="display:none">二维码已过期</span>','presenting'],['<span style="visibility:hidden">二维码已过期</span>','presenting'],['<span style="opacity:0">二维码已过期</span>','presenting']]) {
 test(`${platform} QR visible evidence ${label} yields ${expected}`,async t=>{
  const page=await setup(t,platform,markup.replace('LABEL',label));
  const r=await call(page); assert.equal(r.ok,true);assert.equal(r.result.state,expected);
 });
}
test('visible verification challenge overrides expired QR and cannot refresh',async t=>{
 const page=await setup(t,'bilibili-qr',qr.replace('LABEL','二维码已过期')+'<div class="captcha" style="width:200px;height:100px">二次验证</div>');
 const r=await call(page);assert.equal(r.result.state,'challenge');assert.equal(r.result.region_focus.region_kind,'approved');
});
test('invisible challenge container cannot authorize whole-page projection',async t=>{
 const page=await setup(t,'bilibili-qr',qr.replace('LABEL','')+'<div style="opacity:0"><div class="captcha" style="width:200px;height:100px">二次验证</div></div>');
 const r=await call(page);assert.equal(r.result.state,'presenting');assert.equal(r.result.region_focus.region_kind,'qr');
});
test('takeover projects approved surface then a focused official field',async t=>{
 const page=await setup(t,'bilibili-qr',qr.replace('LABEL','')+'<input aria-label="验证码">');
 const switched=await call(page,'login.switch-mode',{mode:'form'});
 assert.equal(switched.ok,true);assert.equal(switched.result.state,'presenting');
 assert.equal((await call(page)).result.state,'presenting');
 assert.equal((await call(page)).result.region_focus.region_kind,'approved');
 await page.locator('input').focus();
 let r=await call(page); if(!r.ok && r.error_code==='stale_generation') r=await call(page);
 assert.equal(r.result.region_focus.region_kind,'form');assert.match(r.result.region_focus.focused_field_ref,/^official-login:/);
});
test('packaged official catalog admits genuine rule and rejects forged clone',async t=>{
 const page=await setup(t,'official-github','<input>');
 const r=await call(page);assert.equal(r.ok,true);assert.equal(r.result.state,'presenting');assert.equal(r.result.evidence_kind,'unknown');
 await page.evaluate(()=>{testRule=structuredClone(testRule);testRule.login_url='/forged';});
 assert.equal(await page.evaluate(async()=>{await verifyOfficialRule(testRule);return fixedRule({rule:testRule});}),null);
});

test('challenge clearing restores presentation while retaining approved interaction',async t=>{
 const page=await setup(t,'xiaohongshu-qr','<div class="captcha" style="width:200px;height:100px">验证码</div>');
 assert.equal((await call(page)).result.state,'challenge');
 await page.locator('.captcha').evaluate(node=>node.remove());
 const r=await call(page);assert.equal(r.result.state,'presenting');assert.equal(r.result.evidence_kind,'unknown');
 assert.equal(r.result.region_focus.region_kind,'approved');
});

const selfLink='<a href="/user/profile/0123456789abcdef01234567"><span>我</span></a>';
for(const [html,expected] of [
 ['<nav>'+selfLink+'</nav>','signed_in_visible'],
 ['<div class="side-bar">'+selfLink+'</div>','signed_in_visible'],
 [selfLink,'unknown'],
 ['<nav style="display:none">'+selfLink+'</nav>','unknown'],
 ['<nav>'+selfLink.replace('>我<','>某用户<')+'</nav>','unknown'],
 ['<nav>'+selfLink.replace('/user/profile/','https://evil.invalid/user/profile/')+'</nav>','unknown'],
 ['<nav><img alt="头像"></nav>','unknown'],
 ['<nav>'+selfLink+'</nav>'+platformMarkup['xiaohongshu-qr'],'unknown'],
 ['<nav>'+selfLink+'</nav><div class="captcha" style="width:200px;height:100px">安全验证</div>','unknown'],
]) test(`XHS self navigation appearance is only unverified observation: ${html}`,async t=>{
 const page=await setup(t,'xiaohongshu-qr',html);
 // Unsigned/rejected identity requests must never promote DOM appearance.
 await page.evaluate(()=>{window.fetch=async()=>({ok:false,status:403,headers:{get:()=> 'application/json'}});});
 const r=await call(page);
 assert.equal(r.result.browser_session_state,expected);assert.equal(r.result.evidence_kind,'unknown');
 assert.equal(r.result.external_identity,undefined);
 if(expected==='signed_in_visible') assert.notEqual(r.result.state,'challenge');
 if(html.includes('class="captcha"')) assert.equal(r.result.state,'challenge');
});

test('XHS disappearing self navigation revokes the observed login hint',async t=>{
 const page=await setup(t,'xiaohongshu-qr','<nav>'+selfLink+'</nav>');
 const first=await call(page);assert.equal(first.result.browser_session_state,'signed_in_visible');
 await page.locator('nav').evaluate(node=>node.remove());
 const next=await call(page);assert.equal(next.result.browser_session_state,'unknown');
 assert.ok(next.result.view_generation>first.result.view_generation);
});
