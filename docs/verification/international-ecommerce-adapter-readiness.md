# 国际电商适配准备度：Amazon 优先，Coupang 独立评估

日期：2026-09-05。**仅调研，没有业务实现变更，没有真实站点验收。** 已有 catalog/version 事实沿用 [平台支持清单](ecommerce-platform-support.md)，未重跑 catalog、版本或缺失 BrowserAct 检查；不是对远端生产节点能力的证明。本文读取实际安装脚本并运行其真实纯函数和项目真实解析/规范化/事件 mapper，未操作账号、购物车、订单、发布、聊天、登录，也未读取/导出会话或 cookie 值。没有安装、重配浏览器或复制其他工作区凭据。

**后续状态：用户已批准七平台实施，代码与隔离验证结果见第8节。第1—7节保留修复前研究输入和失败探针，不作为当前实现仍有这些缺陷的声明。**

路径缩写：`N = C:/Users/32536/AppData/Roaming/npm/node_modules/@jackwener/opencli`（已知本机 1.8.7），下文 `A = N/clis/amazon`、`C = N/clis/coupang`。行号对应本次读取文件。

## 1. 决策摘要

建议 **Amazon US（amazon.com）先做只读商品发现与证据采集**，先限定英文页面、USD 展示与明确美国配送上下文，不宣称多国家站通用。理由不是“其他平台不支持”，而是 search/ASIN/商品事实/Buy Box/评价摘要已经形成可组合命令；缺口集中在项目商品身份与更新语义，以及安装脚本的国际化边界。

**现状不是直接可投入商品监控的完整 adapter**：已实测同一 source 下不同 ASIN 同标题碰撞；商品价格改变 hash 不变；无标题的 offer 因每次 fetched_at 改变反而产生新 hash。原始字段并非全部丢失，`product_url` 仍在 raw 和 `extra_product_url` 中；丢的是标准 URL/稳定身份和合理的去重、更新语义。ODP mapper 也沿用此 hash，而非替换成 ASIN。

Coupang 有独立搜索和详情实现，搜索可指定页码，详情有图片字段，但无评价正文命令；商品 URL 规范化删除 itemId/vendorItemId，货币字段没有输出。应作为下一独立平台评估，不作为 Amazon 多国家扩展的替代。

## 2. Amazon 实际命令合同

共同 provenance：`source_url, fetched_at`（每次生成 ISO 时间）, `strategy='cookie'`，见 `A/shared.js:106-111`。命令应使用 `-f json`，表格 columns 不是全部 JSON 字段。

| 命令 | 实际参数 | 输出和边界 | 证据 |
|---|---|---|---|
| search | 必填位置参数 query；`--limit` int 默认20，运行层 `Math.max(1, Number(limit)||20)` | 数组；rank, asin, title, product_url, provenance, price_text/value/currency, rating_text/value, review_count_text/count, is_sponsored, badges。只读一次搜索结果 DOM，再 slice；**无 page/cursor/sort/country 参数，没有自动翻页** | `A/search.js:4-47,49-88` |
| product | 必填位置参数 input（ASIN 或商品 URL） | 单行数组；asin/title/product_url/provenance, brand_text, price/rating/review_count 三组字段, review_url, qa_url, breadcrumbs, bullet_points。**不导出商品图片、规格变体、库存、完整 description，也无 reviews 正文** | `A/product.js:6-30,32-94` |
| offer | 必填位置参数 input | 单行数组；asin/product_url/provenance、price三字段、merchant_info_text, sold_by, ships_from, offer_listing_url, review_url, qa_url, is_amazon_sold/fulfilled。读商品页 Buy Box，**不是所有卖家 offer 列表**；没有 seller_id、offer_id、运费/税费/币种换算 | `A/offer.js:38-135` |
| discussion | 必填位置参数 input；`--limit` int 默认10，同样至少1 | 单行汇总；asin/product_url/discussion_url/provenance, average_rating_text/value, total_review_count_text/count, qa_urls, review_samples。样本字段 title/rating_text/value/author/date_text/body/verified_purchase；无 review_id、下一页、排序/星级筛选参数。**评价摘要和当前页面样本，不是全量评论或 Q&A 回答抓取** | `A/discussion.js:4-29,40-119` |
| bestsellers / new-releases / movers-shakers | 可选位置参数 input（榜单 URL/路径）；limit 默认100 | 共用排名数组：list_type/rank/asin/title/product_url、price/rating/count、list_title/category_title/category_url/category_node_id/category_path/visible_category_links/provenance；遍历支持的 page_links，visited 防环、ASIN 去重，到 limit 或队列耗尽停止。不同于 search 的单页行为 | `A/rankings.js:30-61,141-220` |

### 国家 URL 与价格不是同一层能力

- 固定根 URL 为 amazon.com，包括搜索、裸 ASIN 商品/评价、榜单默认入口（`A/shared.js:2-11,113-118,142-156`）。search 没有 country/marketplace 参数。
- 完整商品/评价 URL 会保留显式允许的 Amazon marketplace host，允许域名表见 `A/shared.js:26-58`，ASIN 提取及构造见 `120-156`。查询参数和变体上下文被移除；裸 ASIN 不携带市场信息。非 Amazon URL 中若有合法 `/dp/<ASIN>`，会被转为 amazon.com URL，不会直接导航原恶意 host，但也不是严格拒绝错误来源。
- 价格 regex 只支持前置 `$ € £` 和美式分组/小数；`$` 无条件 USD（`A/shared.js:286-305`）。因此“德国 URL 被接受”不等于德国价格已支持；加拿大/澳洲同符号价格不能据此认定 USD。数字解析探针见第5节。**这些非 US 输入问题不能当作 US/USD fixture 失败：US `$1,299.99` 已正确解析。**
- rating regex 仅英文 `out of 5`；count 支持 K/M 和逗号数字（`A/shared.js:307-322`）。国际语言、数字格式需独立校验。

