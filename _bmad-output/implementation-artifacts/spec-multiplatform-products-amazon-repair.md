---
title: 多平台商品采集与Amazon修复（首批交付提案）
type: feature
created: 2026-09-05
status: done
review_loop_iteration: 0
baseline_commit: caa50b62533b3dae76673d819a962ca3fcb1a744
context:
  - '{project-root}/docs/verification/international-ecommerce-adapter-readiness.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 用户要求“支持多平台然后亚马逊也进行修复”。现有商品同标题碰撞、价格变更遗漏，Amazon币种/格式错误；只有通用平台名单不算适配完成。

**Approach:** **待批准的首批七平台**：淘宝、京东、1688、闲鱼、Amazon、Coupang修复商品采集到存储语义，新增真实eBay Browse搜索/详情。此前国际九平台中的AliExpress、Shopee、Lazada、Temu、TikTok Shop、Walmart明确**本批不实现、不计支持**，保留后续目标，用户可选E扩大范围。不是悄然缩成Amazon单平台。

## Boundaries & Constraints

**Always:** 复用OpenCLI动态发现/运行、现有sink；商品身份与事实版本/观测分离。保留原始字段、1688阶梯价/MOQ、Coupang变体、缺失值。国家/币种有证据才赋值；金额不做跨币种换算。

**Ask First:** 扩大平台/命令/国家覆盖；需要商家授权、真实凭据或共享环境操作时另取授权。

**Never:** 修改全局npm作为交付；假接口、枚举冒充适配；改无关社交去重；交易/账号写操作；承诺全量评论/价格历史或九国际平台实站通过。

## I/O & Edge-Case Matrix

| 场景 | 输入 | 预期/错误 |
|---|---|---|
| 身份 | 同标题不同原生ID/marketplace/变体 | 独立entity；未知商品ID明确失败，不借标题造ID |
| 版本 | 同entity价格变、仅fetched_at变 | 前者更新，后者不生成事实版本 |
| facet | search/product/offer/discussion | 共享entity，分facet持久化，不以缺字段擦除其他facet；跨source保留独立证据 |
| shape | 淘宝/JD detail field/value批；JD item | 聚合成单商品；请求ID与页面身份冲突失败；评论行不冒充商品 |
| 金额 | USD、德式前后置EUR、CAD/JPY | 明确marketplace正确解析；歧义保留原文/null，非零价；US既有正确值不退化 |
| eBay | q或REST itemId、marketplace、环境 | search/detail返回真实API字段；缺凭据/401/403/429/错误body明确失败，不造成功空商品 |

</frozen-after-approval>

## Code Map

- `backend/channels/opencli_channel.py`：collect多返回分支均需商品适配；identity当前继承None。
- `backend/pipeline/normalizer.py`、`storer.py`：hash遗漏商品事实；现有identity更新复用。
- `backend/odp/mapper.py`：event_id为内容hash；payload可承载entity/facet/version，非商品wire不变。
- `scripts/patch-opencli.js`：已有幂等安装补丁；安装器只下载此单文件，新增适配须自包含分发。
- 详细shape及源码位置见context报告与 `docs/verification/ecommerce-platform-support.md`；eBay已取官方OpenAPI见下方。

## Tasks & Acceptance

**Execution:**
- [x] `backend/channels/ecommerce.py`（新增）、`backend/channels/opencli_channel.py`：按site+command适配七平台商品输出；统一entity、facet、URL、观测与原始数据；其他命令不变。
- [x] `backend/pipeline/normalizer.py`、`backend/pipeline/storer.py`、`backend/pipeline/sinks/legacy_db_sink.py`：商品hash排除抓取时钟/排名；source内entity+facet更新，facet不互覆；非商品契约不变。
- [x] `backend/odp/mapper.py`：商品稳定事实版本作为幂等事件、独立entity/facet和observed_at；ODP保留事实快照，不冒充跨source物化最新商品表。
- [x] `scripts/patch-opencli.js`：持久Amazon URL/金额/评分计数修复；修复淘宝search标题去重、Coupang变体URL丢失；自包含安装eBay search/product注册，固定官方host、client-credentials与scope，凭据只从环境读取、禁止日志泄漏；未知上游结构失败，不静默跳过。
- [x] `tests/unit/channels/test_opencli_channel.py`、`tests/unit/pipeline/test_normalizer.py`、`tests/unit/pipeline/test_storer.py`、`tests/unit/pipeline/test_legacy_db_sink.py`、`tests/unit/odp/test_mapper.py`：矩阵行为回归；新 `tests/unit/test_ecommerce_opencli_patch.py` 验实际patch/helper及eBay受控HTTP边界。
- [x] `backend/models/record.py`、`backend/migrations/versions/s8t9u0v1w2x3_add_product_facet_identity.py`：商品专属 source+entity/facet 部分唯一索引，正式 Alembic upgrade/downgrade；旧数据与非商品 identity 语义不改。
- [x] `.github/workflows/ci.yml`：真实官方包与嵌套运行时依赖在pytest前隔离provision，显式fixture错误fail而非skip；本地provision及最终369/1联合验收通过。

