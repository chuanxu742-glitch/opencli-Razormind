"""在隔离的真实 OpenCLI 安装上验证补丁，不修改全局 npm 或访问商家。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "scripts" / "patch-opencli.js"


def _run(args, *, env=None):
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)


@pytest.fixture(scope="module")
def baseline():
    supplied = os.environ.get("OPENCLI_TEST_PACKAGE")
    archive = os.environ.get("OPENCLI_TEST_ARCHIVE")
    required = bool(supplied or archive)
    node = shutil.which("node")
    if not node:
        if required:
            pytest.fail("显式 OpenCLI fixture 要求 Node，不允许跳过")
        pytest.skip("需要 Node 和已安装的 OpenCLI 1.8.7")
    if archive and not Path(archive).is_file():
        pytest.fail("显式 OPENCLI_TEST_ARCHIVE 文件不存在")
    candidates = [Path(supplied)] if supplied else [
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@jackwener/opencli",
        Path("/usr/local/lib/node_modules/@jackwener/opencli"),
        Path("/usr/lib/node_modules/@jackwener/opencli"),
    ]
    source = next((p for p in candidates if (p / "package.json").is_file()), None)
    if source is None:
        if required:
            pytest.fail("显式 OpenCLI fixture 安装根不存在")
        pytest.skip("设置 OPENCLI_TEST_PACKAGE 为真实 OpenCLI 1.8.7 安装根")
    if not archive and (source / ".opencli-admin-ecommerce.json").exists():
        if required:
            pytest.fail("显式安装根已经打补丁；请提供 pristine OPENCLI_TEST_ARCHIVE")
        pytest.skip("本地安装已经打补丁；需要 pristine OPENCLI_TEST_ARCHIVE")
    return node, source, archive


def _copy_baseline(prefix, source, archive, *, include_dependencies=True):
    package = prefix / "node_modules/@jackwener/opencli"
    if archive:
        unpack = prefix / "unpack"
        with tarfile.open(archive) as bundle:
            bundle.extractall(unpack, filter="data")
        package.parent.mkdir(parents=True)
        shutil.move(str(unpack / "package"), package)
        if include_dependencies:
            shutil.copytree(source / "node_modules", package / "node_modules")
    else:
        shutil.copytree(source, package, ignore=None if include_dependencies else shutil.ignore_patterns("node_modules"))
    return package


@pytest.fixture(scope="module")
def installation(tmp_path_factory, baseline):
    node, source, archive = baseline
    prefix = tmp_path_factory.mktemp("ecommerce-opencli")
    package = _copy_baseline(prefix, source, archive)
    result = _run([node, str(PATCH), str(prefix)])
    assert result.returncode == 0, result.stderr + result.stdout
    env = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(HOME=str(prefix / "home"), USERPROFILE=str(prefix / "home"))
    (prefix / "home").mkdir()
    return node, prefix, package, env


def _js(installation, code):
    node, _, package, env = installation
    result = _run([node, "--input-type=module", "-e", code], env=dict(env, FIXTURE_PACKAGE=package.as_uri()))
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def test_patch_reentrant_and_real_cli_discovery(installation):
    node, prefix, package, env = installation
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in package.glob("clis/ebay/*.js")}
    result = _run([node, str(PATCH), str(prefix)], env=env)
    assert result.returncode == 0, result.stderr
    assert before == {p: hashlib.sha256(p.read_bytes()).digest() for p in before}
    catalog = _run([node, str(package / "dist/src/main.js"), "list", "-f", "json"], env=env)
    assert catalog.returncode == 0, catalog.stderr
    ebay = {row["name"]: row for row in json.loads(catalog.stdout) if row["site"] == "ebay"}
    assert set(ebay) == {"search", "product"}
    assert all(row["browser"] is False and row["access"] == "read" for row in ebay.values())


def test_amazon_real_payload_locale_and_strict_identity(installation):
    data = _js(installation, r"""