### 登录、错误与部分成功

- 采集入口 DOM 路径为 cookie strategy，不等于每个读命令都先调用 whoami。shared 识别四段英文 robot 文案；导航 target/context 失效转 CommandExecutionError（`A/shared.js:59-64,351-396`）。未覆盖全部语言/挑战页。
- search 零商品卡抛错；product 标题块缺失抛错；offer 无卖家/履约信息抛错，选定配送地阻挡有专门诊断（含 US 地址提示），见 `A/search.js:77-82`、`product.js:85-89`、`offer.js:127-133`。价格为空不必然使这几类结果失败。
- discussion 先评价页，缺摘要则商品页回退；两页均登录页才明确 AuthRequiredError；有摘要就可成功，即使 review_samples 为空（`A/discussion.js:62-83,108-117`）。因此“成功”只能说明摘要存在，不代表采到了要求数量的正文。
- auth 实现仅 US host 和英文 greeting：先检查是否存在已知认证 cookie 名，再读取头部用户名（`A/auth.js:4-37,40-53`）。本次只读源码，**未调用 auth/whoami/login 或 cookie API**。

## 3. 项目真正执行/存储链与最小扩展位置

1. **动态发现**：`backend/workflow/opencli_adapter_nodes.py:298-343` 从所选 binary `list -f json` 加载 site/name；`346-407` 按 args/access/browser 映射 source/action、params、provider=opencli。`481-524` 做参数 materialization。无需另造 Amazon 站点白名单或 BrowserAct manifest。Coupang product 的两个参数都标 optional，但实际要求至少其一；当前基于 required_args 的 runnable 判定不能表达这种 one-of（安装 `C/product.js:199-215` 对比项目 `346-366,481-486`），这是静态 readiness 假阳性风险，未实际运行 catalog 验证。
2. **执行**：`backend/channels/opencli_channel.py:642-685` 合并 config.args/parameters，CLI help 判定 named 与 positional，随后按 browser runtime 路由；`550-615` 对子进程 exit、JSON 解析做检查。`255-260` 的 `_parse_json` 只包装 JSON object，不验证商品形状。应显式传 `positional_args=[query/input]`，named args 只放该命令真实选项，避免未知 key 被当位置参数。
3. **fetch 没有另一个电商清洗层**：`opencli_channel.py:819-849` 委托 `AbstractChannel.fetch`；`backend/channels/base.py:135-154` 返回 collect 的原始 items，默认 identity=None。OpenCLI 未覆盖 identity，本次实际对象调用也为 None。
4. **sink**：`backend/pipeline/pipeline.py:363-382` 按 source write_strategy 选 sink，并直接传 items。legacy 的 `backend/pipeline/sinks/legacy_db_sink.py:49-90` 调用 normalize_items，再 channel.identity(raw)，再 store_records。
5. **规范化/更新**：`backend/pipeline/normalizer.py:17,68-94` 标准 URL 仅 url/link/href/permalink；其他键保留 extra_。有 title/url/content 时 hash 仅四元组 title|url|content|source_id，否则 full raw JSON（含 fetched_at）。`backend/pipeline/storer.py:141-212` 对 hash 重复 skip；仅 identity 匹配且 hash 改变时原位更新。因此只补 identity 或 product_url **不够**：价格没有进入变化判定，仍会被跳过。
6. **ODP**：`backend/odp/mapper.py:44-71` 的 from_triple 以 content_hash 作为 event_id，payload=normalized、raw_data=raw；本地 mapper 执行证明确实如此，**不意味着已请求 ODP 服务或验证 Rust 持久化**。

最小 owned-code 接入面（建议，未实施）：复用 dynamic node/materialization 和 OpenCLI runtime；在现有 channel/normalizer 边界建立明确平台+marketplace+native ID 商品合同和 URL 映射，不将所有未知字段泛化成另一套框架；在 normalizer/storer/ODP mapper 对齐实体身份与事实变更摘要；需要搜索→详情多命令编排时复用现有 workflow/source 机制并显式保留跨 source 实体关联。安装目录中的解析缺陷属于上游安装脚本边界，不能靠静默编辑全局 npm 文件当作项目持久修复；实施时另行决定受控补丁或上游升级。

建议商品 entity key 为 `amazon:{marketplace}:{asin}`；Coupang 先区分 product 与 item/vendorItem 变体。事实变更摘要应包括价格数值/币种、卖家履约、关键商品事实等，排除单纯 fetched_at；观测时间独立保留。建议实体当前状态原位更新，若业务明确要价格历史，独立记录有效变化/观测语义，不能把当前偶然 hash 行为当历史设计。search/product/offer/discussion 是同一实体不同 facet，不能互相覆盖掉缺失字段或把“未读取”误当“已清空”。跨 source 的去重和同一 source 去重需分开定义。

## 4. Coupang 专项

