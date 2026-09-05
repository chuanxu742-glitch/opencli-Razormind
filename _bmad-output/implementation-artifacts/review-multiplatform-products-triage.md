# 多平台商品变更：已接受审查问题与修复分工

本清单记录已接受的冻结合同内实现缺陷，不改写意图或扩大平台范围。核心、原生补丁、验证与CI修复均已完成；最终联合369 passed、1 skipped（artifact://171），spec已完成。不列被拒绝的审查噪声。

| 编号 | 严重性 | 已接受问题 | 修复动作 / 验收证据 |
|---|---|---|---|
| R1 | 高 | Amazon 缺失市场证据时擅自补 US；多个 URL 市场冲突未完整校验 | 汇总全部观测 URL、source_url 和请求市场。没有证据则失败；只有显式裸 ASIN 请求允许采用命令已有 US 默认。跨市场同 ASIN 不能合并。添加无证据、请求默认和多 URL 冲突回归。 |
| R2 | 高 | Coupang 变体来源冲突、重复参数，以及用请求变体补齐未知页面身份 | 后端比较全部观测 URL/页面 URL/显式变体字段；拒绝重复参数和冲突。请求变体只能核对，不能补造缺失观测。patch worker 同步修正原生 search/product 输出，使 URL 保留真实页面变体。 |
| R3 | 高 | JD URL 看似有效但页面非商品/受阻时仍可覆盖商品 facet | 按真实上游 pageState.isProductPage 与已存在阻断字段判定成功；不得只靠地址推断商品页。回归正常真实 shape 与验证码/登录/非商品页。 |
| R4 | 中 | 普通记录自带同名 _ecommerce 字段会异常或被误分类 | 在 identity/snapshot 共用完整内部元数据识别条件；无完整合法 shape 则保留原非商品 extra_* 语义，不抛 KeyError，不赋商品身份。 |
| R5 | 中 | Amazon review_url/qa_url 跟踪变化制造事实版本 | 将明确的观测链接排除事实摘要，原始字段完整保留；真实商品事实变化仍改变版本。添加仅链接跟踪变化回归。 |
| R6 | 高 | 旧观测可覆盖新商品状态，且最新任务/工作流/lineage 与事实不一致 | 商品更新采用数据库原子新鲜度条件；严格更旧观测不覆盖。时间相同但事实变化仍允许更新，避免上游低精度时间遗漏价格变化；事实相同只有更晚观测才刷新。接受更新时同步 raw/normalized/task_id/workflow_id/workflow_run_id/lineage，保留初始 created_at。只在事实改变时重置富化状态。RETURNING 刷新已有 ORM 实例，真实隔离 DB 读回验证。 |
| R7 | 高 | 并发首次插入相同 source/entity/facet 的不同价格可产生两条当前行 | 为商品 JSON 元数据建立 SQLite/Postgres 部分唯一索引（source_id, identity_key），保留非商品原有语义；新增正规的 Alembic 索引迁移，不迁移/删除历史数据。商品走 INSERT ON CONFLICT DO NOTHING + 原子条件更新，冲突后立即判定/写入，不依赖下一次采集。真实隔离并发持久化与迁移验证。 |
| R8 | 中 | 商品 ODP source_ts 优先级缺少直接验证 | 验证 worker 增加 observed_at 优先于冲突 published_at，以及没有 published_at 时仍使用 observed_at 的 wire 断言。 |

## 持久化设计约束

- 部分唯一索引只覆盖 normalized_data 中存在 ecommerce.entity_id 的商品行；普通记录和旧记录不改写。
- 商品 observed_at 规范为 UTC 固定微秒表示，原始 fetched_at 留在 raw_data；数据库比较同一表示，禁止旧观测回退当前 facet。
- 保持 ODP 事实快照模型：旧事实事件仍可作为独立证据入 ODP，legacy 当前状态不回退；不新增跨 source 最新商品表。
- 非商品现有内容哈希/可选 identity 行为不改；商品原子写入在原非商品 flush/recovery 后执行，避免非商品重试回滚已接受商品写入。
- 不改用户自有 lock、bootstrap、研究资料；没有实站凭据/浏览器验收或 Rust 实库验收的结论仍明确保留。

## 已完成的核心定向证据