const root = process.env.FIXTURE_PACKAGE;
const shared = await import(root + '/clis/amazon/shared.js');
const product = (await import(root + '/clis/amazon/product.js')).__test__;
const offer = (await import(root + '/clis/amazon/offer.js')).__test__;
const discussion = (await import(root + '/clis/amazon/discussion.js')).__test__;
const rows = [
 ['com', '$1,299.99'], ['de','€1.234,56'], ['de','1.234,56 €'],
 ['ca','CDN$29.99'], ['ca','$29.99'], ['co.jp','￥2,999'],
].map(([host, price_text]) => product.normalizeProductPayload({href:'https://www.amazon.'+host+'/dp/B000000001',product_title:'Fixture',price_text}));
const invalid = ['https://evil.example/dp/B000000001','https://amazon.com.evil.example/dp/B000000001','https://user:pass@www.amazon.com/dp/B000000001','https://www.amazon.com/dp/B000000001EXTRA','http://www.amazon.com/dp/B000000001'];
console.log(JSON.stringify({rows, rejected:invalid.map(url=>{try {shared.buildProductUrl(url);return false;}catch{return true;}}),
 bare:shared.buildProductUrl('B000000001'), relative:shared.normalizeProductUrl('/dp/B000000001?tag=track'),
 offer:offer.normalizeOfferPayload({href:'https://www.amazon.ca/dp/B000000001',price_text:'$29.99',sold_by:'Seller'}),
 discussion:discussion.normalizeDiscussionPayload({href:'https://www.amazon.de/product-reviews/B000000001',average_rating_text:'4,5 von 5 Sternen',total_review_count_text:'1.234 Bewertungen',review_samples:[]}),
 japanese:shared.parseRatingValue('5つ星のうち4.7'), unknown:shared.parsePriceText('$29.99'),
 unknownPrice:shared.parsePriceText('price unavailable','https://www.amazon.com/dp/B000000001')}));
""")
    assert [(row["price_value"], row["currency"]) for row in data["rows"]] == [
        (1299.99, "USD"), (1234.56, "EUR"), (1234.56, "EUR"), (29.99, "CAD"), (29.99, "CAD"), (2999, "JPY")]
    assert all(data["rejected"])
    assert data["bare"] == data["relative"] == "https://www.amazon.com/dp/B000000001"
    assert data["offer"]["currency"] == "CAD"
    assert data["discussion"]["average_rating_value"] == 4.5
    assert data["discussion"]["total_review_count"] == 1234
    assert data["japanese"] == 4.7
    assert data["unknown"]["currency"] is None
    assert data["unknownPrice"]["price_value"] is None


def test_locale_ambiguous_amounts_and_numeric_word_boundaries(installation):
    data = _js(installation, r"""
const shared=await import(process.env.FIXTURE_PACKAGE+'/clis/amazon/shared.js');
const inputs=['$10 - $20','1.234,56 € 1.399,99 €','-$19.99'];
console.log(JSON.stringify({
 inputs, amounts:inputs.map(text=>shared.parsePriceText(text,'https://www.amazon.com/dp/B000000001')),
 cad:shared.parsePriceText('29,99 $','https://www.amazon.ca/dp/B000000001'),
 counts:['12 Kundenrezensionen','125 Kundenbewertungen','1.2K ratings','1.2万件'].map(text=>shared.parseReviewCount(text,'https://www.amazon.de/')),
 ratings:['14.5 out of 5','4.5 out of 50','4,5 von 5 Sternen','5つ星のうち4.7'].map(shared.parseRatingValue)
}));
""")
    assert data["amounts"] == [
        {"price_text": text, "price_value": None, "currency": None} for text in data["inputs"]
    ]
    assert data["cad"]["price_value"] == 29.99 and data["cad"]["currency"] == "CAD"
    assert data["counts"] == [12, 125, 1200, 12000]
    assert data["ratings"] == [None, None, 4.5, 4.7]


def test_coupang_variants_and_path_boundary(installation):
    data = _js(installation, r"""
