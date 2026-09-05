# QR账号集群并行工程实施计划

## 1. 地位、基线与范围

本文件是工程排程，不是新SPEC、story或需求审批。唯一规格为 `_bmad-output/implementation-artifacts/spec-qr-account-cluster.md`，完整T1/T2/T2a/T3/T4/T5/T6/T7、13条AC、11行I/O及Design Notes全部有效；引用而不改frozen块。AC01—AC13是原spec Acceptance Criteria由上至下的定位编号，不改原文字义。原批准frozen SHA256 `6ba4a5bc5d0dc9271855b41345b1bf0f38c1d95f66ba99d9933bae49caaa511b`。

**implementation_baseline：`cec6a1a795bff2aca6864f8098743c0c6a59c2b6`。** 合同先合入 `f7ea9009d97d51fb32f3f6a811f446dd66439a25`，再合fixture形成该代码基线；两来源祖先检查exit0。实际worktree创建起点为**包含本计划的独立文档提交**，协调员在启动前记录其完整40位SHA并确认它包含上述代码基线、无未审查业务变化；所有owner使用同一个固定文档提交SHA，不从移动分支、旧d263或旧C1分叉。不在本文嵌入其自身未来commit以免自引用，也没有占位HEAD。本轮已合并上述既有产物；计划发布不额外实现业务，不创建worktree，不启动或修改Orca任务。

应消费而不重做：合同修正 `ae5fad836f5e2bc872230c23cd439240ebf83de4`；受控站点 `f661a7c82fd794294957a0e0d8377bf620565322`。八种封闭payload及空stop_and_save往返已有通过证据；fixture是实际HTTP站点，不是平台或集群验收。T1仍欠 `project_source_bindings.py` 创建/新revision保存account_id、workspace/account检查及发布/执行固定revision；归E1。schema存在不等于服务、授权、真实fencing或安全通道已实现。

合并烟测已通过：父合同8种canonical roundtrip（含stop_and_save空payload），5类非法输入在构造及from_wire均拒绝且错误不回显秘密；真实Chromium/HTTP fixture双context cookie隔离、真实SVG加载、challenge确认200/复用410/刷新旧码410/受控过期410。输入dummy密码后响应无回显。此证据不是物理扫码、自然等待过期、平台认证或集群验收。证据：`local://qr-account-merge-report.md`、`local://qr-account-merge-validation.json`。

读取证据：原spec完整、`local://qr-orca-dag.md`、`local://qr-account-source-inspection.md`、`local://qr-account-live-inspection.md`；并核对实际 `project_source_bindings.py` 两处构造、现有AccountRef/SessionEnvelopeV1/DurableCommandV1/PortalTransientV1、agent_runtime_dispatch的req.input与默认CDP、前端node-catalog的profile旧字段、route-tabs.tsx的COMPUTE_TABS。未暴露Repowise MCP，未安装工具。现有 `frontend/lib/flow/store.ts` 没有上述token不构成无需归属的证明，仍唯一归U。

## 2. 设计决策及并发

- 复用现有合同和fixture；G只锁**有实际消费者**的最短HTTP/service/guard接口，必要修订现有schema，不产生stub service，不重启长共享schema建设。
- schema/model/migration唯一owner C；G与后继M共用 `qrac2-contract`。最终旧全局site约束清退是后继M2，依赖E1，E1不依赖M2，避免环。
- E0与E1共用 `qrac2-execution`、同owner顺序工作。E0仅pool缺陷及对应单测，基线就能独立启动，不等待账号服务；不重复跑用户已报错误来确认存在。
- A独占账号状态机/权限/门户；S独占中心scheduler。A不复制claim逻辑，S不接管登录证据/门户权限。R独占agent_server/dispatch/nodes与节点runtime，内部R0→R1两卡同owner，不另开冲突worker。
- L独占capability/Script Host/感知录制；E独占skill_channel，按明确guard接入。U独占全部本任务前端含route-tabs、store。Q独占指定跨片回归、fixture后续必要修正、容量脚本与现有README/TESTING；D不单开worker。pyproject/uv.lock仅Q。
- 每owner最多一个活worker；不是每卡一个进程。推荐**4个逻辑实施worker**同时活跃；调度上限不是目标，空闲卡按关键路径填充。i5-12500H本机内存可用量未测，不承诺多浏览器压力能力。真实浏览器/PG多副本/容器验证共用**1个重型验证槽**，10M容量独占该槽且停止其他重实验；轻量源码工作可继续。两个中心/调度者是同一个验收实验内部拓扑，不占两个并行验收槽。
- 默认批次：①G、E0、L、Q0（Q0结束立即让R0补位）；②G出口后优先A、S、R0/R1、L；③完成者释放逻辑槽，E1、U、Q1接入。G并不阻止L的规则引擎/最小审计、R0停止态存储/监督器、E0和Q0。容量低于4则优先G→A/S/R关键路径，Q0环境阻塞立即记录不占着worker等待；高于4也不放宽重实验单槽。