**Acceptance Criteria:**
- Given七平台有效输出，when经真实normalize/store读回，then不同实体不碰撞、价格变化更新、仅时间变化无新事实版本，原始阶梯价/变体保留。
- Given临时安装根，when实际patch两次并发现命令，thenAmazon修复与eBay命令可重复安装，全球npm未改。
- GiveneBay合法响应或错误，when执行search/product，then保留REST变体ID/货币/分页信息或明确失败；无凭据仅标未实站验收，不计线上通过。

## Spec Change Log

## Design Notes

执行所有权：主实施worker负责核心后端生产文件；独立patch worker负责 `scripts/patch-opencli.js` 及新增 `tests/unit/test_ecommerce_opencli_patch.py`；现有验证worker负责任务所列五个后端测试文件。文件所有权不交叉，全部完成后整体验证。

entity不含source；存储identity为entity+facet，source仍界定证据归属。legacy保存facet当前值；ODP以事实版本幂等，不因observed_at制造事件。旧记录不批量改写，重新采集按新契约写入，历史迁移不在本批。

审查确认的商品并发约束：legacy 写入采用商品部分唯一索引；INSERT 冲突后先锁定当前行（PostgreSQL SELECT FOR UPDATE；SQLite 由 INSERT 获得事务写锁），再统一判定哈希与新鲜度并执行一次更新。更旧观测不得回退当前 facet；同时间但事实变化允许更新，同事实仅更晚观测刷新。接受更新时同步任务/工作流/lineage，保留 created_at。模型及迁移由核心 worker 负责，CI 由补丁交付所有者协调；冻结意图未改，不要求历史数据重写。

eBay契约：`https://developer.ebay.com/develop/api/spec/browse_api.json` 已读取200（研究digests内 `ebay-browse-openapi.json`）。GET `/buy/browse/v1/item_summary/search`、`/item/{item_id}`；client-credentials/public-data scope、marketplace header、REST变体ID与next按原契约。首批EBAY_US，sandbox/production分离，生产准入由真实账号确认。

## Verification

最终验证已完成：官方 OpenCLI 1.8.7 归档与独立CI运行时依赖夹具下，broader backend + patch 联合命令 369 passed、1 skipped、100 warnings，exit0（artifact://171）；22项补丁测试全部执行，唯一skip为未配置PostgreSQL的可选跨后端检查。实际CI provision Python正文已本地执行成功，不宣称GitHub托管CI已运行。真实临时SQLite sink/readback、并发身份与新旧观测、ODP mapper/wire、实际CLI受控HTTP均通过；没有实站浏览器/eBay凭据调用、ODP Rust实库或共享部署。

部署需执行商品部分唯一索引迁移 s8t9u0v1w2x3；历史数据不批量改写，商品特有字段迁至 ecommerce.facts。临时CI/归档三个自有根已清理。冻结区域完整原始字节保持不变；沿原算法的SHA256仍为 b91124ff1762fc04eb073001d508f8ec6bf518e21472b247372fd2ee0ef73097（完整含标签区域raw/LF SHA256为2dc4be6000dd61dfefeacc959405a4d0372bf3141a06da6b7a48f4e1fc5dd344）。

未设置story_key，按规则跳过sprint状态同步。

## Suggested Review Order

**入口与证据边界**

- 按实际命令接入商品，保留非商品语义
  [`opencli_channel.py:647`](../../backend/channels/opencli_channel.py#L647)

- 严格核对平台、市场、原生身份与变体
  [`ecommerce.py:111`](../../backend/channels/ecommerce.py#L111)

**事实与持久化**

- 分离商品事实版本和观测时间
  [`normalizer.py:70`](../../backend/pipeline/normalizer.py#L70)

- 行锁下分类，原子维护最新商品facet
  [`storer.py:69`](../../backend/pipeline/storer.py#L69)

- 仅商品身份新增部分唯一索引
  [`s8t9u0v1w2x3_add_product_facet_identity.py:17`](../../backend/migrations/versions/s8t9u0v1w2x3_add_product_facet_identity.py#L17)

- 商品事件优先采用观测时间
  [`mapper.py:48`](../../backend/odp/mapper.py#L48)

**受控运行时**

- 固定上游基线，安装真实只读电商命令
  [`patch-opencli.js:33`](../../scripts/patch-opencli.js#L33)

**验证与部署**

- 真实SQLite回归覆盖七平台更新
  [`test_legacy_db_sink.py:41`](../../tests/unit/pipeline/test_legacy_db_sink.py#L41)

- 真实安装补丁与CLI验收
  [`test_ecommerce_opencli_patch.py:86`](../../tests/unit/test_ecommerce_opencli_patch.py#L86)

- CI下载校验官方包并配置必跑夹具
  [`ci.yml:181`](../../.github/workflows/ci.yml#L181)

- 凭据仅在执行主机环境配置
  [`.env.example:167`](../../.env.example#L167)

- 核对最终证据、迁移要求与未验收范围
  [`international-ecommerce-adapter-readiness.md:205`](../../docs/verification/international-ecommerce-adapter-readiness.md#L205)