const {canonicalizeProductUrl} = await import(process.env.FIXTURE_PACKAGE + '/clis/coupang/utils.js');
const urls=['https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222&q=tracking',
'https://www.coupang.com/vp/products/123456789?itemId=112&vendorItemId=223',
'https://www.coupang.com/vp/products/123456789oops',
'https://coupang.com.evil.example/vp/products/123456789',
'https://www.coupang.com/vp/products/123456789?itemId=oops'];
console.log(JSON.stringify(urls.map(url=>canonicalizeProductUrl(url,''))));
""")
    assert data[:2] == ["https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222", "https://www.coupang.com/vp/products/123456789?itemId=112&vendorItemId=223"]
    assert data[2:] == ["", "", ""]


def test_coupang_native_search_and_product_preserve_observed_variants(installation):
    data = _js(installation, r"""
import vm from 'node:vm';
const root=process.env.FIXTURE_PACKAGE;
await import(root+'/clis/coupang/search.js');
await import(root+'/clis/coupang/product.js');
const {getRegistry}=await import(root+'/dist/src/registry.js');
const search=getRegistry().get('coupang/search');
const product=getRegistry().get('coupang/product');
const base='https://www.coupang.com/vp/products/123456789';
const document={body:{innerText:'마이쿠팡'},querySelector:()=>null,querySelectorAll:()=>[]};
const searchPage={goto:async()=>{},autoScroll:async()=>{},evaluate:async code=>vm.runInNewContext(code,{
 document,window:{},URL,fetch:async()=>({ok:true,text:async()=>JSON.stringify({products:[
  {productId:'123456789',title:'Same title',price:12900,url:base+'?itemId=111&vendorItemId=221&track=a'},
  {productId:'123456789',title:'Same title',price:13900,url:base+'?itemId=112&vendorItemId=222&track=b'},
  {productId:'123456789',title:'Same title',price:12900,url:base+'?itemId=111&vendorItemId=221&track=c'}
 ]})})
})};
const rows=await search.func(searchPage,{query:'fixture',limit:10});
async function detail(observed,requested) {
 const location={href:observed,pathname:new URL(observed).pathname};
 const page={goto:async()=>{},wait:async()=>{},evaluate:async code=>vm.runInNewContext(code,{
  document,location,URL,window:{__INITIAL_STATE__:{productId:'123456789',itemName:'Fixture',salePrice:12900}}
 })};
 try {return {rows:await product.func(page,requested?{url:requested}:{'product-id':'123456789'})};}
 catch(error){return {error:error.message};}
}
console.log(JSON.stringify({
 search:rows,
 same:await detail(base+'?itemId=111&vendorItemId=221&track=page',base+'?itemId=111&vendorItemId=221'),
 switched:await detail(base+'?itemId=112&vendorItemId=222',base+'?itemId=111&vendorItemId=221'),
 stripped:await detail(base,base+'?itemId=111'),
 duplicate:await detail(base+'?itemId=111&itemId=112',base+'?itemId=111'),
 observedOnly:await detail(base+'?itemId=112&vendorItemId=222')
}));
""")
    base = "https://www.coupang.com/vp/products/123456789"
    assert [(row["url"], row["price"]) for row in data["search"]] == [
        (base + "?itemId=111&vendorItemId=221", 12900),
        (base + "?itemId=112&vendorItemId=222", 13900),
    ]
    assert data["same"]["rows"][0]["url"] == base + "?itemId=111&vendorItemId=221"
    assert data["same"]["rows"][0]["source_url"].endswith("&track=page")
    assert all("error" in data[key] for key in ("switched", "stripped", "duplicate"))
    assert data["observedOnly"]["rows"][0]["url"] == base + "?itemId=112&vendorItemId=222"


def test_taobao_actual_extractor_keeps_same_title_different_ids(installation):
    data = _js(installation, r"""