```text
合并+smoke基线 B
 ├─ G [C:接口最短门禁] ─┬─ A [账号/门户] ────────────┐
 │                      ├─ S [中心调度] ────────────┤
 │                      ├─ E1 [E:T1残留/T4/T5] ─ M2 ┤
 │                      ├─ U [前端] ─────────────────┤
 │                      └─ R1 [R:节点接入] ──────────┤
 ├─ E0 [E:pool] ─────────── E1                       │
 ├─ R0 [R:监督/存储] ────── R1                       ├─ I 集成 → 全矩阵真实验收
 ├─ L [规则/审计/敏感guard] ────────────────────────┤
 └─ Q0 [环境准入] ── Q1 [测试/容量/README/TESTING] ──┘
```

箭头是启动/合同依赖，不要求所有业务服务实现完才开始consumer编码。跨片真实验收在I，consumer不得把测试替身/未接服务报告成业务完成。G接口发布后仍可由同C owner最小修订；只阻塞真实受影响consumer，绝不让所有独立工作重过整套门禁。

## 3. 最短接口合同（计划锁定目标，不冒称已有实现）

G必须逐项给实际函数/HTTP路由、现有schema类型、错误映射和consumer对照；每个接口artifact包含版本、所依赖完整代码commit、consumer清单、内容hash，并作为Orca任务可读取的持久附件传递。local副本只做编辑草稿，不是重启后的权威依赖。对无需改schema的接口只出精简说明，不造占位模块。

### C1：A ↔ S ↔ E，数据库事务归属

- A的 `browser_account_service.resolve_account_session(db, account_ref: AccountRef, execution_context)` 为E唯一入口；execution_context沿现有execution/run引用，不容许任意shell/input或客户端指定endpoint。返回现有SessionEnvelopeV1或有类型waiting/blocked；已领取封套跨下游重用，不二次acquire。
- A拥有账号/session状态与证据事务：创建/登录/暂停/恢复/人工confirm/可信证据CAS、platform identity保护、一致性与保存版本确认。A在同一事务验证授权/固定SourceBindingRevision/账号归属后调用S `enqueue(db, command: DurableCommandV1)`；相同workspace+caller幂等键返回原命令。
- S拥有 `enqueue`, `claim(node_identity, limit<=100)`, `renew(node_identity, claim: NodeClaimV1)`, `ack/commit_result(node_identity, claim: NodeClaimV1, result: NodeResultV1)`, `expire/recover(batch_limit<=100)`。直接复用现有NodeResultV1，保留external_identity、profile_manifest_ref、isolation_evidence_ref及evidence；result与claim的command_id/session_id/node_id/boot_id/epoch/expected_revision必须全部一致，再按DB权威workspace/account关联CAS，绝不把NodeEvidenceV1当完整结果或新造替代型。claim/renew/result服务自己创建短数据库事务；enqueue可参加A现有事务，不隐藏commit；结果在同一个CAS事务调用A的无I/O状态转换函数，禁止A↔S递归/嵌套领取。G明确函数名称与参数，不引入第二本任务账。
- CAS匹配workspace/account/session/node/boot_id/epoch/expected_revision；持久输入先经过封闭kind payload schema再入DB，所有错误最小结构化码。S只选命令/活租约/有效容量，不扫描休眠账号；容量和claim原子联动。DB过期不证明旧浏览器已停，无法停止证明转quarantined。
- E创建/加载SourceBindingRevision均保留account_id；发布与执行核验project/workspace/账号一致，切账号新revision，运行固定revision。HTTP/WS/III的payload hash/lineage纳入固定revision，auth_required是独立运行状态不能篡改已发布revision。

### C2：A HTTP/WS ↔ U，及A ↔ R的非持久门户

- 现有API前缀约定内 `/workspaces/{wid}/browser-accounts`：POST创建，GET keyset列表默认50上限200，GET `/{id}`；POST `/{id}/login-sessions`，会话GET/view/takeover/异常confirm/close；账号auth-required/suspend/resume/显式迁移。G锁定会话子路径、HTTP method、ApiResponse包络、游标字段、幂等header与revision CAS输入，U不猜测。
- HTTP identity复用现有解析；WS显式验证一次票据兑换后的cookie，不能假设Bearer依赖自动覆盖WS。票据body一次兑换、绑定user/workspace/account/session，秘密不在URL；HttpOnly/Secure、Origin/CSRF。默认10分钟上限30分钟。
- 404隐藏异域，403角色不足，410窗口失效，409 revision/identity/isolation冲突，503 node_unavailable/capability_missing；typed错误不得触发换账号。
- A各副本只对活跃portal每≤500ms批量读取新鲜DB权限事实（当前membership/role、disabled、active、session撤销/expiry/revision）；从权威撤销提交起≤1s停止转发与断连，授权无法确认fail closed，deadline本地保守硬断；无每帧N+1/长期事务旧快照。
- A↔R使用既有PortalTransientV1严格绑定，G锁定TLS节点端点/反向隧道owner登记、短期路由和控制/像素帧编码；schema SecretStr只是防repr，不是安全实现。表单input/QR bytes不得进DurableCommand、capability args/result、agent_runtime_dispatch.req.input、日志、trace、录制、通用事件或文件；输入一次瞬时投递并清空，不自动重放。断线旧视图关闭，重连重新授权。