- `search query [--page 1] [--limit 20] [--filter rocket]`；page 为正整数，limit 1..50，其他 filter 抛 ArgumentError（`C/search.js:408-473`、`C/utils.js:12-32`）。指定页只取该页，不自动翻所有页；rocket 先导航第一页点击筛选再切到目标页，autoScroll 2/3 次。筛选点击属于页面交互，不是加购；本次也没有执行。
- 源路径：页面内带 credentials fetch 搜索候选 → JSON-LD → bootstrap，DOM 总会读取并补齐/覆盖；filter 模式只取 DOM（`C/search.js:185-209,267-301,304-397,455-465`）。接口出错/非 JSON 会 fallback，不可据成功断言 API 路径成功。
- search 完整 helper 输出：rank/product_id/title/price/original_price/unit_price/discount_rate/rating/review_count/rocket/delivery_type/delivery_promise/seller/badge/category/url（`C/utils.js:224-264`）。无 currency、fetched_at、source_url、image_url。
- `product [product-id] [--url URL]` 两个在 schema 中都非必填，函数要求至少一个。数字 ID 至少6位；url-only 做 Coupang host/path 验证（`C/product.js:199-225`，`C/utils.js:92-138`）。同时提供两者时偏向 product-id 校验，不等价于 url-only 路径。
- 详情读取 JSON-LD、bootstrap、DOM，合并实际优先 bootstrap > JSON-LD > DOM；JSON-LD 的 offers 只取第一项，不读取 priceCurrency；只要 title 或 price 任一存在即可 ok（`C/product.js:29-187`）。输出 product_id/title/price/original_price/discount_rate/rating/review_count/seller/brand/rocket/delivery_promise/image_url/url（`241-255`）。只有评分和计数，**没有评论正文/评论分页**。
- 价格数字剥离非数字/小数点；`12,900원`→12900，但没有币种字段（`C/utils.js:66-74`）。KRW 只能作为明确 Korea 市场约束的未来映射，不能谎称命令已给出币种。
- URL canonicalization 保留 product ID、删除 itemId/vendorItemId 和其他查询参数，helper 去重以 URL 优先（`C/utils.js:34-36,120-138,266-279`）。存在变体/卖家身份折叠风险；第5节有确定性 URL 证据，未实站验证具体变体。
- 详情路径 currentProductId 不匹配会报 EmptyResultError；但 bootstrap 搜索第一个 product-like 节点时未要求它的 ID 等于 expectedProductId（`C/product.js:44-47,88-105,175-185,231-255`）。[推断] 推荐商品节点排在前面时可能拿错标题/价格却返回当前页面 ID；这是待专项 fixture/实站确认风险，不是已观测线上事故。
- search 只有无商品时才用 loginHints 判断 AuthRequiredError；product 不论有无 data，只要有登录链接且无“마이쿠팡”文字就报 auth（`C/search.js:466-470`，`C/product.js:227-237`）。[推断] 公开可读商品也可能被此启发式拒绝。whoami 源检查登录重定向、Access Denied 与用户名（`C/auth.js:4-33`）；本次未执行任何身份读取。

## 5. 实际本地只读探针（输入、调用、结果）

执行方式：Eval 的 Node `child_process.spawnSync` 启动 `node --input-type=module -e <JS>`，真实 import 安装 helper；将 JSON stdout 作为 `PROBE_FIXTURES` 环境变量传给 `uv run --no-sync python -c <Python>`，cwd 为本仓库。脚本仅在内存中，无永久测试/临时文件，无网络/数据库写入。第一次直接 import OpenCLIChannel 触发 registry/cli_channel 循环导入；调整为先 `import backend.channels.registry` 后成功，未修改业务代码。

等价可重跑核心 JS（这里的 ASIN/商品/卖家均为人工 fixture，不是实站数据）：

```javascript
const root = 'C:/Users/32536/AppData/Roaming/npm/node_modules/@jackwener/opencli';
const load = p => import('file:///' + root + '/clis/' + p);
const s = await load('amazon/shared.js');
const p = (await load('amazon/product.js')).__test__;
const o = (await load('amazon/offer.js')).__test__;
const d = (await load('amazon/discussion.js')).__test__;
const c = await load('coupang/utils.js');
const rows = [
  p.normalizeProductPayload({href:'https://www.amazon.com/dp/B000000001',product_title:'Same title',price_text:'$19.99'}),
  p.normalizeProductPayload({href:'https://www.amazon.com/dp/B000000002',product_title:'Same title',price_text:'$29.99'})
];
console.log(JSON.stringify({
  prices: ['$1,299.99','€1.234,56','1.234,56 €','CDN$29.99','￥2,999'].map(x=>[x,s.parsePriceText(x)]),
  urls: ['B000000001','https://www.amazon.de/dp/B000000001?th=1','https://evil.example/dp/B000000001'].map(x=>[x,s.buildProductUrl(x),s.buildDiscussionUrl(x)]),
  rows,
  offer: o.normalizeOfferPayload({href:'https://www.amazon.com/dp/B000000001',sold_by:'Example seller',price_text:'$19.99'}),
  discussion: d.normalizeDiscussionPayload({href:'https://www.amazon.com/product-reviews/B000000001',average_rating_text:'4.5 out of 5',total_review_count_text:'1,200 ratings',review_samples:[]}),
  coupang: c.normalizeSearchItem({productId:'123456789',title:'Fixture',price:'12,900원',reviewCount:'120',url:'https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222'},0)
}));
```

真实结果（Node exit 0 / stderr 空）：

| 输入 | 观察输出 | 解释/预期对照 |
|---|---|---|
| `$1,299.99` | price_text=`$1,299.99`, value=1299.99, USD | US 例正确 |
| `€1.234,56` | price_text=`€1.234`, value=1.234, EUR | 德式语义应1234.56；数字与原文均被截断 |
| `1.234,56 €` | 原文保留，value/currency=null | 未解析，不应当作零价 |
| `CDN$29.99` | `$29.99`, 29.99, USD | 加拿大语义应CAD；不能以美元符号判US |
| `￥2,999` | 原文保留，value/currency=null | 不支持该符号 |
| 裸 `B000000001` | amazon.com/dp 与 amazon.com/product-reviews | 默认 US |
| 德国完整 URL + `?th=1` | amazon.de/dp 与 amazon.de/product-reviews，查询移除 | host保留不代表价格/变体支持 |
| evil.example/dp URL | 两个输出均改成 amazon.com | 无外站导航，但无严格非法域名输入拒绝 |
| Coupang fixture | price=12900, review_count=120, url=`https://www.coupang.com/vp/products/123456789` | itemId/vendorItemId丢弃，无currency |
| discussion fixture | average_rating_value=4.5, total_review_count=1200, review_samples=[] | 正规汇总 shape 允许零样本；本次调用 normalize helper，不是执行整条浏览器命令 |