import vm from 'node:vm';
const root=process.env.FIXTURE_PACKAGE;
await import(root+'/clis/taobao/search.js');
const {getRegistry}=await import(root+'/dist/src/registry.js');
const command=getRegistry().get('taobao/search');
const cards=['12345678901','12345678902','12345678901','12345678903'].map(id=>({parentElement:{getAttribute:()=>id,parentElement:null},querySelector:s=>s.includes('title--')?{textContent:'Same title'}:null,querySelectorAll:()=>[]}));
const page={goto:async()=>{},wait:async()=>{},autoScroll:async()=>{},evaluate:async code=>{
 if(code.startsWith('location.href'))return;
 return vm.runInNewContext(code,{document:{body:{innerText:'products'},querySelectorAll:()=>cards},setTimeout});
}};
console.log(JSON.stringify(await command.func(page,{query:'fixture',limit:10})));
""")
    assert [item["item_id"] for item in data] == ["12345678901", "12345678902", "12345678903"]


PRELOAD = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const scenario=process.env.FIXTURE_SCENARIO;
const sandbox=process.env.FIXTURE_ENVIRONMENT==='sandbox';
const origin=sandbox?'https://api.sandbox.ebay.com':'https://api.ebay.com';
const web=sandbox?'https://www.sandbox.ebay.com':'https://www.ebay.com';
globalThis.fetch=async(input,init)=>{
 const url=new URL(input);
 assert.equal(url.origin,origin);
 assert.equal(init.redirect,'error');
 assert(init.signal);
 fs.appendFileSync(process.env.FIXTURE_TRACE,url.toString()+'\n');
 if(url.pathname==='/identity/v1/oauth2/token'){
  assert.equal(init.method,'POST');
  assert.equal(init.headers.Authorization,'Basic '+Buffer.from('fixture-id:fixture-secret').toString('base64'));
  const body=new URLSearchParams(init.body);
  assert.equal(body.get('grant_type'),'client_credentials');
  assert.equal(body.get('scope'),'https://api.ebay.com/oauth/api_scope');
  if(scenario==='token-error')return new Response(JSON.stringify({error:'fixture-secret fixture-token'}),{status:400});
  return Response.json({access_token:'fixture-token',token_type:'Application Access Token',expires_in:7200});
 }
 assert.equal(init.headers.Authorization,'Bearer fixture-token');
 assert.equal(init.headers['X-EBAY-C-MARKETPLACE-ID'],'EBAY_US');
 if(['401','403','429'].includes(scenario))return new Response('fixture-secret fixture-token',{status:Number(scenario)});
 if(scenario==='error-body')return Response.json({errors:[{message:'fixture-secret fixture-token'}]});
 if(scenario==='malformed')return Response.json({});
 const raw={itemId:'v1|123456789012|0',title:'Fixture',price:{value:'29.99',currency:'USD'},itemWebUrl:web+'/itm/123456789012'};
 if(scenario==='wrong-listing')raw.itemWebUrl=web+'/itm/999999999999';
 if(scenario==='wrong-variation')raw.itemWebUrl+='?var=999';
 if(scenario==='cross-environment')raw.itemWebUrl='https://www.sandbox.ebay.com/itm/123456789012';
 if(url.pathname==='/buy/browse/v1/item_summary/search'){
  assert.equal(url.searchParams.get('q'),'fixture');
  assert.equal(url.searchParams.get('limit'),'2');
  if(scenario==='zero')return Response.json({total:0,limit:2,offset:0});
  const next=scenario==='unsafe-next'?'https://evil.example/buy/browse/v1/item_summary/search?q=x':origin+'/buy/browse/v1/item_summary/search?q=fixture&limit=2&offset=2';
  return Response.json({itemSummaries:[raw],total:3,limit:2,offset:0,next});
 }
 assert.equal(url.pathname,'/buy/browse/v1/item/v1%7C123456789012%7C456');
 raw.itemId='v1|123456789012|456';
 raw.itemWebUrl+='?var=456';
 return Response.json(raw);
};
"""


def _ebay_cli(installation, tmp_path, scenario, environment="production", product=False):
    node, _, package, base_env = installation
    preload = tmp_path / "transport.mjs"
    preload.write_text(PRELOAD, encoding="utf-8")
    trace = tmp_path / "requests.txt"
    env = dict(base_env, FIXTURE_SCENARIO=scenario, FIXTURE_ENVIRONMENT=environment, FIXTURE_TRACE=str(trace))
    for key in ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_SANDBOX_CLIENT_ID", "EBAY_SANDBOX_CLIENT_SECRET"]:
        env.pop(key, None)
    prefix = "EBAY_SANDBOX_" if environment == "sandbox" else "EBAY_"
    if scenario != "missing-credentials":
        env[prefix + "CLIENT_ID"] = "fixture-id"
        env[prefix + "CLIENT_SECRET"] = "fixture-secret"
    command = ["product", "v1|123456789012|456"] if product else ["search", "fixture", "--limit", "2"]
    result = _run([node, "--import", preload.as_uri(), str(package / "dist/src/main.js"), "ebay", *command, "--environment", environment, "-f", "json"], env=env)
    assert "fixture-secret" not in result.stdout + result.stderr
    assert "fixture-token" not in result.stdout + result.stderr
    urls = trace.read_text().splitlines() if trace.exists() else []
    return result, urls


