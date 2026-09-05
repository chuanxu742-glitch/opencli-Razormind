# 电商平台支持清单：本地实现与运行时证据

核查与更新日期：2026-09-05。下面“当前七平台交付状态”记录已实现代码和隔离验证；其后的原始调研章节保留当时未修复、未安装新补丁的历史事实。没有真实电商登录/抓取/加购/发布/聊天或共享部署操作。本文不是网络主流平台排名。

## 当前七平台交付状态

本批批准并实现的范围为**淘宝、京东、1688、闲鱼、Amazon、Coupang、eBay**。商品接入由 `backend/channels/ecommerce.py` 复用 OpenCLI 通道与现有 sink；`scripts/patch-opencli.js` 对固定 OpenCLI 1.8.7 安装施加自包含补丁并注册 eBay 命令。**代码存在和隔离验收通过，不等于本机全局安装/所有节点已部署，更不等于实站通过。** 完整合同、凭据运行位置、版本完整性和复现命令见 [准备度报告第8节](international-ecommerce-adapter-readiness.md#8-实施与验证增补2026-09-05)。

| 平台 | 当前商品链与已修复边界 | 验证/限制 |
|---|---|---|
| 淘宝 | search；detail 的 field/value 整批聚合；item_id身份；受控补丁按商品身份去重，避免同标题不同ID上游丢卡 | 真实提取器受控DOM与隔离存储通过；非全量评论、非完整天猫适配 |
| 京东 | search、detail批聚合、item；保留SKU，请求ID与页面冲突明确失败 | 隔离存储/身份回归通过；未实站验收图片和页面 |
| 1688 | search/item；offer_id、价格阶梯、MOQ、卖家等事实保留 | 阶梯变价持久化回归通过；不压成无数量条件的单价 |
| 闲鱼 `xianyu` | search/item；item_id、item_url和description等事实映射 | 隔离更新回归通过；未操作聊天、发布、回复 |
| Amazon | search/product/offer/discussion及榜单商品适配；US优先；修复国家URL、明确市场币种和数字格式边界 | 真实安装helper受控fixture通过；discussion仍仅摘要/样本，offer非所有卖家列表；不宣称各国家站实站通过 |
| Coupang | search/product；保留itemId/vendorItemId变体URL，product_id与变体共同参与身份 | 补丁helper和隔离持久化通过；无评论正文，不执行加购 |
| eBay | **新增真实 Browse API search/product**，client-credentials，只读、browser:false；首批仅EBAY_US，production/sandbox分离 | 临时官方包中实际CLI命令发现及受控HTTP成功/错误边界通过；未提供真实凭据、未调用真实eBay API |

七平台共同支持source内entity+facet当前状态：不同商品不因同标题碰撞，价格事实变化可更新，只变观测时间不制造事实版本；跨source保留独立证据，非商品/社交命令语义不变。商品特有字段现在位于 `normalized_data.ecommerce.facts`，没有继续保留旧商品 `extra_*` 兼容别名；仓库内消费者已核查，保存过的/外部表达式可能需要更新。历史记录不批量迁移，重新采集按新合同写入。

最终联合验证：**369 passed、1 skipped**（`artifact://171`），其中补丁22项全部执行；唯一skip为未配置PostgreSQL的可选跨后端检查。CI的官方包校验/临时嵌套依赖provision已在本地实际执行，不宣称完整GitHub CI通过。新版写入需升级商品部分唯一索引迁移 `s8t9u0v1w2x3`，详见准备度报告。未验证真实浏览器/eBay授权/ODP Rust实库；未修改全局npm、用户锁文件或共享部署。

**本批明确不实现、不计入已支持：AliExpress、Shopee、Lazada、Temu、TikTok Shop、Walmart。** eBay从下方历史B层提升为本批新代码适配；Shopify/WooCommerce等通用pack仍不能据文案计入七平台交付。

## 以下为修复前原始盘点（历史证据）

## 结论与支持层级

此前 `ecommerce-adapter-recon.md` 的“电商侧仅淘宝关键词搜索”只适用于 **BrowserAct manifest**，不能代表整个项目。

- **A：已集成命令/manifest**：项目执行通道能够识别、路由的能力。本次实机 OpenCLI catalog 有淘宝、京东、1688、闲鱼、Amazon、Coupang 的商品能力；另外有 Facebook Marketplace 的卖家列表/会话读取、什么值得买好价搜索。BrowserAct 电商 manifest 仍只有淘宝关键词搜索。
- **B：仅 vendored 脚本**：代码随仓库存在，但无 BrowserAct channel manifest；不等于该 channel 可直接执行。包括淘宝/天猫详情、评价、店铺，闲鱼脚本，Amazon 云 API 脚本，以及通用 pack 内的 eBay/Shopify/WooCommerce 提取策略。
- **C：声明或依赖能力未验收**：文案中的“适用于某平台”、可覆盖的外部依赖/远端节点、未检查的部署版本，不能提升成实站可用。本文的本机 OpenCLI 能力已经检查实际安装源与 catalog，不是仅凭 npm 包名猜测；但仍未做登录抓取验收。
- **D：已检查实现中无专用商品能力**：拼多多、Shopee、Lazada、Temu，以及抖音商城/快手电商/小红书店铺；AliExpress 只有通用 pack 文案声明，未见专用实现。**eBay 不属于完全无代码：通用 vendored pack 确实有专用选择器。**

“支持”在这里表示有对应实现/命令，不表示已经在真实网站取到正确数据，也不表示支持交易、订单、支付或商家后台全部业务。

## 1. 版本、安装与动态接入边界

### 实机依赖

实际 `where.exe opencli` 返回：

```text
C:\Users\32536\AppData\Roaming\npm\opencli
C:\Users\32536\AppData\Roaming\npm\opencli.cmd
```

`opencli.cmd:17` 指向 `node_modules/@jackwener/opencli/dist/src/main.js`。

本文以 **`N = C:/Users/32536/AppData/Roaming/npm/node_modules/@jackwener/opencli`** 表示本机安装根；读取 `N/package.json:1-15` 得到包名 `@jackwener/opencli`、版本 `1.8.7`、bin `dist/src/main.js`；`opencli --version` 实际输出 `1.8.7`，退出码 0。

`C:/Users/32536/.opencli/clis` 实际为空。仓库 `integrations/opencli/` 只有 FMHY 集成，不是电商适配器集合。所列电商实现来自 `N/clis/`。这里确认的是**本机安装文件**，没有对它们与 npm 发布 tarball 做逐字节一致性证明，也不把本机 catalog 自动等同于每个生产节点。

### 仓库部署声明（与实机事实分开）

- `Dockerfile:31-36`、`agent/Dockerfile:71-77`、`chrome/Dockerfile:7-16`：默认安装 `@jackwener/opencli@1.8.7`，并运行 `scripts/patch-opencli.js`。Docker ARG 可以被构建者覆盖。
- `scripts/install-managed-opencli.ps1:11-14,26-41`：OpenCLI `1.8.7`，OhMyOpenCLI 与 capability source commit 均固定为 `73cc60c83586ef2c95469b3b70d6cfc80fa5bc53`。OhMyOpenCLI repo URL 由安装参数传入，并非本次读取的另一个源树。
- `iii/workers/collector-opencli/Dockerfile:7`：`npm install -g @jackwener/opencli` **未固定版本**，不能按本机 1.8.7 推断其最终 catalog。
- `backend/opencli_runtime.py::configured_opencli_bin/resolve_opencli_bin`：允许 `OPENCLI_BIN` 覆盖，否则 PATH 查找 `opencli`。

### 后端是动态 catalog，不是固定电商白名单

- `backend/workflow/opencli_adapter_nodes.py::_load_opencli_catalog`（298-343）：执行 `[bin_path, "list", "-f", "json"]`，解析返回数组，要求条目有 `site`/`name`。未按静态“允许平台表”筛选电商站点。失败返回空 catalog。
- `backend/channels/opencli_channel.py::OpenCLIChannel.collect`（642-698）：从 config 读取 `site`、`command`、`args`，查询真实 CLI help/options 和 browser 要求，构建命令；浏览器命令再由浏览器池/agent 选路。不是逐站硬编码执行类。
- `backend/channels/registry.py::_load_all_channels` 注册通用 `opencli`、`browser_act` 等通道，以及专用 `douyin_detail`，不维护淘宝/京东等站点白名单。
- **不要混淆另一套 managed acquisition**：`backend/acquisition/registry.py::_REGISTRATIONS`（39-62）当前审计注册的只有 `official-site.observe`。这是 managed-acquisition capability 范围，不是通用 OpenCLI adapter catalog 的全局站点上限。
- `frontend/lib/plugins/opencli-adapter-catalog.ts` 中 `jd` 标签及 `1688/amazon/taobao` 分类是展示信息，本身不能证明运行能力；本文以 CLI catalog 和安装源为依据。

## 2. 已发现的电商 CLI 命令（完整列出本节平台，不展开全部 1332 条）

下表命令均实见于本次 `opencli list -f json` 输出。形式为 `opencli <site> <command> ... -f json`。所有这些电商命令的 catalog 均声明 `strategy=cookie`、`browser=true`，需要相应浏览器运行环境/会话；不是 BrowserAct 云 API key 模板。

| 平台 / site | 读取类命令 | 写入/认证类命令（本次未执行） | 实现与能力边界 |
|---|---|---|---|
| 淘宝 `taobao` | `search`, `detail`, `reviews`, `cart`, `whoami` | `add-cart`, `login` | 搜索、详情、评论、购物车；不含订单/支付命令 |
| 京东 `jd` | `search`, `detail`, `item`, `reviews`, `cart`, `whoami` | `add-cart`, `login` | `item` 另提供规格、主图/详情图等，不能漏掉它与 `detail` 的区别 |
| 1688 `1688` | `search`, `item`, `store`, `assets`, `download`, `whoami` | `login` | 商品搜索/详情、供应商店铺、图片视频素材与下载；`download` catalog 标 read，但会写本地文件，本次未执行 |
| 闲鱼 `xianyu` | `search`, `item`, `inbox`, `messages`, `whoami` | `chat`, `publish`, `reply`, `login` | 正式 site 名是 `xianyu`；目标域是 `goofish.com`，无 `goofish` site 不代表不支持闲鱼 |
| Amazon `amazon` | `search`, `product`, `offer`, `discussion`, `bestsellers`, `new-releases`, `movers-shakers`, `whoami` | `login` | 商品/offer/评价讨论与榜单；安装源 shared 常量以 amazon.com 为主，不据此宣称所有国家站已验收 |
| Coupang `coupang` | `search`, `product`, `whoami` | `add-to-cart`, `login` | 韩国电商商品搜索/详情与加购；不是 BrowserAct pack |
| Facebook Marketplace `facebook` | `marketplace-listings`, `marketplace-inbox` | 此处不把 Facebook 其他社交写入命令算电商能力 | 自己的卖家 listing、买卖双方会话；非全站商品搜索/下单 |
| 什么值得买 `smzdm` | `search` | 无该站其他 catalog 命令 | 好价聚合/导购，输出价格、商城、更新时间、互动；不是交易商城 |

### 对应真实源码路径/策略

以上 `N` 是本机安装根，不是仓库相对路径：

- 淘宝：`N/clis/taobao/search.js:18-28` 导航 `s.taobao.com/search` 并 DOM 提取；`detail.js:15-21` 导航 `item.taobao.com/item.htm`；`reviews.js:16-40` 使用淘宝页面，并引用 `rate.tmall.com/list_detail_rate.htm`。其余命令为同目录 `cart.js/add-cart.js/auth.js`。
- 京东：`N/clis/jd/search.js:16-22`、`detail.js:15-19`、`reviews.js:16-24` 是浏览器页面读取；`item.js:436-443,548-627` 结合 DOM/页面状态/资源及带 credentials 的价格接口。不是要求外部商业 API key 的接口。
- 1688：`N/clis/1688/search.js::readSearchPayload/collectSearchRows`、`item.js::readItemPayload`、`store.js::readStorePayload`：DOM/页面 `window.context`，调用 `assertAuthenticatedState`；`shared.js:2-7` 定义 1688 URL；`assets.js::readAssetsPayload`、`download.js` 提供素材链。
- 闲鱼：`N/clis/xianyu/search.js::buildSearchUrl/buildSearchEvaluate`（11-12,72-95）在 Goofish 页面内调用 `window.lib.mtop.request` / `mtop.taobao.idlemtopsearch.pc.search`；`item.js::buildFetchItemEvaluate`（25-49）调用 `mtop.taobao.idle.pc.detail`。是浏览器会话内站点接口，不是独立无认证开放 API。会识别登录/验证码页面。
- Amazon：`N/clis/amazon/search.js::readSearchPayload`、`product.js::readProductPayload`、`offer.js::readOfferPayload`、`discussion.js::readDiscussionPayload`、`rankings.js::createRankingCliOptions`：浏览器 DOM；`shared.js:2-11` 给出 URL 和 cookie strategy。**此路径不依赖 vendored Amazon Python 的 `BROWSERACT_API_KEY`。**
- Coupang：`N/clis/coupang/search.js:400-455`、`product.js:191-224`：cookie/browser，页面导航与 evaluate；搜索脚本亦尝试会话内 `fetch(...,{credentials:'include'})`。
- Facebook：`N/clis/facebook/marketplace-listings.js:12-29` 导航 `/marketplace/you/selling/`；`marketplace-inbox.js:12-29` 导航 `/marketplace/inbox/`，均 DOM。
- 什么值得买：`N/clis/smzdm/search.js:128-145`，导航 `search.smzdm.com` 并执行 `buildSmzdmSearchJs`。

### 天猫必须单独限定

没有独立 `tmall` CLI catalog site。淘宝命令源以淘宝 URL 为入口，评论脚本引用天猫评分接口，但这**不足以证明完整天猫商品/店铺适配**。BrowserAct 淘宝搜索 pack 声称搜索淘宝和天猫；其 manifest 入口仅 `s.taobao.com`。天猫详情的明确专用证据来自下述 vendored `extract-product.py` 的 `isTmall` 分支及 skill 示例，应列 B 层，而非单列已验收 `tmall` 命令。

## 3. BrowserAct：78 packs，不是 78 个可执行 channel

实际运行 `PackCatalog().list_packs()`：总计 **78**，`domain=ecommerce` **19**；递归查找并 `load_manifest` 校验得到 **2** 个 manifest：

```text
search-research/google-search-serp/channel.manifest.json: google-search-serp
ecommerce/taobao-keyword-search/channel.manifest.json: taobao-keyword-search
```

`backend/browser_act_packs/catalog.py::PackCatalog._scan`（108-150）按 `SKILL.md` 建目录；`backend/channels/browser_act_channel.py::_load_pack_manifest`（207-227）明确拒绝缺失 `channel.manifest.json` 的 pack。出现在 catalog 不等于可由 channel 执行。

### 已接入 manifest

`backend/browser_act_packs/ecommerce/taobao-keyword-search/channel.manifest.json:1-15`：`keyword` 必填；navigate → wait stable → eval `scripts/search-products.py`，按 page 翻页，成功要求 `itemId`。此前报告记录的 sort 生成未生效、商品 URL/身份规范化问题仍仅是此前探针结论，本次未重新运行也未修复。不能因 manifest 存在宣称稳定入库。

### B 层专用脚本（均无 channel manifest）

根 `P = backend/browser_act_packs/ecommerce/`：

- 淘宝/天猫：`taobao-product-detail/scripts/extract-product.py`（14-18 有 `location.hostname` 的 `isTmall`）、`taobao-product-reviews/scripts/extract-reviews.py` 和 `next-review-page.py`、`taobao-shop-catalog/scripts/extract-catalog.py`。为生成浏览器 DOM JavaScript 的 Python 脚本。
- 闲鱼：`goofish-search-list/scripts/extract-search-items.py`、`apply-search-filters.py`、`goto-page.py`；`goofish-item-detail/scripts/extract-item-detail.py`（12-24 识别删除/验证码/未加载，后续 DOM 商品/卖家/图片）。
- Amazon Alexa/Rufus：`amazon-alexa-qa/scripts/check-alexa-panel.py`、`inject-question.py`、`extract-response.py`，DOM 交互脚本；`SKILL.md:18-21` 要求 Amazon 已登录。
- Amazon 云 API：下表脚本全部调用 `https://api.browseract.com/v2/workflow` 的 template task API，以 Bearer key 鉴权并轮询，需 **`BROWSERACT_API_KEY`**（不是 Amazon 官方 API key）。没有 manifest；没有在本次向远端发起请求。

| P 下脚本路径 | 主要声明 |
|---|---|
| `amazon-asin-lookup-api-skill/scripts/amazon_asin_lookup_api.py` | ASIN 详情 |
| `amazon-best-selling-products-finder-api-skill/scripts/amazon_best_selling_products_finder_api.py` | 关键词畅销商品 |
| `amazon-buy-box-monitor-api-skill/scripts/amazon_buy_box_monitor_api.py` | Buy Box/卖家 |
| `amazon-competitor-analyzer/amazon_competitor_analyzer.py` | ASIN 竞品分析 |
| `amazon-listing-competitor-analysis-skill/scripts/amazon_listing_competitor_analysis.py` | listing 竞品提取 |
| `amazon-product-api-skill/scripts/amazon_product_api.py` | 商品搜索 |
| `amazon-product-search-api-skill/scripts/amazon_product_search_api.py` | 商品搜索 |
| `amazon-reviews-api-skill/scripts/amazon_reviews_api.py` | 评价 |

### 通用 packs：代码支持与文案宣称不能混为一谈

剩余 4 个电商 pack 是 `ecommerce-listing`、`ecommerce-product-detail`、`ecommerce-reviews`、`ecommerce-seller-info`，均无 manifest。

- **eBay：有实际专用 vendored 策略，B 层。** `ecommerce-listing/scripts/extract-listing.py:53-69` 的 `.s-item` 卡片；`extract-listing-next-page.py:21-23` 的 eBay 翻页；`ecommerce-seller-info/scripts/extract-seller.py:49-57` 的 eBay 卖家/好评率。无 eBay OpenCLI 命令，未实站验证。
- **Shopify / WooCommerce：B 层通用脚本策略。** listing 脚本 71-98 为 WooCommerce 商品 grid、Shopify/Product JSON-LD；product-detail 脚本 54-64 为 `window.ShopifyAnalytics.meta.product`。不是接入 Shopify 管理 API。
- **AliExpress / Alibaba 国际站 / Walmart / Etsy / Target / Best Buy / Rakuten / Magento / BigCommerce / PrestaShop** 出现在 `ecommerce-product-detail/SKILL.md:3` 的广泛适用声明；其通用 JSON-LD/DOM fallback 不能当作各站专用 adapter 或实站成功。`extract-product.py:15-51` 证明通用 schema.org/Product 读取，不证明这些站点今日能返回该结构。将其记为 C 层声明/通用候选，不算“已接入平台”。
- 通用 listing 文案另列 Google Shopping 等，也不等于 Google Shopping 专用 manifest。普通 Google SERP manifest 不应计作商品搜索适配。

## 4. 要求检查的平台矩阵与社交边界

| 平台 | 本次最终归类 |
|---|---|
| 淘宝 | A：OpenCLI 商品命令 + BrowserAct 搜索 manifest；B：其余 BrowserAct 脚本 |
| 天猫 | B：明确 vendored 详情/评论/店铺声明与脚本；淘宝搜索路径关联，独立 tmall CLI 不存在，完整覆盖未验证 |
| 京东 | A：本机 OpenCLI；无 BrowserAct 专用 pack |
| 1688 | A：本机 OpenCLI；不等于 Alibaba 国际站 |
| 闲鱼/Goofish | A：`xianyu` OpenCLI；B：Goofish BrowserAct 脚本 |
| Amazon | A：本机 OpenCLI DOM 路径；B：云 API/Alexa 及通用 BrowserAct 脚本 |
| eBay | B：通用 pack 中确有 eBay 列表、翻页、卖家策略；无 CLI/manifest |
| AliExpress | C：通用 detail 文案宣称；无专用 CLI/manifest |
| 拼多多 | D：未发现商品搜索/详情/店铺采集；另有 TurboPush **视频发布**平台声明，不能算电商采集 |
| Shopee、Lazada、Temu | D：已检查 backend/integrations/安装 clis 中未发现专用商品实现，CLI catalog 无这些站点 |
| 抖音 | 有社交/视频与创作者能力，无已发现商城商品/订单/店铺命令 |
| 快手 | 无 OpenCLI catalog site；有 TurboPush 图文/视频发布平台声明，无已发现商品采集 |
| 小红书 | 有笔记/评论/创作者能力，无已发现店铺商品/订单命令 |

抖音实际 CLI 全部命令：`activities, collections, delete, draft, drafts, hashtag, location, login, profile, publish, search, stats, update, user-videos, videos, whoami`。其中 search 描述为“关键词搜索抖音视频”，stats 为“单个作品的创作者中心深层指标”。仓库 `backend/channels/douyin_detail_channel.py::DouyinDetailChannel.collect`（140-178）请求 `/aweme/v1/web/aweme/detail/`，明确是视频详情，不是商品详情。

小红书实际 CLI 全部命令：`ask, comments, creator-note-detail, creator-notes, creator-notes-summary, creator-profile, creator-stats, delete-note, download, draft-clear, draft-delete, draft-open, drafts, feed, follow, liked, login, note, notifications, publish, saved, search, unfollow, user, whoami`。catalog 对 creator 系列的描述均为账号/笔记/观看互动指标；不能以平台有电商业务为由推定这些命令提供电商数据。

`backend/workflow/turbopush_runtime.py::TURBOPUSH_PLATFORMS`（79-92）列小红书/抖音/快手图文和视频、拼多多 video；`resolve_turbopush_service_credentials`（163-174）需要本地 TurboPush 服务端口与 auth。`backend/workflow/turbopush_accounts.py:107-110,149,255-263` 也有快手/拼多多配置与别名。这些是**内容发布目标**而不是商品源。未检查其本机服务/登录状态，亦未执行发布。

## 5. 实际执行的只读探针与限制

| 实际命令/动作 | 结果 |
|---|---|
| `npm root -g` | `C:\Users\32536\AppData\Roaming\npm\node_modules`，退出 0 |
| `where.exe opencli` | 见上文两条 launcher 路径，退出 0 |
| `opencli --version` | `1.8.7`，退出 0 |
| `opencli list -f json` | 退出 0；回收完整 JSON 后解析 **1332 条命令 / 176 个 site**；本报告保存电商相关紧凑清单，不复制完整输出 |
| `uv run --no-sync python -c "from backend.browser_act_packs.catalog import PackCatalog; from backend.browser_act_packs.manifest import load_manifest; c=PackCatalog(); p=c.list_packs(); m=list(c.root.rglob('channel.manifest.json')); print('packs=',len(p),'ecommerce=',sum(x.domain=='ecommerce' for x in p),'manifests=',len(m)); print('\\n'.join(str(x.relative_to(c.root))+': '+load_manifest(x).capability for x in m))"` | 退出 0；`packs= 78 ecommerce= 19 manifests= 2`；两个 schema 校验成功的 manifest 如上 |
| 源码/目录只读核查 | 本机 `N/clis` 对应商品脚本、空用户 overlay、仓库 channels/registries/部署版本及全部 19 个 ecommerce pack 的种类边界 |

未进行任何真实电商访问、账号动作、第三方收费 API 调用或订单操作。`list` 成功只证明 catalog 能加载；cookie/browser 标记也不证明登录有效、反爬可通过、DOM 未变、分页完整或数据可正确入库。此前已知缺少可用 BrowserAct 环境，本次没有为了重复确认而重跑可用性检查。远端 agent、容器最终镜像和自定义 `OPENCLI_BIN` 的实际版本/能力仍需在对应执行节点检查，不能由本机报告替代。