接续实际 Python 核心（`PROBE_FIXTURES` 为上述完整 stdout）：

```python
import backend.channels.registry
import json, os
from backend.channels.opencli_channel import OpenCLIChannel, _parse_json
from backend.pipeline.normalizer import normalize_item
from backend.odp.mapper import RecordEventMapper
f = json.loads(os.environ['PROBE_FIXTURES'])
rows = _parse_json(json.dumps(f['rows']))
a, ha = normalize_item(rows[0], 'fixture-source')
b, hb = normalize_item(rows[1], 'fixture-source')
_, hp = normalize_item(dict(rows[0], price_value=99), 'fixture-source')
x, hx = normalize_item(f['offer'], 'fixture-source')
_, hy = normalize_item(dict(f['offer'], fetched_at='2099-01-01T00:00:00Z'), 'fixture-source')
c, hc = normalize_item(f['coupang'], 'fixture-source')
_, hc2 = normalize_item(dict(f['coupang'], price=99999), 'fixture-source')
e = RecordEventMapper.from_triple(channel_type='opencli', source_id='fixture-source', task_id='fixture-task', raw=rows[0], normalized=a, content_hash=ha).to_wire()
print(json.dumps({
  'amazon_url':a['url'], 'extra_product_url':a['extra_product_url'],
  'same_title_distinct_asin_hash_collision':ha==hb,
  'amazon_price_change_same_hash':ha==hp,
  'opencli_identity':OpenCLIChannel().identity(rows[0]),
  'offer_timestamp_changes_hash':hx!=hy,
  'coupang_url':c['url'], 'coupang_price_change_same_hash':hc==hc2,
  'odp_event_id_equals_hash':e['event_id']==ha, 'odp_payload_url':e['payload']['url'],
  'parse_error_object':_parse_json('{"error":"fixture error"}')
}, ensure_ascii=False))
```

Python exit 0、stderr 空，完整判断输出：

```json
{"amazon_url":"","extra_product_url":"https://www.amazon.com/dp/B000000001","same_title_distinct_asin_hash_collision":true,"amazon_price_change_same_hash":true,"opencli_identity":null,"offer_timestamp_changes_hash":true,"coupang_url":"https://www.coupang.com/vp/products/123456789","coupang_price_change_same_hash":true,"odp_event_id_equals_hash":true,"odp_payload_url":"","parse_error_object":[{"error":"fixture error"}]}
```

说明：价格改变 fixture 仅替换数值，刻意隔离该字段是否参与 hash；不是伪称第二次实站价格变化。预期商品监控应使不同 ASIN 独立、相同实体价格改变可持久更新、同事实仅 fetched_at 改变不误当新实体。当前实现相反的两个现象必须分开：**有 title 的商品：价格变化被忽略；无 title/url/content 的 offer：仅抓取时间变化就导致全 raw hash 变化。** discussion 同样没有标准字段，源码支持相同时间敏感风险，但本次未单独对其做 hash 对照。

`_parse_json` 接受 error object 仅证明形状不校验。结合 subprocess exit0→ChannelResult.ok 路径，可推断返回 exit0 错误对象时有假成功风险；本次没有注入/执行 subprocess false-success 场景，**不能宣称当前 Amazon CLI 已发生这种失败**。Amazon 自身多数空内容路径会抛错。

legacy store 的 skip/update 后果由第3节实际源分支推导；**本次未写数据库，也未运行完整 pipeline/ODP 网络调用**。raw/extra 字段保留不等于 UI、hash、entity identity 或更新已正确。

## 6. 下一阶段具体 smoke 验收（尚未执行）

1. **前提和边界**：用户明确授权只读、使用现有已授权 US 浏览器上下文，确认目标节点安装版本及 DOM 可见；记录 marketplace/展示币种/配送区域，不记录 cookie/token/地址私人细节。禁止调用 login、购物车/购买、评论发布、聊天；同一 Amazon target 顺序执行避免导航互扰。不为跑通重配/复制会话。
2. **发现**：`opencli amazon search "desk shelf organizer" --limit 3 -f json`；人工对照可见页面，要求每行合法ASIN、标题、canonical product_url、来源、抓取时间；明确这是单页限量，不验收为全站全集。真实空结果与挑战页不得同属成功空数组。
3. **详情**：对选定两条结果依次 `opencli amazon product <完整amazon.com商品URL> -f json`，要求详情ASIN和请求身份一致、标题/价格/评分与页面对应；缺价格可作为部分数据明确标记，不假造0。无图片/规格功能不作为现有成功项；若用户需要图片，先作为显式新增合同。
4. **offer**：同ASIN `opencli amazon offer <URL> -f json`，对照卖家、发货方、Buy Box价格/币种，明确当前配送区域影响，不要求所有offer；配送限制、缺Buy Box和缺登录分别可诊断，不以成功记录替代。
5. **discussion**：`opencli amazon discussion <URL> --limit 3 -f json`，核对摘要与样本正文来源，记录实际抓取页（评价页或商品页fallback）。有摘要但0样本只能验收“摘要成功”；不得宣称3条/full reviews。无review_id意味着样本级去重另有设计缺口。
6. **normalize/store（实施后隔离数据源/测试库）**：同标题两个ASIN→两个entity；同ASIN不同marketplace→不同entity；相同US实体price/currency/offer事实改变→当前状态更新且保留明确历史策略；只fetched_at改变→不制造重复entity/事实版本。search/detail/offer/discussion合并不擦除其他facet；source_id变化不悄然改变商品本体身份。金额采用明确精度合同（US可整数美分/decimal），null不是0；拒绝无证据跨币种比较。
7. **双存储路径**：分别验证 legacy 与实际选用ODP路径的identity/event_id/更新/重复计数，重跑不会丢第二ASIN或堆积时间戳重复；数据库读回证明持久化，不以CLI exit0代替。
8. **非US回归门槛**：德国前后置欧元/小数逗号、加拿大美元符号、日元/语言评分、完整URL与裸ASIN市场差异必须独立通过后才扩大国家支持。本次探针表应成为修改后的对照，不宣称US已受这些输入影响。
9. **Coupang后续**：仅授权韩国场景，search page1/page2验证页面实际切换及去重，rocket验证实际筛选；product核对页面ID与bootstrap事实同属一商品，保留/定义itemId/vendorItemId；KRW显示证据、price更新、图片URL与评分计数检查，不能要求不存在的评论正文命令。无账号/地区可用性证据时维持“未实站验收”。