### C3：L ↔ R ↔ E，目标、规则与敏感guard

- R给L显式完整SessionTargetV1及epoch/rule/view_generation；L只执行Bundle固定打包动作，返回LoginObservationV1、安全region/focus描述，不返回秘密/原始HTML/整页截图。R执行真实裁剪像素和同表单输入；每次origin/target/generation/focus重新检查，未知或多候选立即隐藏。
- R独占dispatch：仅非敏感开页/切换/刷新/探测走Script Host；租约解析CDP/BBX remote/daemon/HOME/DISPLAY/环境，不接受用户覆盖，HTTP与WS一致。
- L在现有perception/record实现敏感session guard，G锁定 `set_session_sensitive(session_id, enabled)` 与采集前检查参数如何绑定现有接口；L负责撤销已挂录制listener/待发事件，E在skill_channel阻止model loop、通用感知/事件；R在登录/接管准入至安全退出强制生命周期，不能只靠UI。name/value两条DOM取值前阻断，含password变text、OTP/token；普通页面行为保持。

### C4：R ↔ S/A，停止、快照、迁移

SessionEnvelopeV1、NodeClaimV1、NodeResultV1（内含NodeEvidenceV1）、ProfileManifestV1直接消费合入类型，禁止平行别名。R启动前核验持久最大epoch和boot；独立监督器保守deadline停止任务/Chrome/Bridge/VNC及代理，监督器死时服务管理器杀子树。完全停止后才流式不可变版本快照、清单/大小/hash/complete marker、fsync/原子current，再由S/A幂等提交DB；保留前一完整版本、DB失败command_id对账。恢复已提交manifest，损坏/空卷/dirty/不兼容阻塞；仅首次新账号允许空Profile。归属节点失联只等待；管理员给真实隔离证据后，停止态快照经节点认证TLS流式迁移且校验后原子切归属，非自动故障切换。密码策略导航/输入前实际生效、停止态仅密码凭据元数据检查，不导出/解密/删除；卷加密须运维挂载与配置证明。

## 4. 可分配任务卡与唯一write set

通用：所有卡读完整原spec及本计划，工作树branch命名 `chuanxu742-glitch/<owner-worktree>`；所有卡继承第1节的精确implementation_baseline，实际fork_base为协调员启动前记录的同一个包含本计划的完整文档提交SHA。只创建8个实施owner worktree（C/A/S/R/L/E/U/Q），I使用协调集成树。后继卡复用同树/终端。开始前记录真实base/head与依赖接口commit；后续只引入已审查依赖commit，不跟踪移动分支，不在活owner文件做集成“顺手修”。下面的write set是**独占上限，不强制改每个文件**；新发现必要生产调用者必须先登记给已有owner并做冲突检查，不可自行扩目录。全部永久测试仅E0的pool测试或Q指定测试；其他owner使用可删除的片内repro，向Q提交用例需求。项目formatter/lint/build/full suite仅I集成后统一一次。

实施者沿用用户指定的**真实Luna profile `openai-codex/gpt-5.6-luna`**；创建/恢复时记录实际终端banner/profile证据，不把标题或默认OMP worker冒称Luna。未找到该profile报告阻塞，不偷换普通agent；本轮不启动它。

**三层完成定义：** `depends_on`只约束启动需要的接口出口/前置卡。每个业务卡交“可集成提交”只须完整实现其独占范围、必要片内真实smoke、固定commit和诚实的待联合验证清单，不以I最终通过为完成前提；不可提交stub、假fallback或缺核心逻辑。Q1的可集成完成是可运行验收代码/文档、测试装载或适用片内自检、全矩阵映射，不要求尚未存在的I已执行通过。I在收到这些可集成提交后开展唯一跨片终验；只有I的全部实际AC证据通过才称功能交付。由此A/S/E/U/Q1→I是单向关系，没有I→Q1等完成语义环。

### G — 最短接口出口 / owner C, qrac2-contract
- 映射：T1差额、T2/T2a/T3/T4接口；AC01—AC05、AC09—AC12。depends_on：无（B已合并+smoke）。
- write set：`backend/schemas/browser_account.py`, `backend/schemas/browser.py`, `backend/schemas/browser_space.py`, `backend/schemas/source_binding.py`, `backend/schemas/workflow.py`, `backend/models/browser.py`, `backend/models/edge_node.py`, `backend/models/browser_space.py`, `backend/models/source_binding.py`, `backend/models/__init__.py`；必要时新 `backend/migrations/versions/refine_browser_account_contract.py`。不重写已交付payload或已应用add_browser_accounts迁移；真正需要的模型/迁移差额在G由C完成，后继迁移保持单head，不推给依赖E1的M2，不让A/S/E等待M2。无差额则不创建空迁移。接口artifact按第3节持久附到Orca任务，不新增第二SPEC。
- 产出：C1—C4逐consumer可执行签名/HTTP表/错误，必要现有schema commit；不足字段不以dict/任意input兜底，无stub service。G发布分C1/C2/C3/C4小出口，相关consumer收到所需出口即可开始；不用等全体接口一次完美。
- 验收：现有8种命令canonical往返与额外/嵌套秘密/错kind拒绝，不回显秘密；若schema变更用片内真实模块smoke证明。无变更时复用合并smoke不无意义重跑。handoff：接口表、实际schema hash/commit、consumer映射、命令exit/output及明确未实现服务边界。