@pytest.mark.parametrize("environment", ["production", "sandbox"])
def test_real_ebay_cli_oauth_search_product(installation, tmp_path, environment):
    search, urls = _ebay_cli(installation, tmp_path, "success", environment)
    assert search.returncode == 0, search.stderr
    row = json.loads(search.stdout)[0]
    assert row["itemId"] == "v1|123456789012|0"
    assert row["environment"] == environment and row["marketplace"] == "EBAY_US"
    assert row["price"] == {"value": "29.99", "currency": "USD"}
    assert row["pagination"]["next"].endswith("offset=2")
    assert len(urls) == 2  # 单页，未跟随任意 next URL
    product, _ = _ebay_cli(installation, tmp_path, "success", environment, product=True)
    assert product.returncode == 0, product.stderr
    detail = json.loads(product.stdout)[0]
    assert detail["itemId"] == "v1|123456789012|456"
    assert detail["itemWebUrl"].endswith("?var=456")
    assert detail["source_url"].endswith("v1%7C123456789012%7C456")


@pytest.mark.parametrize("scenario", ["missing-credentials", "token-error", "401", "403", "429", "error-body", "malformed", "unsafe-next", "wrong-listing", "wrong-variation", "cross-environment"])
def test_real_ebay_cli_errors_are_not_success_or_secret_echo(installation, tmp_path, scenario):
    result, urls = _ebay_cli(installation, tmp_path, scenario)
    assert result.returncode != 0
    assert "ebay" in (result.stdout + result.stderr).lower()
    assert len(urls) == (0 if scenario == "missing-credentials" else 1 if scenario == "token-error" else 2)
    assert not result.stdout.strip().startswith("[")
    assert all(url.startswith("https://api.ebay.com/") for url in urls)
    if scenario == "missing-credentials":
        assert urls == []


def test_real_ebay_cli_legitimate_zero_results(installation, tmp_path):
    result, _ = _ebay_cli(installation, tmp_path, "zero")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_drift_is_rejected_before_any_install_mutation(installation):
    node, prefix, package, env = installation
    target = package / "clis/amazon/shared.js"
    original = target.read_bytes()
    manifest = (package / "cli-manifest.json").read_bytes()
    bridge = (package / "dist/src/browser/bridge.js").read_bytes()
    try:
        target.write_bytes(original + b"\n// upstream drift\n")
        result = _run([node, str(PATCH), str(prefix)], env=env)
        assert result.returncode != 0
        assert (package / "cli-manifest.json").read_bytes() == manifest
        assert (package / "dist/src/browser/bridge.js").read_bytes() == bridge
    finally:
        target.write_bytes(original)


def test_unknown_fresh_upstream_fails_without_any_install_write(baseline, tmp_path):
    node, source, archive = baseline
    package = _copy_baseline(tmp_path, source, archive, include_dependencies=False)
    target = package / "clis/coupang/search.js"
    # Keep every replacement anchor intact: content verification must catch drift.
    target.write_bytes(target.read_bytes() + b"\n// unknown upstream change\n")
    receipt = package / ".opencli-admin-ecommerce.json"
    assert not receipt.exists()

    def snapshot():
        paths = [path for directory in ("dist", "clis") for path in (package / directory).rglob("*") if path.is_file()]
        paths.append(package / "cli-manifest.json")
        return {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    before = snapshot()
    result = _run([node, str(PATCH), str(tmp_path)])
    assert result.returncode != 0
    assert snapshot() == before
    assert not receipt.exists()
