"""在隔离的官方 OpenCLI 与用户 HOME 上验证独立 adapters；不访问真实商家。"""

import base64
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
INSTALLER = ROOT / "scripts" / "install-opencli-adapters.mjs"
ADAPTERS = ROOT / "integrations" / "opencli"


def _run(args, *, env=None):
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)


@pytest.fixture(scope="module")
def baseline():
    supplied = os.environ.get("OPENCLI_TEST_PACKAGE")
    archive = os.environ.get("OPENCLI_TEST_ARCHIVE")
    node = shutil.which("node")
    if not node or not supplied or not archive:
        pytest.fail("需要 Node、OPENCLI_TEST_PACKAGE 与官方 OPENCLI_TEST_ARCHIVE；不读取全局安装")
    source = Path(supplied).resolve()
    archive = Path(archive).resolve()
    if not archive.is_file() or not (source / "node_modules").is_dir():
        pytest.fail("显式 OpenCLI 归档或隔离 runtime dependencies 不存在")
    expected = "2M+oPc70R1jNGzKzNrsm3fN4/gdvxCKlla7s9eaaTjkDjlzHpoZFN1YdV01A185kwCTN/ChOg+rbO4epO73c3w=="
    if base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode() != expected:
        pytest.fail("OpenCLI 1.8.7 官方归档 SHA512 不匹配")
    return node, source, archive


def _copy_baseline(prefix, source, archive, *, include_dependencies=True):
    package = prefix / "node_modules/@jackwener/opencli"
    unpack = prefix / "unpack"
    with tarfile.open(archive) as bundle:
        bundle.extractall(unpack, filter="data")
    package.parent.mkdir(parents=True)
    shutil.move(str(unpack / "package"), package)
    if include_dependencies:
        shutil.copytree(source / "node_modules", package / "node_modules")
    return package


def _isolated_runtime(prefix, node):
    for directory in ("home", "bin", "cache", "tmp"):
        (prefix / directory).mkdir(parents=True, exist_ok=True)
    isolated_node = prefix / "bin" / Path(node).name
    shutil.copy2(node, isolated_node)
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    env.update(
        HOME=str(prefix / "home"), USERPROFILE=str(prefix / "home"),
        PATH=str(prefix / "bin"), NPM_CONFIG_PREFIX=str(prefix),
        NPM_CONFIG_CACHE=str(prefix / "cache"),
        TEMP=str(prefix / "tmp"), TMP=str(prefix / "tmp"),
        XDG_CACHE_HOME=str(prefix / "cache"),
    )
    return str(isolated_node), env


def _business_snapshot(package):
    paths = [path for path in (package / "clis").rglob("*") if path.is_file()]
    paths.append(package / "cli-manifest.json")
    return {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).digest() for path in paths}


@pytest.fixture(scope="module")
def installation(tmp_path_factory, baseline):
    node, source, archive = baseline
    prefix = tmp_path_factory.mktemp("ecommerce-opencli")
    package = _copy_baseline(prefix, source, archive)
    node, env = _isolated_runtime(prefix, node)
    before = _business_snapshot(package)
    for command in (
        [node, str(PATCH), str(prefix)],
        [node, str(INSTALLER), "--source", str(ADAPTERS), "--prefix", str(prefix), "--home", env["HOME"]],
        [node, str(package / "dist/src/main.js"), "list", "-f", "json"],
    ):
        result = _run(command, env=env)
        assert result.returncode == 0, result.stderr + result.stdout
    assert _business_snapshot(package) == before
    return node, prefix, package, env


def _js(installation, code):
    node, _, package, env = installation
    result = _run([node, "--input-type=module", "-e", code], env=dict(
        env, FIXTURE_PACKAGE=package.as_uri(),
        FIXTURE_ADAPTERS=(Path(env["HOME"]) / ".opencli/clis").as_uri(),
    ))
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def test_patch_reentrant_and_real_cli_discovery(installation):
    node, prefix, package, env = installation
    before = _business_snapshot(package)
    result = _run([node, str(PATCH), str(prefix)], env=env)
    assert result.returncode == 0, result.stderr
    assert _business_snapshot(package) == before
    catalog = _run([node, str(package / "dist/src/main.js"), "list", "-f", "json"], env=env)
    assert catalog.returncode == 0, catalog.stderr
    ebay = {row["name"]: row for row in json.loads(catalog.stdout) if row["site"] == "ebay"}
    assert set(ebay) == {"search", "product"}
    assert all(row["browser"] is False and row["access"] == "read" for row in ebay.values())