### E0 — 已报告pool错误 / owner E, qrac2-execution
- 映射：T4、T7；AC04/AC05及竞争I/O。depends_on：无。
- write set：`backend/browser_pool.py`, `tests/unit/test_browser_pool.py`。
- 产出：未知显式endpoint失败、多队列竞争只保留一个token且取消归还其余、Local/Redis元数据协议一致，修复Redis get_agent_url调用错误。不是账号服务依赖，不重跑旧失败以确认用户观察。
- 验收命令：`uv run pytest --no-cov tests/unit/test_browser_pool.py`；真实Local三槽非路由获取/释放3→2→3、未知A不获得B、取消token守恒；真实Redis消费者路径于Q0可用后验证。没有Redis时不能称已跨后端通过。handoff：独立commit、测试输出、对E1协议变化说明。

### R0 — 节点监督与停止态Profile / owner R, qrac2-runtime
- 映射：T3、T5密码检查；AC03/AC07/AC12。depends_on：无；复用现有封套/manifest，尚未锁中心端点不阻塞此卡。
- write set：新 `backend/browser_account_runtime.py`；`agent/entrypoint.sh`, `chrome/entrypoint.sh`, `agent/Dockerfile`, `docker-compose.yml`, `docker-compose.build.yml`, `scripts/chrome-pool.sh`。
- 产出：独占栈监督/失租实际停机、持久epoch、PROFILE_DIR贯通两镜像、停止态流式快照/恢复/脏目录隔离/密码元数据及实际策略；每活跃会话DISPLAY/CDP/BBX/daemon/HOME/缓存/Profile隔离，旧pool脚本退出账号托管生命周期。
- 验收命令：`docker compose -f docker-compose.yml -f docker-compose.build.yml config`，并分别实际部署默认与构建组合；受控真实站点登录→停栈→快照→重启→校验恢复；两个会话并行栈不串用、监督器死亡亦停子树、损坏/空卷/dirty拒绝、密码策略无新增凭据但Cookie正常。配置解析成功不等于运行通过，Docker阻塞单列。handoff：停止证据、manifest/hash/恢复证据、实际策略与非秘密凭据计数、部署挂载/准入说明、commit。

### A — 账号服务与安全门户 / owner A, qrac2-api
- 映射：T2；AC02/AC07—AC10/AC13。depends_on：G。实际只需G的C1/C2出口即可启动，机器DAG保守记G。
- write set：新 `backend/services/browser_account_service.py`, `backend/api/v1/browser_accounts.py`；`backend/api/v1/__init__.py`, `backend/main.py`, `backend/security/workspace_rbac.py`, `backend/security/identity.py`, `backend/services/browser_space_service.py`。
- 产出：真实注册API/账号权限、keyset、幂等+revision CAS、可信身份组合自动saving→停止保存成功才saved；错误保留saving/error幂等重试，不额外常规confirm。人工仅manual_fallback且unknown。门户一次票据/TLS/同源短期流、跨副本≤1s撤销、期限硬断、反向隧道短期owner与重连重授权。
- 验收命令由Q提供 `uv run pytest --no-cov tests/unit/test_browser_account_service.py tests/integration/test_browser_account_cluster.py`；片内可用真实API/PG smoke，集成前不得宣称节点保存完成。行为包含两个workspace/所有角色、票据复用、过期、另一副本撤销计时、查询超时fail closed、saving前撤销拒绝/已saving断流后仍幂等完成、身份错配保留旧Profile。handoff：OpenAPI路由/调用样例（无秘密）、状态/权限实测、commit、依赖C1/C2版本。

### S — 中心持久调度 / owner S, qrac2-scheduler
- 映射：T3中心、T1有界claim；AC01/AC03/AC04/AC09。depends_on：G。
- write set：新 `backend/services/browser_account_scheduler.py`。
- 产出：唯一中心claim/renew/ack/结果CAS/恢复、容量原子占用、节点身份约束、命令幂等及完整payload校验，WS仅通知；固定账号不可用持久等待不换账号、不库存轮询。通过C1调用A状态转换，无第二状态机。
- 验收：Q的cluster入口在独立TEST_DATABASE_URL_PG上实际两个API/调度者，重复claim/结果/中心重启/节点过期拒绝，命令/租约索引有界；不能用SQLite证明分布式锁。handoff：PG事务与EXPLAIN证据、结果拒绝/恢复轨迹、commit、A/R调用合同。