## 7. 多平台实现准备（待方案批准）

本节是方案 companion，只读源码补充，不代表批准或完成实现。拟批准范围为现有淘宝/JD/1688/闲鱼/Amazon/Coupang 六平台商品可靠性，加上另行真实实现的 eBay Browse 搜索/详情；其他海外平台不能仅以枚举、展示标签或通用提取声明计作已实现。以下安装路径仍用 `N` 缩写；Amazon/Coupang 的精确合同与失败 fixture 见前文。

### 国内平台字段与身份矩阵

| 平台 / 命令 | 实际输入与输出 | 身份/数据边界与源码 |
|---|---|---|
| 淘宝 search | 位置 query；sort=`default/sale/price`；limit 默认10、最大40。输出 rank,title,price（¥文本）,sales,shop,location,item_id,url | item_id 是商品ID，不是所选SKU；`N/clis/taobao/search.js:11-16,76-85` |
| 淘宝 detail | 位置 id；输出**整批 `{field,value}` 数组**，字段含商品名称、价格、销量、评价数、店铺评分、店铺、发货地、可选规格、ID、链接，部分字段可缺失 | 商品身份在最后的 ID/链接行；必须以命令上下文聚合一整批，不能把每个字段行作为商品。`N/clis/taobao/detail.js:29-66` |
| JD search / detail | search 位置 query、limit 默认10最大30 → rank,title,price（¥文本）,shop,sku,url；detail 位置 sku → 商品名称、价格、SKU、店铺、评价数量、好评率、评价标签、链接的 field/value 数组 | sku 是SKU层级，不能抹掉变体差异；detail 同样需要批聚合。`N/clis/jd/search.js:10-14,51-58`、`detail.js:48-58` |
| JD item | 位置 sku；images 默认200。输出 title,price,shop,specs,mainImages,detailImages,totalImages,pageState | **无顶层 sku/url**；需从请求上下文保留SKU，并与 pageState.href/实际商品页面核对；price 可为 `not found`，不能转为0。`N/clis/jd/item.js:550-564,565-578,655-696` |
| 1688 search | 位置 query、limit；输出 rank,offer_id,member_id,shop_id,title,item_url,seller_name/url,price_text,price_min/max,currency,moq_text/value,location,badges,sales_text,return_rate_text,source_url,fetched_at,strategy | offer_id 是商品身份，member_id/shop_id 是卖家身份；内部最多12页，不是由通用channel提供cursor。`N/clis/1688/search.js:8,35-57,241-301` |
| 1688 item | 位置 input（offer ID/商品URL）；输出 offer_id,member_id,shop_id,title,item_url,main_images,price_text,price_tiers,currency,moq_text/value,seller_name/url,shop_name,origin_place,delivery_days_text,customization_text,private_label_text,visible_attributes,sales_text,service_badges,stock_quantity及provenance | **阶梯价格和MOQ是合同内容**，不得静默压成一个无数量条件的单价；`N/clis/1688/item.js:25-56,156-176` |
| 闲鱼 search | 位置 query；limit 默认20最大60、min-price/max-price（元）、province/city。输出 rank,item_id,title,price（¥文本）,condition,brand,location,badge,want,url；内部翻页 | site 为 xianyu，URL为goofish；item_id 是商品身份。`N/clis/xianyu/search.js:3-9,161-171,199-206,247-251` |
| 闲鱼 item | 位置 item_id；输出 item_id,title,description,price,original_price,want_count,collect_count,browse_count,status,condition,brand,category,location,seller_name/id/score,reply_ratio_24h,reply_interval,item_url,seller_url,image_count,image_urls | item_url 目前不进标准URL；description 已按现有规则进 content，改造不得误丢。seller_id 不等于商品ID。`N/clis/xianyu/item.js:79-102,115-118` |

**上游不可恢复的数据丢失**：淘宝 search 在提取ID前先以 title 去重（`N/clis/taobao/search.js:43-49`），因此不同ID同标题的第二张卡片可能根本未输出。仅修项目 hash 无法恢复这一类上游丢卡；需要在批准范围中明确受控上游补丁或诚实保留限制。

**货币与分类约束**：上述国内输出只有1688显式给出 currency；其余不能只凭 `¥` 推断CNY，应由可信平台/市场上下文定币种并保留原始文本。商品识别须同时参考平台和命令，不能把任意含 price/sku 的社交记录变成商品。淘宝/JD reviews 输出的是评论行，不能赋相同商品identity后互相覆盖（`N/clis/taobao/reviews.js:10-14`、`N/clis/jd/reviews.js:10-14,40-43`）。平台原生实体身份、市场/SKU变体、采集source作用域、facet类型是不同维度；fetched_at应保留为观测时间但不单独改变商品事实摘要。