- R1–R5 内存调用实际 adapt_items/normalize_item：无市场与跨市场拒绝、裸 ASIN 请求保留 US 默认、变体重复/缺失/别名冲突拒绝、JD 页面状态拒绝、普通同名元数据保留、review/qa 链接变动不改版本，exit 0。
- 补充边界：Coupang source_url 与 url 变体冲突拒绝；JD 仅有 isProductPage+href 无实际内容拒绝；真实 JD 零价格仍被保留；形似完整但原生 ID 非法的普通同名元数据不误判，exit 0。
- R6–R7 临时 SQLite 文件实际 store_records：同时间价格更新、旧数据拒绝、更晚同事实刷新任务/工作流/lineage、已有 ORM 对象刷新、created_at 保留；三个并发首次插入最终只有一条最新 facet，exit 0。
- 新迁移 `s8t9u0v1w2x3` 衔接已查询的唯一 head `r6s7t8u9v0w1`。实际 SQLite upgrade/downgrade 验证：历史重复非商品 identity 行字节值不变，商品部分唯一约束生效，回退后约束移除，exit 0。PostgreSQL 索引 DDL 编译成功；未冒充 PostgreSQL 实库验收。
- 新增商品模型/迁移纳入后续更新后的 review diff；先前十四文件 review bundle 是修复前快照，不可误当最终 diff。
- PostgreSQL READ COMMITTED 分类窗口已关闭：INSERT 冲突后先 SELECT FOR UPDATE（populate_existing）锁住当前 facet，再读取哈希/观测时间并执行一次 UPDATE RETURNING。新协议的实际 SQLite 时序/并发首次插入/批内计数 smoke 均 exit 0；正确 PostgreSQL JSON 谓词的 FOR UPDATE SQL 编译成功，不声称 PostgreSQL 服务验收。

## 已接受的原生补丁交付问题

| 编号 | 严重性 | 已接受问题 | 修复动作 |
|---|---|---|---|
| R9 | 高 | Coupang 原生 search 页内规范化提前丢变体；product 输出忽略实际页面 URL | 单文件补丁覆盖真实 search.js/product.js，并将两者官方 SHA-256 纳入首次安装预检。search 保留完整变体 URL 供既有 URL 去重；product 从实际 evaluate 捕获 location.href，要求请求变体与观测匹配，缺失/切换/重复参数明确失败。真实命令 func 与受控浏览器 eval 联合回归，不仅测 helper。 |
| R10 | 中 | 金额区间/多金额/负数被截取为正价，法语加拿大金额漏读，评分/计数数字边界错误 | 价格只接受完整单金额并保留原文，歧义数值/币种为 null；明确加拿大市场支持小数逗号；K/M 后必须不是单词内部字符；评分分子/分母有数字边界，保留德语/日语正确值。 |
| R11 | 中 | 显式 fixture 前提缺失仍跳过，未知上游只有已有 receipt 漂移测试 | 显式 Node/archive/package 前提错误必须 fail；新增无 receipt 首次安装的未知源码变动回归，保留所有替换锚并验证 runtime/manifest/adapter/receipt 均未写入。 |

最终原生补丁定向验证：使用 CI 同一份官方 1.8.7 archive 与仅 17 个真实 runtime dependencies 的隔离 package，执行 `uv run --no-sync python -m pytest tests/unit/test_ecommerce_opencli_patch.py --no-cov -q`，22 passed、97 warnings、exit 0（139.62 秒；artifact://169）。通用尾部小数证据先于 marketplace locale，不保留 CAD 特判；首次未知上游拒绝测试无需复制运行依赖。已通知验证 worker 源码稳定，等待唯一一次最终联合测试，不追加审查轮次。

## 最终交付验收

- 使用实际CI provision的干净官方归档与临时嵌套依赖执行唯一最终broader+patch联合gate：369 passed、1 skipped、100 warnings，exit0；22项补丁测试全部执行（artifact://171）。
- 唯一skip为未配置PostgreSQL的可选跨后端检查；没有浏览器/eBay实站、ODP Rust实库或完整GitHub托管CI通过声明。
- CI provision原Python正文已本地运行exit0；仅明确自有的三个临时归档/CI根已清理。历史资料、用户锁文件、数据库、全局npm和普通pytest缓存未清理。