### R1 — 节点认证、中心接入与真实执行封套 / owner R, qrac2-runtime
- 映射：T3节点、T4 HTTP/WS/BBX；AC02—AC05/AC10—AC12。depends_on：G、R0；不是第二runtime worker。
- write set：`backend/agent_server.py`, `backend/agent_runtime_dispatch.py`, `backend/agent_runtimes/bbx_adapter.py`, `backend/api/v1/nodes.py`, `backend/ws_agent_manager.py`, `backend/config.py`, `backend/api/v1/browser_containers.py`, `backend/acquisition/runner.py`。R0 runtime文件后续维护仍R唯一owner，卡间顺序移交，不两worker同改。
- 产出：管理员授权持久node_id与独立可撤销凭据，claim/renew/上报只限该节点，不信共享fleet token自报身份；安装包包含runtime依赖；实际容量/boot/过期准入，不具监督/磁盘/栈能力明确capability_missing。R0栈接入C1—C4；HTTP/WS封套相同，CDP/remote/daemon/env服务端注入，秘密走独立非持久通道；anonymous acquisition保留独立匿名保障。显式隔离迁移TLS传输与结果证据落实。
- 验收：cluster/login真实节点路径、HTTP/WS/BBX双账号不串用，失联实际停栈旧epoch/boot不得执行/落账，未隔离迁移409，隔离后快照传输校验，瞬时输入/像素不得走req.input。handoff：节点注册/撤销/安装包/能力证据、链路封套与执行结果、无秘密审计、commit。

### L — 规则、投影定位与敏感采集封锁 / owner L, qrac2-login
- 映射：T2a；AC07—AC13。depends_on：无；现有规则/observation schema足以独立开始，跨服务连接消费G的C3出口。
- write set：`backend/services/browser_capability_service.py`, `backend/skills/perception.py`, `backend/skills/record.py`, `chrome/script-host/background.js`, `chrome/script-host/packs/index.json`；新 `chrome/script-host/packs/account-login/content.js`, `chrome/script-host/packs/account-login/rules.json`。
- 产出：复用Q1 fixture建立完整受控规则及固定打包动作、精确origin/target代际、唯一QR/批准表单与刷新边界、可信身份+页面状态证据、未知同session接管。审计在任何持久化前固定白名单，无原result/page URL/title/str(exc)；敏感DOM name/value采集前禁用、已挂record listener与待发事件停止。真实平台规则只有实际验证版本才开放自动能力。
- 验收：`uv run pytest --no-cov tests/integration/test_browser_account_login.py`（Q交付），复用已有Bundle/Script Host相关回归；真实受控浏览器多origin/多QR/跨frame/旧generation/假成功/错身份/挑战/password→text/OTP/attribute name+value，非敏感页面仍工作。不以空rules/template或mock通过替代。handoff：规则版本/能力矩阵、真实平台逐规则验证或诚实阻塞、guard签名、无秘密证据、commit。

### E1 — 原执行链、T1绑定残留与旧调用迁移 / owner E, qrac2-execution
- 映射：T1残留、T4、T5；AC04—AC06/AC11。depends_on：G、E0；不依赖M2，不等A/S/R实现完才编码。
- write set：`backend/pipeline/pipeline.py`, `backend/channels/opencli_channel.py`, `backend/channels/skill_channel.py`, `backend/workflow/runtime_resources.py`, `backend/workflow/fleet_inventory.py`, `backend/workflow/runtime_registry.py`, `backend/workflow/opencli_hda_tracer.py`, `backend/workflow/iii_collection_dispatch.py`, `backend/workflow/iii_collection_store.py`, `backend/workflow/hda_templates.py`, `iii/workers/collector-opencli/src/main.py`, `iii/lib/opencli_cli.py`, `backend/services/browser_service.py`, `backend/api/v1/browsers.py`, `backend/api/v1/project_source_bindings.py`。
- 产出：AccountRef固定binding revision保存/重载/发布校验、同站双账号原pipeline/channel/HTTP/WS/III/BBX真实封套沿原生命周期，不把控制字段传CLI、不重新取槽、不unknown endpoint回退、不换账号；III lineage/hash含固定revision。skill_channel接L guard暂停模型/感知/录制。旧映射按实际物理Profile归并、停止相关任务/保全清单，仅可证明workspace导入，歧义可认领/阻塞；历史密码元数据不明阻塞，R检查不解密/删除/复制密码；移除旧绑定API/生产消费者，保留原Profile和审计。
- 验收：Q的source_binding/channel/workflow/III回归及真实两账号发布→执行；T5冲突保留数据、可见修复、同Profile只一个账号，AccountRef缺失不猜site/endpoint；E1提交生产调用清退清单供M2解除旧模型约束。handoff：固定revision演示、旧调用穷尽token检索结果、迁移审计与阻塞样例（无登录态）、commit、M2所需约束清退事实。