### 验证文件与 fixture 所有权建议

方案批准后建议验证工作流独占下列测试文件；**目前未编辑**。已有第5节失败fixture无需重跑确认，作为后续回归输入保留在本文。具体接口由核心实现者先给出，验证者不提前创造平行合同。

- `tests/unit/pipeline/test_normalizer.py`：不同原生ID同标题、市场隔离、价格/币种/阶梯变化、仅观测时间变化，以及社交/非商品保持原行为。现有 `177-193` full-raw fallback 对非商品应保持，不能为商品场景全局删除。
- `tests/unit/pipeline/test_storer.py`：真实数据库不同实体并存、同实体事实变更更新、未变重复、变体区分。现有 `309-343` identity更新、`347-360` 同hash跳过、`364-379` 无identity原行为、`424-439` 同批identity先到优先是受影响合同；仅在新方案确实改变行为时调整。
- `tests/unit/pipeline/test_legacy_db_sink.py`：优先增加/改造真实内存DB读回的可观察行为，不新增mock转发回声断言；源/任务外键、实体更新、facet不互擦要有真实结果。
- `tests/unit/odp/test_mapper.py`：商品entity/事实版本/观测时间的输出语义和非商品兼容；仅字段复制断言不作为新增回归价值。
- `tests/unit/channels/test_opencli_channel.py`：淘宝/JD field/value批聚合、JD请求SKU与实际页面关联、非商品/评论保留原形状；不能逐字段行造商品。
- Amazon durable patch 工作流独占其补丁功能回归，避免与上述文件重叠；用临时npm-root**实际执行patcher**后import真实helper，覆盖US正确例、EUR/CAD边界、重复执行幂等与未知源码拒绝/明确失败。`tests/unit/test_patch_opencli_remote_bridge.py:12-82` 当前手写已patch代码且仅检查源码标记，不能照搬作为“实际补丁生效”的证据。

值得永久保留的是身份碰撞、变体/市场隔离、金额缺失与0、更新/重复转换、错误批次拒绝、商品与社交边界等真实易错条件，而非每个平台字段复制的参数化堆积。后续可复用 `tests/conftest.py:26,51-70` 的内存SQLite/create_all/sessionmaker；仅将 `backend.database.AsyncSessionLocal` 绑定临时工厂，构造source/task后执行真实LegacyDbSink→normalizer→storer并读回，`forward_to_odp=False`，不接共享/生产库。ODP mapper/wire可无网络验证，但Rust持久化仍需另验；本节未执行新探针。

### 受控补丁分发约束

`scripts/patch-opencli.js:27-45` 支持显式npm prefix，可用于临时副本验证；`47-69` 当前找不到文件/文本锚会skip/warn返回，新增商品补丁不能把这种结果误报已修复。`scripts/install-managed-opencli.ps1:26-35` 安装固定1.8.7后从中央API下载并执行**单个 patch-opencli.js 文件**；远端没有理由自动具备仓库相邻模块。新增Amazon逻辑必须随该单文件交付，或显式更新分发合同与所有安装调用者，不能暗中依赖repo邻居文件。`40-49` 的另一个固定OhMyOpenCLI checkout服务于已有official-site能力，不能推定为六电商的已部署overlay。

禁止直接修改全局npm安装作为持久实现；验证只用显式临时prefix，保留任何既有用户package-lock修改。此次追加仅补充方案上下文，代码、测试、安装器和锁文件均未修改。

## 8. 实施与验证增补（2026-09-05）

本节是本批变更记录与验收边界，取代前文“待批准/尚未实施”的当前状态，不改写历史探针。批准并交付的范围是淘宝、京东、1688、闲鱼、Amazon、Coupang商品可靠性，以及真实eBay Browse搜索/详情；**AliExpress、Shopee、Lazada、Temu、TikTok Shop、Walmart不在本批实现、不计已支持**。

### 已实现合同与兼容边界

- `backend/channels/ecommerce.py::adapt_items` 按site+command识别商品，保留原始来源事实，拒绝缺失/冲突身份、错误批次和非法观测时间；淘宝/JD detail整批field/value聚合，JD item核对有效请求SKU与pageState.href。`OpenCLIChannel.collect` 所有成功运输分支共享适配入口，并使用实际CLI help解析后的参数上下文，不把旧key转位置参数后漏掉身份校验。
- 实体身份区分平台、marketplace、原生ID及Coupang item/vendorItem变体；商品`search/product/offer/discussion`共享entity，但分别保存facet。source界定证据归属，不并入商品本体身份；不同source互不擦除。legacy保留每个facet当前状态；价格等事实变化更新，单纯观测时间变化刷新observed_at但不制造事实版本。
- `normalizer` 产出 `normalized_data.ecommerce`（entity/facet、observed_at、facts、fact_version等）；商品特有字段统一位于 **`ecommerce.facts`**，例如 `ecommerce.facts.price_value` / `ecommerce.facts.price_tiers`。不保留旧商品`extra_*`兼容别名，原始行仍在raw_data；field/value批保留原始批证据。仓库内消费者已由实现者核查；**已保存工作流或外部消费者若自行引用旧extra字段，需改用新路径**。非商品/社交记录沿用原合同。历史数据不批量改写，重新采集按新合同写入，跨历史记录迁移不在本批。
- 1688保留数量阶梯、MOQ和币种证据，不压成单价；Coupang保留变体URL；缺价不是0。ODP wire携带实体/facet/事实版本/观测信息，以稳定事实事件去重，不因时钟制造事件；这不是跨source最新商品物化表，也不承诺价格历史功能。
- 自包含 `scripts/patch-opencli.js` 在固定1.8.7基线上修复Amazon URL/货币/数字/评分计数、淘宝标题去重、Coupang变体URL，并安装eBay命令；未知基线在商品补丁写入前明确失败。没有直接编辑全局npm作为交付，也未部署共享节点。