def test_amazon_real_payload_locale_and_strict_identity(installation):
    data = _js(installation, r"""
const root = process.env.FIXTURE_ADAPTERS;
const shared = await import(root + '/amazon/shared.js');
const product = (await import(root + '/amazon/product.js')).__test__;
const offer = (await import(root + '/amazon/offer.js')).__test__;
const discussion = (await import(root + '/amazon/discussion.js')).__test__;
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


def test_all_seven_amazon_commands_use_local_marketplace_helpers(installation):
    data = _js(installation, r"""
import vm from 'node:vm';
const names=['search','product','offer','discussion','bestsellers','new-releases','movers-shakers'];
for(const name of names) await import(process.env.FIXTURE_ADAPTERS+'/amazon/'+name+'.js');
const {getRegistry}=await import(process.env.FIXTURE_PACKAGE+'/dist/src/registry.js');
const productUrl='https://www.amazon.ca/dp/B000000001';
const node=(text,attrs={})=>({textContent:text,innerText:text,href:attrs.href,getAttribute:key=>attrs[key]??null});
function select(selector){
 if(selector.includes('rating-out-of-text')||selector.includes('#acrPopover'))return node('4.5 out of 5 stars',{title:'4.5 out of 5 stars'});
 if(selector.includes('total-review-count')||selector.includes('#acrCustomerReviewText')||selector.includes('#customerReviews'))return node('1,234 ratings');
 if(selector.includes('aria-label*="out of 5 stars"'))return node('',{'aria-label':'4.5 out of 5 stars'});
 if(selector.includes('a-offscreen')||selector.includes('a-color-price'))return node('$29.99');
 if(selector.includes('sellerProfileTriggerId'))return node('Fixture seller');
 if(selector.includes('ShipsFrom')||selector.includes('shipsFrom')||selector.includes('merchant-info'))return node('Ships from Fixture warehouse Sold by Fixture seller');
 if(selector.includes('productTitle')||selector==='h2'||selector.includes('line-clamp'))return node('Fixture product');
 if(selector.includes('/dp/'))return node('Fixture product',{href:productUrl});
 if(selector.includes('zg-bdg-text'))return node('#1');
 return null;
}
const card={getAttribute:key=>key==='data-asin'?'B000000001':null,querySelector:select,querySelectorAll:()=>[],innerText:'Fixture product'};
const document={title:'Fixture products',body:{innerText:'Fixture products'},querySelector:select,
 querySelectorAll:selector=>selector.includes('s-search-result')||selector.includes('p13n')?[card]:[]};
const paths={bestsellers:'/Best-Sellers/zgbs','new-releases':'/gp/new-releases','movers-shakers':'/gp/movers-and-shakers'};
const result={};
for(const name of names){
 const input=paths[name]?'https://www.amazon.ca'+paths[name]:productUrl;
 const href=name==='discussion'?'https://www.amazon.ca/product-reviews/B000000001':input;
 const page={goto:async()=>{},wait:async()=>{},evaluate:async code=>vm.runInNewContext(code,{document,window:{location:{href}}})};
 result[name]=await getRegistry().get('amazon/'+name).func(page,{input,query:'fixture',limit:1});
}
console.log(JSON.stringify(result));
""")
    assert set(data) == {"search", "product", "offer", "discussion", "bestsellers", "new-releases", "movers-shakers"}
    for name, rows in data.items():
        assert rows[0]["asin"] == "B000000001"
        if name == "discussion":
            assert rows[0]["average_rating_value"] == 4.5
            assert rows[0]["total_review_count"] == 1234
        else:
            assert (rows[0]["price_text"], rows[0]["price_value"], rows[0]["currency"]) == ("$29.99", 29.99, "CAD")


def test_locale_ambiguous_amounts_and_numeric_word_boundaries(installation):
    data = _js(installation, r"""
const shared=await import(process.env.FIXTURE_ADAPTERS+'/amazon/shared.js');
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
const {canonicalizeProductUrl} = await import(process.env.FIXTURE_ADAPTERS + '/coupang/utils.js');
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
const root=process.env.FIXTURE_ADAPTERS;
await import(root+'/coupang/search.js');
await import(root+'/coupang/product.js');
const {getRegistry}=await import(process.env.FIXTURE_PACKAGE+'/dist/src/registry.js');
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


def test_coupang_write_command_retains_variants_before_any_write(installation):
    data = _js(installation, r"""
await import(process.env.FIXTURE_ADAPTERS+'/coupang/add-to-cart.js');
const {getRegistry}=await import(process.env.FIXTURE_PACKAGE+'/dist/src/registry.js');
const command=getRegistry().get('coupang/add-to-cart');
let navigated, evaluated=false, stopped=false;
const page={goto:async url=>{navigated=url;throw new Error('STOP_BEFORE_WRITE');},
 evaluate:async()=>{evaluated=true;throw new Error('WRITE_PATH_FORBIDDEN');}};
try {
 await command.func(page,{'product-id':'123456789',url:'https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222&track=x'});
} catch(error){stopped=error.message.includes('STOP_BEFORE_WRITE');}
console.log(JSON.stringify({navigated,evaluated,stopped,access:command.access,browser:command.browser}));
""")
    assert data["navigated"] == "https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222"
    assert data["stopped"] is True and data["evaluated"] is False
    assert data["access"] == "write" and data["browser"] is True


def test_taobao_actual_extractor_keeps_same_title_different_ids(installation):
    data = _js(installation, r"""
import vm from 'node:vm';
const root=process.env.FIXTURE_ADAPTERS;
await import(root+'/taobao/search.js');
const {getRegistry}=await import(process.env.FIXTURE_PACKAGE+'/dist/src/registry.js');
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


@pytest.mark.parametrize("relative,anchor", [
    ("dist/src/daemon.js", "httpServer.listen(PORT, '127.0.0.1', () => {"),
    ("dist/src/browser/daemon-transport.js", "const DAEMON_URL = `http://127.0.0.1:${DAEMON_PORT}`;"),
    ("dist/src/browser/bridge.js", "async _ensureDaemon(timeoutSeconds, contextId, preferredContextId)"),
    ("dist/src/execution.js", "const BrowserFactory = getBrowserFactory(cmd.site);"),
    ("dist/src/runtime.js", "export function getBrowserFactory(site)"),
])
def test_unknown_runtime_fails_without_any_install_write(baseline, tmp_path, relative, anchor):
    node, source, archive = baseline
    package = _copy_baseline(tmp_path, source, archive, include_dependencies=False)
    node, env = _isolated_runtime(tmp_path, node)
    target = package / relative
    original = target.read_text(encoding="utf-8")
    assert anchor in original
    target.write_text(original.replace(anchor, "UNSUPPORTED_RUNTIME_STRUCTURE"), encoding="utf-8")
    paths = [path for path in package.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    result = _run([node, str(PATCH), str(tmp_path)], env=env)
    assert result.returncode != 0
    assert {path: path.read_bytes() for path in paths} == before


def test_modified_patched_runtime_fails_without_partial_changes(baseline, tmp_path):
    node, source, archive = baseline
    package = _copy_baseline(tmp_path, source, archive, include_dependencies=False)
    node, env = _isolated_runtime(tmp_path, node)
    result = _run([node, str(PATCH), str(tmp_path)], env=env)
    assert result.returncode == 0, result.stderr
    target = package / "dist/src/execution.js"
    source = target.read_text(encoding="utf-8")
    target.write_text(source.replace("reachable = response.ok;", "reachable = true;"), encoding="utf-8")
    paths = [path for path in package.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    result = _run([node, str(PATCH), str(tmp_path)], env=env)
    assert result.returncode != 0
    assert {path: path.read_bytes() for path in paths} == before