### U — 独立账号页与编辑器全链路 / owner U, qrac2-ui
- 映射：T6、T5 UI清退；AC02/AC04—AC10/AC13。depends_on：G。
- write set：新 `frontend/app/(app)/browser-accounts/page.tsx`, `frontend/components/browsers/browser-accounts-panel.tsx`, `frontend/lib/api/browser-accounts.ts`；`frontend/lib/navigation.ts`, `frontend/components/shell/route-tabs.tsx`, `frontend/app/(app)/browsers/page.tsx`, `frontend/components/browsers/browser-spaces-panel.tsx`, `frontend/components/browsers/browser-bindings-panel.tsx`, `frontend/lib/api/browser-spaces.ts`, `frontend/components/flow/inspector.tsx`, `frontend/lib/workflow/node-catalog.ts`, `frontend/lib/workflow/source-business-config.ts`, `frontend/lib/flow/store.ts`, `frontend/lib/flow/store-canonical-actions.ts`, `frontend/lib/flow/store-slices.ts`, `frontend/lib/flow/types.ts`。
- 产出：导航含route-tabs入口与独立账号页，池页只活跃资源/账号链接；正常扫码/表单无需额外确认保存，刷新/验证/saving/error进度、unknown/人工来源/平台证据/runtime分别显示，challenge同session接管，旧帧立即隐藏、内存输入发送清空不重试。编辑器accountId/sourceBindingRevisionId保存→重载→展开→发布→执行保留，同站不同账号不去重，移除profile旧路由双源；旧绑定UI退出。
- 验收：实际运行前后端，在真实页面完成添加/扫码或必要输入/自动保存休眠/恢复、挑战/过期/权限撤销、同站双账号编辑重载发布；静态mock截图不算。frontend命令从现有package.json读取，不虚构测试入口。handoff：实际URL/操作与截图（无秘密）、链路revision证据、依赖API版本、commit。

### M2 — 最终模型/约束清退 / owner C, qrac2-contract
- 映射：T5最终旧约束清退；AC01/AC03/AC05/AC06。depends_on：G、E1（E1不依赖此卡）。
- write set：`backend/models/browser.py`, `backend/schemas/browser.py`；新 `backend/migrations/versions/finalize_browser_account_cutover.py`。与G重叠路径是同C owner且G→E1→M2顺序复用，绝无两个writer；只移除E1已完成生产清退的旧全局site约束/obsolete模型schema，不改已应用迁移历史，后继Alembic接G实际最后head。
- 产出：严格依据E1清退证据移除最终旧约束与过时模型/schema路径；不承担consumer启动必需的模型/schema差额（此类差额始终回G阶段C owner修订）。安全数据迁移不删除Profile、不猜workspace/复制登录态，并非重造T1。
- 验收：独立PG上真实Alembic upgrade与同站不同workspace/账号、跨域FK拒绝/唯一活租约/10GiB字段读回；保存旧映射与原数据，歧义仍阻塞。不是用ORM create_all证明Alembic升级。handoff：单head/迁移前后事实/映射保全/commit、consumer清单。

### Q0 — 环境准入 / owner Q, qrac2-acceptance（不另开D）
- 映射：T7所有真实验收前置。depends_on：无。
- write set：无业务文件；仅本地/Orca非秘密环境证据。
- 产出：可用的独立TEST_DATABASE_URL_PG、Redis、两个中心+受监督节点实验拓扑，Chrome/Script Host与已合入fixture启动方式；Docker daemon已知不可连接，记录为外部前置，不空转重查或伪造容器通过。提供实际Profile卷加密配置/挂载映射与运维审查记录；只有DB布尔/自报不足。真实平台操作需用户合法账号/必要手机确认/平台允许的验证环境；不要求用户先报平台名单，但无授权或人机操作就明确未验真实平台。
- 验收：获准准备环境后启动最小拓扑、记录非秘密版本与连通/停机控制证据；现有安全回归的失败/缺服务/凭据缺失分开登记，不将skip算通过。资源无法取得时阻塞相应实测，不阻止Q1编写验收。handoff：环境准入清单和每项真实通过/阻塞理由、重型实验预订规则。