### 审查后存储约束与部署迁移

商品当前facet新增数据库部分唯一索引 `uq_collected_records_product_identity`：键为 `(source_id, identity_key)`，仅作用于 `normalized_data.ecommerce.entity_id` 存在的商品行；非商品旧身份不受该索引影响。部署新版写入逻辑前，需对目标数据库按项目原流程升级Alembic至 `s8t9u0v1w2x3`（上一版本 `r6s7t8u9v0w1`；文件 `backend/migrations/versions/s8t9u0v1w2x3_add_product_facet_identity.py`）。**本次没有在用户/生产数据库执行迁移。** 迁移只建索引、不批量改写历史记录；若预发布环境已存在相同商品source+facet重复行，唯一索引创建会拒绝，需要先审查并明确处理这些重复证据，不能静默删除。

写入以数据库原子insert/conflict/update保证并发首次采集不产生重复商品facet；更旧观测无论事实是否相同都不回滚当前状态。相同观测时间但事实变化可更新（兼容粗粒度采集时钟）；相同事实则仅严格较新观测刷新observed_at及task/workflow/lineage，不制造事实版本。ODP source_ts对商品优先采用观测时间，而不是冲突的published_at。SQLite临时库已由核心实现者验收迁移upgrade/downgrade及旧非商品重复身份不受影响；PostgreSQL仅编译DDL，不能当作真实PG迁移通过。

### eBay接口、凭据与实际执行位置

实际新增命令：

```text
opencli ebay search <q> --marketplace EBAY_US --environment production --limit 20 --offset 0 -f json
opencli ebay product "v1|123456789012|0" --marketplace EBAY_US --environment production -f json
```

示例ID为结构示例，不是真实可用商品。命令调用固定官方Browse接口：GET `/buy/browse/v1/item_summary/search`、GET `/buy/browse/v1/item/{item_id}`，client-credentials OAuth与公开数据scope；保留真实API字段（如itemId、itemWebUrl、price.value/currency）和来源时间。首批只接受`EBAY_US`；environment只接受production/sandbox，各自使用不同官方host、凭据及身份命名空间。

search **一次调用只取一页**；limit为1—200，offset为0—9999且必须是limit整数倍。非空结果带pagination（next/offset/limit/total），next须通过同环境官方host/path校验；合法零结果返回`[]`，不伪造商品。product必须给精确REST itemId并匹配返回的完整ID，包含变体为`0`的情况；不能只匹配listing而忽略variation。缺凭据、OAuth失败、401/403/429、错误body、异常结果结构、危险next、错listing/variant/环境都明确失败，日志不回显秘密。

四个可选服务端环境变量（`.env.example`均为空，不附任何真实值）：

| 环境 | client ID | client secret |
|---|---|---|
| production | `EBAY_CLIENT_ID` | `EBAY_CLIENT_SECRET` |
| sandbox | `EBAY_SANDBOX_CLIENT_ID` | `EBAY_SANDBOX_CLIENT_SECRET` |

**凭据只进入实际执行OpenCLI子进程的环境，不能写source/workflow参数或前端。** eBay声明browser:false，项目直执行路径运行于当前API主机或Celery worker主机，**不是所选浏览器Agent**。Docker api/worker已有`x-backend-common.env_file`继承项目`.env`，无需新增Compose键；本次未改Compose或启动部署。本地Pydantic Settings读取`.env`并不会填充`os.environ`，因此仅把键写入文件不保证CLI收到：需事先导出进程环境，或以 `uv run --env-file .env uvicorn backend.main:app ...` / `uv run --env-file .env celery ...` 启动对应执行进程（其余启动参数沿项目原命令）。本次未读取用户`.env`或真实凭据。

### 固定包完整性与可复现隔离验证

补丁工作流提供并核验的官方 `@jackwener/opencli@1.8.7` npm归档：

```text
filename: jackwener-opencli-1.8.7.tgz
SHA1: 1935c9c20fe208745a7e203386195cc0f3520ebb
SHA512 (base64): 2M+oPc70R1jNGzKzNrsm3fN4/gdvxCKlla7s9eaaTjkDjlzHpoZFN1YdV01A185kwCTN/ChOg+rbO4epO73c3w==
```

补丁已核对**10个上游目标文件**的固定基线及补丁后receipt完整性。最终联合验证从官方归档解出代码到pytest临时npm prefix，依赖则来自下述CI provision创建的另一个完整临时package；不再借用全局安装。**22项补丁测试全部实际运行**：真实CLI discovery/重复patch、fresh与receipt路径未知源码拒绝且不写入、Amazon与通用数字边界、原生Coupang search/product函数及变体保留、淘宝真实提取器，以及真实CLI内拦截fetch的eBay成功/错误边界。HTTP响应是fixture，**不是远端eBay调用**。

可在PowerShell从仓库根复现（先具备Node、完整OpenCLI1.8.7依赖安装；以下仅下载公开npm归档与写临时目录，不需要任何商家凭据）：

```powershell
$baseline = Join-Path ([IO.Path]::GetTempPath()) ("opencli-product-baseline-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $baseline | Out-Null
npm pack @jackwener/opencli@1.8.7 --pack-destination $baseline
$env:OPENCLI_TEST_ARCHIVE = Join-Path $baseline "jackwener-opencli-1.8.7.tgz"
# 此路径只提供node_modules依赖，测试代码仍取自上面的官方归档。
$env:OPENCLI_TEST_PACKAGE = Join-Path ((npm root -g).Trim()) "@jackwener/opencli"
uv sync --frozen --extra dev
uv run --no-sync python -m pytest tests/unit/pipeline tests/unit/odp tests/unit/channels/test_opencli_channel.py tests/unit/test_opencli_adapter_nodes.py tests/integration/test_opencli_channel_api.py tests/unit/test_ecommerce_opencli_patch.py --no-cov -q
```