### Q1 — 跨片回归、容量与交付文档 / owner Q, qrac2-acceptance
- 映射：T7，AC01—AC13，I/O全部11行。depends_on：Q0（环境调查完成即可，不要求所有外部前置已解决）；测试编写读G出口，不阻塞在全部业务完成。
- write set：`tests/fixtures/browser_account_login_app.py`, `pyproject.toml`, `uv.lock`, `tests/unit/test_browser_space_service.py`, `tests/unit/test_browser_service.py`, `tests/unit/test_browser_runtime_bundle.py`, `tests/unit/test_agent_server.py`, `tests/unit/test_browser_docker_config.py`, `tests/unit/agent_runtimes/test_bbx_adapter.py`, `tests/unit/api/test_source_binding.py`, `tests/unit/api/test_browser_spaces.py`, `tests/unit/channels/test_opencli_channel.py`, `tests/unit/channels/test_skill_channel_emit_batching.py`, `tests/skills/test_skill_channel.py`, `tests/skills/test_perception.py`, `tests/skills/test_record.py`, `tests/skills/test_record_live.py`, `tests/unit/test_iii_collection_dispatch.py`, `tests/integration/test_browser_space_service.py`, `tests/integration/test_workflow_compile_api.py`, `tests/integration/test_workflow_opencli_hda_trace_api.py`, `tests/integration/test_workflow_capabilities_api.py`, `tests/integration/test_workflow_bbx_tools_api.py`, `tests/integration/test_workflow_fleet_api.py`, `tests/integration/test_iii_collection_vertical.py`, `tests/integration/test_iii_collection_cancellation.py`, `tests/integration/iii_collection_test_support.py`；新 `tests/unit/test_browser_account_service.py`, `tests/integration/test_browser_account_cluster.py`, `tests/integration/test_browser_account_login.py`, `scripts/verify_browser_account_capacity.py`；`README.md`, `TESTING.md`。
- 产出：保留fixture并只按真实缺陷最小修正，不复制测试站点或装重复依赖；真实AC/I/O矩阵/可运行脚本/测试，测试防行为边界而非mock回声/源码字符串/内部调用/文案。README/TESTING写实际部署、迁移、恢复、权限/秘密/故障边界与命令，不新增泛滥说明文档。
- 终验命令（由I执行，不作为Q1交可集成提交的反向依赖）：`uv run pytest --no-cov tests/unit/test_browser_pool.py tests/unit/test_browser_space_service.py tests/unit/test_browser_account_service.py`；`uv run pytest --no-cov tests/integration/test_browser_account_cluster.py`；`uv run pytest --no-cov tests/integration/test_browser_account_login.py`；`uv run python scripts/verify_browser_account_capacity.py --accounts 10000000 --page-size 100 --claim-size 100`；Docker组合命令见R0。Q1实现完整可运行代码并通过适用片内证明即可交可集成提交，状态明确“等待I联合执行”；只有I真实运行通过才宣布整体验收通过。
- 容量严格原AC：专用容量DB、无真实凭据，同硬件固定活跃数100万→1000万；EXPLAIN ANALYZE无Seq Scan/全量物化，page≤100、claim≤100、中心RSS增量≤20%，新增休眠库存不增加进程/轮询项；保存RSS/p95/进程与计划原始证据，不能低规模外推或把结构能力当平台吞吐。
- handoff：每AC与I/O实际命令/exit/环境/证据链接，未运行/失败/外部阻塞分列，fixture基线、变更原因、依赖锁差异、commit。

### I — 集成与终验 / 集成owner，qrac2-integration
- 映射：全部T/AC/I/O。depends_on：G、E0、R0、A、S、R1、L、E1、U、M2、Q1。
- write set：无常设业务文件；只集成审查过的独占commit。跨片缺陷退回原owner；只有原owner已停止且记录唯一移交后，I才能修该精确路径，不能和活owner并发编辑。
- 集成顺序：B→G→E0→R0→A→S→R1→L→E1→U→M2→Q1。纯独占commit可调整，但M2必须在E1后，G消费者先收确切G版本；集成A/S的互相调用一次性联合检查，不运行半合入树假失败。每卡artifact记录原始完整commit，不用未提交目录充当依赖。
- 验收：单一重型槽依次完成PG多副本/节点fencing与迁移→受控浏览器登录/秘密与密码策略→实际UI及真实已验证平台→10M容量；保留既有安全回归并统一跑项目可用formatter/lint/typecheck/build/test。每命名AC/I/O均必须实际通过，mock/skip/配置解析/worker_done不替代。外部前置缺失时报告对应未验项目，不标集群完成。
- handoff：最终commit、base/源commit清单、覆盖矩阵、命令exit/时间/证据、frozen hash保持、用户工作保全事实、所有真实未通过项；未授权push则不push。

## 5. 全范围覆盖与诚实通过条件

| 原AC | 主要owner/卡 | 不能省略的终验 |
|---|---|---|
| AC01 千万结构容量 | S/G/M2/Q1 | 100万→1000万真实DB、索引有界/RSS≤20%/固定活跃进程 |
| AC02 跨域与≤1s撤销 | A/R1/U/Q1 | 另一副本活流、权威提交计时、新鲜快照失败关闭、deadline |
| AC03 租约/中心与节点重启 | S/R0/R1/M2/Q1 | 两中心/两调度者、真实停机、旧epoch/boot拒绝、未隔离不迁移 |
| AC04 原执行链双账号 | E0/E1/S/R1/Q1 | 不换账号、真实恢复会话、输出原链 |
| AC05 固定revision/HTTP/WS/III/BBX | E1/U/R1/Q1 | 保存重载展开发布、隔离CDP/remote/daemon/env、anonymous独立 |
| AC06 旧映射歧义 | E1/M2/U/Q1 | 保留数据/原Profile、可认领阻塞、全局site/default退出 |
| AC07 实测规则自动保存恢复 | L/A/R0/R1/U/Q1 | 用户正常无额外保存、真实手机必要操作/平台规则、节点重启恢复 |
| AC08 假成功/错身份 | L/A/Q1 | QR消失/200/跳转都不替代身份+状态，旧绑定不覆盖 |
| AC09 刷新/保存竞态 | L/A/S/R1/U/Q1 | 旧generation/过期拒绝、当前最多一次版本、先冻结停止刷新 |
| AC10 秘密不持久化 | A/L/R1/U/Q1 | DB/消息/log/trace/event/model/截图文件无秘密、唯一当前批准QR、多origin |
| AC11 Skill敏感采集 | L/E1/R1/Q1 | DOM前name/value两路、password变text/OTP、已挂listener/待发事件、普通页面恢复 |
| AC12 浏览器密码策略 | R0/R1/E1/Q1 | 真实密码登录→停止→快照→恢复，凭据未新增且Cookie正常，未知拒绝导入/输入 |
| AC13 无规则/布局变动 | L/A/U/Q1 | unknown/非自动、同session接管、manual_fallback不伪造rule_verified |

I/O逐行归属：自动登录保存=A/L/R/U；QR刷新竞态=L/A/R/U；挑战/无规则=L/A/R/U；假成功/错账号=L/A；密码输入=A/R/L/U；休眠恢复=R/S/E；认证失效=A/E/L；权限/窗口过期=A/R/U；竞争/断网=S/R/E0；节点故障/迁移=S/R/A；旧配置歧义=E/M/U。Q1为全部测试owner，I逐行验收。原Design Notes的dirty版本/atomic fsync/上一个完整版本/有界GC/外部副作用不盲重放、物理隔离证据与磁盘加密证据仍由相关R/S/E卡及终验完整承担，不能仅靠字段存在打勾。

## 6. 旧Orca排程迁移（本轮只建议，不操作）

旧 `local://qr-orca-dag.md` 保留为历史，**执行排程由本计划取代**，不覆盖历史。`run_51f5f98e52a5`最近检查A/R/L/E/U/Q是ready、dispatch=null，I pending/dispatch=null；F4/Q1 completed是复用基线事实，不重派。新阶段Q1是本计划卡id，不是历史fixture Q1 task。

优先在后续获派发授权后复用旧run：协调器先用当前版本guide检查真实任务/dispatch，不把历史snapshot当实时保证；停止旧ready自动派发来源，记录old_task→new_card映射，更新未派发任务说明/base/owner/dependencies（若公共API允许）并新加S、G/M、分阶段卡；旧六片不能同时按旧附件启动。旧F1 blocked保持历史失败，由替代记录说明F2/F4已承接，不能伪造旧F1通过。

若任务spec不可变或原owner上下文不宜复用，则创建新run并将旧A/R/L/E/U/Q/I逐项标记superseded/cancelled（用当前CLI实际支持状态，不猜指令），确认无dispatch/worker占用后才允许新run派发；保存旧→新映射与唯一active-run标识。任何未知响应先查receipt/dispatch，不重发worker-start。新run不复制已经完成F4/fixture工作。派发前门禁必须证明每个card只有一个dispatch且每owner只有一个活worker；本轮不修改Orca、不启动worker。

## 7. 发布及排程静态验证

代码基线与局部烟测已收到，原spec/frozen不动；本计划是唯一永久排程文档，未另造YAML权威。发布时用临时脚本直接解析下列任务卡和覆盖表，实际检查全部13 AC、11 I/O、T1/T2/T2a/T3/T4/T5/T6/T7有owner，DAG无环，每路径一个worktree owner，同owner阶段顺序、关键热点归属与精确代码baseline；输出追加于本节。实际fork_base依据第1节的文档提交约定在以后派发前记录，不能把尚未启动的worktree标为已验证。清理临时脚本；文档静态验证不是业务验收。

### 本轮实际检查结果

在父树以 `.venv/Scripts/python.exe -B <独有TEMP目录>/verify_plan.py _bmad-output/implementation-artifacts/plan-qr-account-cluster-parallel.md` 运行临时检查脚本，**exit 0，stderr为空**。脚本直接解析本Markdown，不维护第二份YAML/JSON任务权威。检查结果：

```text
PASS
implementation_baseline=cec6a1a795bff2aca6864f8098743c0c6a59c2b6
cards=13; implementation_worktrees=8; write_paths=108
dag=acyclic; cross_owner_conflicts=0; completion_semantic_cycles=0
T_coverage=T1,T2,T2a,T3,T4,T5,T6,T7
AC_coverage=13/13; IO_coverage=11/11; hotspots_checked=10
ordered_same_owner_reuse:
  backend/schemas/browser.py: G -> M2, owner=qrac2-contract
  backend/models/browser.py: G -> M2, owner=qrac2-contract
topological_order=E0,G,L,Q0,R0,A,E1,Q1,R1,S,U,M2,I
spec_sha256_unchanged=f74952657e8256d2fc62038fd56462c0e7b020181537a8e9f874425e6ee4abc6
```

这是静态排程、基线引用、原spec文件不变与所有权检查，不证明尚未实施的业务、安全、平台或容量验收。任务未派发；future fork_base完整文档commit SHA须由协调员启动前记录。临时检查脚本及其独有目录在报告前删除。