运行前应核对归档完整性；也可把两个环境变量指向已有可信归档/依赖安装而跳过下载。**显式给出测试路径后，缺Node、路径无效、非pristine安装等前提错误必须fail，不能静默skip；最终22项补丁测试均执行。** 未提供显式fixture的普通本地运行仍可因无安装而skip，但不能计作验收。本次项目`.venv`起初缺dev pytest，裸`uv run pytest`误用外部pytest导致看似缺`python_calamine`；`uv sync --frozen --extra dev`成功安装锁定开发依赖后，统一`python -m pytest`绑定项目解释器。没有改锁文件或用生产代码绕过依赖错误。

CI补齐：`.github/workflows/ci.yml` 的Backend Quality在pytest前下载并SHA512校验同一官方归档，在`RUNNER_TEMP`解包；仅把原包运行时`dependencies`投影到另一个临时manifest安装（`--omit=dev --ignore-scripts --package-lock=false`），然后复制到解包目录的`package/node_modules`，通过`GITHUB_ENV`设置两个测试路径。这样没有依赖全局OpenCLI，也避免普通npm prefix提升依赖后测试找不到嵌套node_modules。网络只发生在provision阶段，不在pytest；测试继续修改自己的隔离副本。实际在本地执行**该CI step原Python代码**，SHA512通过、安装17个运行时依赖、导出路径成功，exit0；未修改项目/global npm或锁文件。初次直接对原包`npm install --omit=dev`仍解析其开发peer图并触发npm10 Arborist `edgesOut`错误，最终运行时专用临时manifest路径已实际成功，不改官方包源码或manifest来掩盖错误。

审查修复后的**最终唯一联合gate：369 passed、1 skipped、100 warnings，exit 0**，pytest用时236.50秒（输出证据`artifact://171`）。其中22项patch功能测试全部执行，且两个显式fixture路径都指向实际CI provision产生的干净临时目录。此前354/1（`artifact://133`）是审查前历史结果，不作为最终计数。唯一skip仍为 `tests/unit/pipeline/test_db_cursor_store.py` 的可选PostgreSQL跨后端检查，缺PG URL。warnings为既有Pydantic/pytest-asyncio弃用及AsyncMock未await提示。定向验证使用`--no-cov`，没有跑无关全应用suite，也不宣称完整GitHub Actions CI已经运行。

最终gate同时覆盖审查发现的商品证据冲突与时序边界：Amazon缺市场证据/所有来源URL市场冲突、Coupang重复或冲突变体及禁止借请求填充未观察变体、JD非商品页/无内容拒绝；非商品任意`_ecommerce`字典仍是普通数据；review/qa跟踪链接不制造事实版本。三个并发首次SQLite写入最终仅一条最新facet；更旧观测不回滚、同时间变价可更新、新观测更新task/workflow/lineage、同batch三输入计为一条最终记录加两条跳过。PG路径改为插入冲突后行锁下分类再一次更新，已编译相应锁语句，但没有真实PG并发验收。

本地provision实际调用形式如下（`ci_code`就是当前工作流同名step的Python正文，未改写再运行；本地已成功，不等于GitHub托管runner已执行）：

```python
import os
import subprocess
import tempfile
from pathlib import Path
import yaml

temp_root = Path(tempfile.mkdtemp(prefix="opencli-ci-provision-"))
workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
step = next(s["run"] for s in workflow["jobs"]["backend"]["steps"]
            if s.get("name") == "Provision isolated OpenCLI patch-test baseline")
ci_code = "\n".join(step.splitlines()[1:-1])
env = {**os.environ, "RUNNER_TEMP": str(temp_root),
       "GITHUB_ENV": str(temp_root / "github-env")}
subprocess.run(["uv", "run", "--no-sync", "python", "-c", ci_code],
               env=env, check=True)
```

### 实际SQLite与CLI smoke证据及尚未验收项

另实际执行独立`uv run --no-sync python -c`程序，创建内存SQLite全部表和临时source/task，运行真实adapt_items→LegacyDbSink→normalizer/storer→SQL读回，`forward_to_odp=False`：

```json
{"initial_accepted":2,"price_update_accepted":1,"observation_duplicates":1,"stored_rows":3,"distinct_identity_keys":3,"products":[{"price":19.99,"observed_at":"2026-09-05T00:00:00Z"},{"price":29.99,"observed_at":"2026-09-07T00:00:00Z"}]}
```

exit0、stderr空；两条同标题不同ASIN商品都保存，价格变化更新原实体，单纯时间变化不增版本且观测时间刷新，offer另占一个facet记录。七平台均有真实隔离SQLite价格更新/重复读回回归，ODP mapper/wire也已执行。原第5节失败fixture对应的修复后合同已有永久行为回归，不删除历史失败证据。

**没有真实Amazon/Coupang等浏览器访问、没有真实eBay授权/生产准入或API调用、没有ODP Rust数据库验收、没有账号写/购物/发布/支付、没有全局npm修改或共享部署。** 对应实站前提和只读验收仍按第6节执行；不能将受控CLI/SQLite成功提升为平台线上成功。

### 验证临时资源清理

最终证据保存后，已删除本次明确自有的两个CI provision临时根（一次失败安装、一次成功安装）及原补丁工作流的官方归档/运行时临时根，三个路径均确认不存在。研究/SQLite smoke脚本均在内存执行，仓库中没有本工作流遗留的一次性脚本或临时数据库。没有清理普通pytest缓存、用户数据库、全局npm、用户锁文件或不确定归属文件；测试夹具生成的普通pytest临时目录不作扩大清理。
