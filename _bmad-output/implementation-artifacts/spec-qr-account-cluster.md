---
title: '扫码账号与按需浏览器集群'
type: 'feature'
created: '2026-09-05'
status: 'in-progress'
review_loop_iteration: 0
context: []
baseline_commit: 'caa50b62533b3dae76673d819a962ca3fcb1a744'
approval:
  decision: 'K+A'
  approved_tokens_o200k_base: 8793
  approved_content_sha256: '38c550a1df870dc933b94220000c666f885ecda3d1f7357adecceaf0bdcf8647'
  frozen_content_sha256: '6ba4a5bc5d0dc9271855b41345b1bf0f38c1d95f66ba99d9933bae49caaa511b'
  scope: '用户批准完整范围、Profile Lease语义变更、归属节点故障阻塞且旧节点隔离后显式迁移。'
  execution: '用户指定Orca多worktree并行开发、Luna实施、最终统一集成验证；覆盖旧默认顺序/普通subagent实施方式。'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 现有站点绑定、浏览器槽和进程内路由不能表达隔离的多账号、可靠保存与跨进程调度。

**Approach:** 建立 workspace 账号记录；选择平台/添加账号后，服务端按已验证的版本化登录规则自动打开真实登录页、切换登录方式，仅投影安全登录区域。用户扫码及完成手机必要确认（或输入原生表单所需信息）；可靠成功探测后自动停止浏览器、保存 Profile并休眠，无需常规点击“我已登录/保存”。后续任务按 `account_id` 独占恢复。无需用户先报平台名单或并发数字；通用核心不等于无规则自动适配所有网站。

## Boundaries & Constraints

**Always:** 账号总存量千万级是结构扩展目标，不是千万常驻浏览器。Profile只存登录/网站状态，Bundle与登录规则只读且版本固定；任一Profile最多一个Chromium写者，包括“只读”采集。workspace/account/session/目标tab与frame/命令/租约全链路校验。分别展示自动或人工来源、平台认证证据、runtime健康；READY及Cookie非空不证明登录有效。原生页面中的受控自动化与区域投影复用同一浏览器，不复制HTML重建第三方协议。可信认证失效或人工报告需扫码后暂停该账号任务，重认证按原任务安全恢复规则继续。

**Ask First:** 本检查点明确申请批准：①把原 `spec-browser-runtime-bundle.md:23` 的 Profile Lease 改为持久账号租约、代际 fencing 与节点实际停机约束；②采用持久归属节点，节点故障时阻塞该账号，仅在旧执行/存储访问已隔离后显式迁移，**不是自动跨节点故障切换**。这两项是待批准建议，不是用户已同意。删除 Profile、破坏性旧数据迁移、自动故障切换或新增外部存储服务另行批准。

**Never:** 新建平行采集系统；不可用时换账号；空Profile恢复；失联等同隔离；删Singleton锁作为互斥；全量账号字典/扫描；任意远程JS、LLM任意点击或猜二维码作为登录成功判断；移植第三方DOM/绕过CSP或人机验证；默认保管平台密码；承诺全平台自动支持、永久登录或迁移免扫码；全功能备份管理不在范围内。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 自动登录保存 | 有权限账号、已验证规则 | 自动开登录页/切换方式→投影QR或原生表单→用户扫码/必要输入→可信身份+状态验证→自动停浏览器/提交Profile/休眠 | saving失败不显示成功、不释放占用；无额外人工保存步骤 |
| QR刷新竞态 | 过期、刷新失败、旧二维码事件 | 按规则有界刷新并递增视图代际，旧码立即遮蔽；只展示当前允许origin中的唯一候选 | 多候选/定位不明/跨域跳转时unknown并隐藏投影；不猜码，失败转同会话接管 |
| 挑战/无规则 | 验证码、短信、CSP/跨域限制或缺可信探测 | 展开同account/session原生受控窗口完成必要操作 | 明示challenge或“非自动/未验证规则”；无法可靠检测时仅异常人工确认，平台证据仍unknown |
| 假成功/错账号 | QR消失、HTTP200、URL变化、身份与既有账号不符 | 不自动提交；可信身份与页面状态组合通过且账号一致才保存 | account_identity_mismatch阻塞；不改绑、不覆盖旧Profile |
| 密码输入 | 当前表单区域与有效短期会话 | 输入仅瞬时送入同一原始表单，服务端不读取密码回显 | 禁入DB/持久命令/任务参数/日志/录屏/通用事件；过期或目标代际变化拒绝输入 |
| 休眠恢复 | 已提交 Profile、指定 account_id | 原账号独占启动，Bundle loaded=desired 且自检通过后执行 | 缺失/损坏/不兼容阻塞，禁止新建空 Profile |
| 认证失效 | 平台明确拒绝、登录挑战或人工标记 | auth_required，暂停该账号后续任务并入口重扫 | 网络失败仅报网络错误；无证据不得宣称有效或失效 |
| 权限/窗口过期 | 跨 workspace、查看者、泄露/过期票据 | 无权新连接拒绝；撤销提交后≤1秒停止既有流转发，过期按deadline硬断 | 404 隐藏异域记录；403 权限不足；410 失效窗口；不承诺撤销瞬间零在途帧 |
| 竞争/断网 | 两个调度者、重复命令、旧代际结果 | 一个有效写者；重复调用返回原命令；旧结果不落账 | 租约失效节点停止浏览器和任务；无法证明停止则隔离阻塞 |
| 节点故障/迁移 | 归属节点不可达 | 等待恢复；显式迁移须隔离证据及已停止快照 | 503 node_unavailable；409 isolation_required；设备绑定可能需要重扫 |
| 旧配置歧义 | 全局 site/共享 Profile、归属不明 | 保留数据、列出待认领项、阻断相关执行 | account_migration_required；不得猜 workspace 或复制登录态冒充多个账号 |

</frozen-after-approval>

## Code Map

以下为已读取源码；行号为调查锚点，实施时按符号定位。

- `backend/models/browser.py:9,35`：BrowserBinding.site 全局唯一；BrowserInstance 是槽，profile_name 唯一不是账号授权。`backend/models/edge_node.py:18-43`：URL 身份及 online/last_seen，不是持久容量。
- `backend/services/browser_space_service.py:create_space(232),close_space(770)`：预留全局实例、未验证 workspace 资源归属；关闭不删 Profile。复用生命周期/事件，不能直接继承授权漏洞或进程锁。
- `backend/browser_pool.py:104-110,158-180`：未知 endpoint 回退任意槽；竞争获取可能消耗多个 token。`backend/channels/opencli_channel.py:67,81,728-750,886`：路由、远程分派、health_check；737 无条件 get_agent_url，而 RedisPool 无该方法。
- `backend/pipeline/pipeline.py:164-190`、`backend/channels/skill_channel.py:591-595`：旧 site→endpoint 与另一个 session-affinity 消费者。`backend/workflow/runtime_resources.py:58-118`：资源 ID 是推导值而非真实租约/快照，不能作为账号证据。
- `backend/acquisition/runner.py:80-157,209-245`：复用数据库 CAS/heartbeat 思路；恢复查询无界，不能照搬其全量扫描。`backend/ws_agent_manager.py:65-75`：WS 字典仅适合活连接，不是任务权威状态。
- `backend/api/v1/nodes.py:169,406-450,782`：注册、安装包、WS入口。`backend/agent_server.py:108,198,227,678,703-728`：固定默认 CDP、活动进程、进程树终止、请求及能力调用，需执行代际校验。
- `backend/api/v1/browser_containers.py:294` 固定 chrome 挂载；`agent/entrypoint.sh:36,50-51,67,88-89` 使用 agent 路径且裸 noVNC；`chrome/entrypoint.sh:9,14-15,69-70` 同有无密码 VNC/删锁。`docker-compose.yml:384,435` 与 `docker-compose.build.yml:60-63` 分别匹配各镜像，不能统一错挂。
- `backend/security/workspace_rbac.py:14-51,68`：复用成员/角色校验并增加账号权限；`backend/auth/crypto.py:44-49` 只支持字符串 Fernet，不能加密巨大 Profile 后塞 DB。
- `frontend/app/(app)/browsers/page.tsx:13-22` 混排资源/绑定/Space；`frontend/lib/navigation.ts`、`frontend/components/shell/route-tabs.tsx:75` 为入口。`docs/opencli-agent-data-operations-platform-PRD.md:122` 要求账号与执行资源分离。
- `backend/agent_runtime_dispatch.py:183-205` 仅script-host使用CDP参数；`backend/agent_runtimes/bbx_adapter.py:690-708` 支持config.remote但默认继承daemon。`iii/workers/collector-opencli/src/main.py:243-250` 调用 `iii/lib/opencli_cli.py` 并传chrome_endpoint。`scripts/chrome-pool.sh` 是旧池管理入口，须退出账号生命周期所有权。
- `backend/workflow/delivery_authorization.py:116-128,670-696` 已有scope+id keyset+limit+1/next_cursor模式，直接沿用，不另造offset/total分页。`backend/models/source_binding.py:1-13,103` 定义workspace/project与不可变revision；账号选择应固化到此授权绑定而非新增平行Source系统。
- `frontend/components/flow/inspector.tsx:2358`、`frontend/lib/workflow/node-catalog.ts:656-695,755-793`、`frontend/lib/workflow/source-business-config.ts:29` 是账号选择、校验、HDA内联投影与旧DataSource适配的必经链。
- `backend/schemas/browser.py:24-67` 已有组件版本、结构化capability/args_schema/allowed_hosts/risk/gate/config；扩展为受约束登录规则，不引入第二套插件系统。`chrome/script-host/packs/index.json:1-9` 当前只有page-basics元数据动作，没有已验证平台登录规则。
- `chrome/script-host/background.js:21-51,108-134` 校验打包动作，但缺tabId会选active tab且返回URL/title；账号登录必须固定tab/frame/document并消除敏感回显。`backend/agent_runtime_dispatch.py:119-179` 只调用打包Script Host动作，可复用，不接受请求携带JS。
- `backend/services/browser_capability_service.py:83-171` 默认持久化args、完整result及page URL/title，且host只校验args.url；新增登录路径必须验证真实导航origin并实施专用最小审计，不能把密码/验证码/QR或带token URL送入现有通用审计。
- `backend/skills/perception.py:64-80` 的name可取input attribute value，value另取el.value；`backend/skills/record.py:75-104` name同样泄漏，且仅password类型擦除change.value。`backend/channels/skill_channel.py:602-618` 启动通用感知/model loop；登录敏感模式必须在采集前阻断，不能只清最终value。

## Tasks & Acceptance

**Execution:** 按 T1→T2/T2a/T3→T4→T5/T6→T7 顺序；新增文件标“新”，未标者修改既有文件。

- [ ] **T1 数据合同**：`backend/models/browser.py`、`backend/models/edge_node.py`、`backend/models/browser_space.py`、`backend/models/__init__.py`、`backend/migrations/versions/add_browser_accounts.py`（新）、`backend/schemas/browser_account.py`（新）。账号含workspace/site/label、归属node_id、profile_id/提交版本、Bundle、auth_required、受保护的平台身份及证据（unknown/valid/invalid、来源/时间）；会话含临时instance_id、rule版本、tab/frame/document、view_generation及登录状态。来源区分rule_verified/manual_fallback，人工兜底另记确认人；不存输入密码/验证码/QR内容。新增持久命令、账号租约、节点容量/boot_id、profile manifest；复合外键防跨域、账号唯一活租约、workspace+调用方幂等键。索引 `(workspace_id,status,id)`、`(node_id,status,available_at,id)`、活跃租约到期；列表默认50/上限200、claim≤100，槽只随活跃容量增长。
  沿用已有keyset约定；新增账号绑定写入 `backend/models/source_binding.py` 的不可变SourceBindingRevision，并修改 `backend/api/v1/project_source_bindings.py`、`backend/schemas/source_binding.py` 的修订/发布校验。切换账号创建新revision，运行固定revision，不随最新绑定漂移；运行时认证状态独立可变。
- [ ] **T2 账号服务与安全门户**：`backend/services/browser_account_service.py`、`backend/api/v1/browser_accounts.py`（均新）、`backend/main.py`、`backend/security/workspace_rbac.py`、`backend/services/browser_space_service.py`、`backend/schemas/browser_space.py`。`/workspaces/{wid}/browser-accounts`提供创建/游标列表/详情、`/{id}/login-sessions`及会话视图/接管/异常confirm/close、账号auth-required/suspend/resume/显式迁移。管理员/维护者管理，操作员登录使用，查看者仅元数据；每次按账号归属校验，不信任endpoint。幂等键+revision CAS；正常流程由验证成功触发202 saving，持久提交后才saved，confirm仅manual_fallback。状态机 `opening→presenting↔refreshing→verifying→saving→saved/dormant`；challenge/unknown进入同会话接管，可靠探测恢复可回verifying；到期/关闭进入expired/closed且不伪称成功；保存失败保留saving/error可幂等重试。状态迁移绑定epoch/rule/view generation，持久命令不含表单秘密。
  区域投影/输入与完整接管共用短期同源HTTP/WS认证代理：票据单次兑换、绑定用户/workspace/account/session，默认10分钟上限30分钟，HttpOnly/Secure cookie，Origin/CSRF校验，秘密不进URL/日志。上游仅服务端租约映射，封闭外部VNC/CDP；节点间鉴权，禁剪贴板/上传下载与录像。NAT反向隧道仅作传输，DB登记短期owner并跨副本代理；断线关闭视图、重连重新授权，不能把旧码或输入重放到新会话。
  路由实际注册点为 `backend/api/v1/__init__.py`；复用 `backend/security/identity.py` 的身份解析但为WS显式实现cookie/票据验证，不能假设HTTP Bearer依赖自动覆盖WS。
  撤销/权限变更以权威DB提交时间起算：所有代理副本对活跃流每≤500ms批量读取当前成员存在/role、User.disabled、Workspace.active及会话撤销/到期/revision等权威权限事实；现有WorkspaceMembership没有revision字段，不依赖虚构版本。查询使用新鲜数据库快照，超时计入1秒授权新鲜度上限；超过上限或无法确认授权即停止HTTP/WS转发并断连。通知仅加速；到期由本地保守deadline硬断。只检查活跃门户会话，不做每帧/每账号N+1、全量库存扫描或复用长事务旧快照；不保证追回已发送帧。
- [ ] **T2a 规则与安全投影**：`backend/schemas/browser.py`、`backend/services/browser_capability_service.py`、`backend/agent_runtime_dispatch.py`、`chrome/script-host/background.js`、`chrome/script-host/packs/index.json`，新增 `chrome/script-host/packs/account-login/content.js`、`chrome/script-host/packs/account-login/rules.json`，并复用T2/T3服务。Bundle内声明式规则固定id/version、精确允许origin/登录URL/跳转与frame范围、方式切换动作、QR/表单定位及敏感区域、刷新触发/最大次数/间隔、成功证据组合、身份提取及挑战条件；配置校验拒绝任意代码/任意URL，Script Host只执行打包动作。限定tab/frame/document，禁止active-tab默认及用户覆盖；每次观察/输入/刷新/跳转重新核验真实origin和目标代际。运营配置并真实验证一次后向用户开放“自动登录”；支持清单只含对应规则版本的实测能力，未配置/未验证保持unknown/非自动。
  投影真实页面的受控像素区域/原生输入，不复制DOM或加载第三方脚本到控制台。QR只截唯一且当前允许origin的二维码元素；表单仅显示规则允许区域并遮蔽敏感回显，布局/候选不确定立即隐藏，复杂挑战展开同会话原生窗口，不绕过CSP/验证码。凭据输入走短期TLS流直达原表单而非持久队列；只允许当前字段/焦点，禁原始input/result进入capability审计、trace、错误、代理请求日志与截图，事件仅状态/代际/错误码。自有输入控件若需要仅保存短时内存，投递后清空、不自动重试；不许获取密码值或借“脱敏”先落库。规则引擎交付受控测试站点完整规则及行为验证，不以空规则/模板冒充真实平台支持。
  非敏感开页/切换/刷新/探测可复用capability；密码/验证码输入与QR/区域像素流走T2/T3独立的鉴权、期限、同session非持久通道，**不得调用通用capability args/result或agent_runtime_dispatch的req.input来承载秘密**。在 `browser_capability_service.invoke_capability` 任何持久化前构造固定白名单审计，输出仅状态/规则版本/必要身份关联；禁止原始result、page_before/page_after及异常str写库，审计输入覆盖参数不是安全通道。截图不落盘、不whole-page fallback；不复用会导出input.value的通用页面感知/录制链。
  同时修改 `backend/skills/perception.py`、`backend/skills/record.py`、`backend/channels/skill_channel.py`：进入登录/接管前暂停该session通用Skill感知、模型输入和录制（含已挂监听器/待发事件），服务端强制敏感模式至安全退出；在DOM取值前阻断name/value两条路径，不只擦除最终value，也不能只检查type=password。密码改成text、OTP/token、账号可识别输入均不得进入模型/通用事件；专用区域通道仍工作。非敏感页面原接口/行为保持不变，不新增宽泛脱敏框架。
- [ ] **T3 持久调度/节点执行/存储**：`backend/services/browser_account_scheduler.py`、`backend/browser_account_runtime.py`（均新）、`backend/agent_server.py`、`backend/api/v1/nodes.py`、`backend/ws_agent_manager.py`、`backend/acquisition/runner.py`、`backend/config.py`、`backend/api/v1/browser_containers.py`、`agent/entrypoint.sh`、`chrome/entrypoint.sh`、`agent/Dockerfile`、`docker-compose.yml`、`docker-compose.build.yml`。按 Design Notes 实现；账号命令经节点认证HTTP有界claim/ack/renew，WS只加速通知；纳入Agent安装包依赖。中心重启从命令/租约恢复；失联、容量不足入持久等待，不循环遍历休眠账号。容器及shell节点只有具备受监督独立浏览器/Bridge/VNC子进程、持久卷与fencing能力才能接受账号任务，否则明确 capability_missing，不降级到固定共享CDP。用单一显式 PROFILE_DIR 参数贯通镜像挂载与启动器。
  每活跃会话独占隔离运行栈：DISPLAY、CDP、BBX、OpenCLI daemon、HOME/缓存/环境与Profile均不可串用；容器可复用内部固定端口，宿主不为每账号预留端口，仅为活跃容量分配。`scripts/chrome-pool.sh` 不得启动/删除账号托管槽；`backend/acquisition/runner.py` 既有managed匿名保障保持独立，新增账号分支不得借用个性化Profile满足anonymous要求。
  node_id为中心持久分配身份而非URL，管理员授权注册并签发节点独立可撤销凭据，URL仅连接属性；claim/renew/上报只能操作该节点获授权的账号/命令，不信任共享fleet token自报身份。容量事实含受监督槽上限/占用/可用磁盘、boot_id与过期时间，失效报告不产生可调度容量。
  在导航登录页或接受凭据输入前，通过受管理浏览器配置禁用内置密码保存、密码凭据自动登录/自动填充，并核验实际浏览器策略生效；未生效拒绝密码输入流程。此限制不禁止Cookie/session复用的自动登录，只约束内置密码管理器，不承诺第三方页面永不自行存储输入。
- [ ] **T4 现有执行链切换**：`backend/pipeline/pipeline.py`、`backend/channels/opencli_channel.py`、`backend/channels/skill_channel.py`、`backend/browser_pool.py`、`backend/workflow/runtime_resources.py`、`backend/workflow/fleet_inventory.py`、`backend/workflow/runtime_registry.py`、`backend/workflow/opencli_hda_tracer.py`、`backend/schemas/workflow.py`。把 workspace/account_id 从运行上下文传至共用账号服务，选取真实 session/lease 而不是推导锁ID；不把账号控制字段传给平台CLI。账号路径不走任意池回退，健康与认证分开；非账号匿名采集保留独立匿名槽。旧显式 endpoint 未知必须失败；修复多队列竞争/取消token归还及Local/Redis元数据协议一致。下游使用同一已领取会话，不能再次领取不同槽或嵌套死锁。III已有collect分派同样传递账号引用/租约并校验，不能绕回共享CDP；不另建采集执行器。
  同时修改 `backend/agent_runtime_dispatch.py`、`backend/agent_runtimes/bbx_adapter.py`、`iii/workers/collector-opencli/src/main.py`、`iii/lib/opencli_cli.py`、`backend/workflow/iii_collection_dispatch.py` 的真实会话封套传递：服务端注入租约指定CDP/BBX remote/daemon与隔离环境，拒绝用户覆盖；HTTP与WS必须一致。不仅新增account_id字段；III中固定payload hash/lineage应覆盖账号绑定revision且沿用原生命周期记录。命令只引用既有execution与结构化能力，不接受任意shell。
- [ ] **T5 旧数据/调用者迁移**：`backend/services/browser_service.py`、`backend/api/v1/browsers.py`、`backend/schemas/browser.py`、T1迁移、T4调用者及 `frontend/components/browsers/browser-bindings-panel.tsx`。先停相关任务，按实际物理Profile归并并校验唯一写者，备存旧映射清单；仅workspace归属可证明者导入待确认账号，其余管理员认领。共享Profile只导入一个账号，不按site拆复制；旧site默认选择改成workspace内显式account引用，同站多账号必须明确选择。改写旧source/任务/工作流绑定；歧义阻塞并可列出修复，不留隐式兼容回退。迁移成功后移除全局site约束、旧绑定API/UI和全部生产调用，保留审计映射及原Profile；不自动删除数据。
  旧Profile导入前检查浏览器密码凭据元数据；存在历史密码项或无法可靠判定时标记待处理并阻塞安全导入，不静默解密、删除、复制密码。不能仅凭Login Data文件存在判定有密码，也不能删除整个库“修复”。
- [ ] **T6 独立账号界面**：`frontend/app/(app)/browser-accounts/page.tsx`、`frontend/components/browsers/browser-accounts-panel.tsx`、`frontend/lib/api/browser-accounts.ts`（均新），`frontend/lib/navigation.ts`、`frontend/app/(app)/browsers/page.tsx`、`frontend/components/browsers/browser-spaces-panel.tsx`、`frontend/lib/api/browser-spaces.ts`。账号页独立于Chrome池；选择平台/添加账号后自动显示QR或原生表单、刷新和成功保存进度；常规仅扫码/手机确认或必要表单输入，不额外点确认/保存。显示规则自动能力、证据来源、账号身份与runtime健康；challenge可展开同会话，unknown显式“非自动”及异常人工确认，不能静默退回旧默认流程。账号游标搜索，资源页只显示活跃槽及账号链接。
  同时修改 `frontend/components/flow/inspector.tsx` 的OpenCLISourceEditor、`frontend/lib/workflow/node-catalog.ts` 的OpenCLISourceSlot/isOpenCLISourceSlotArray/buildOpenCLIMultiSourceHDAInternals、`frontend/lib/workflow/source-business-config.ts` 的openCLISlotFromDataSource/sourceSlotKey：accountId及固定sourceBindingRevisionId在保存→加载→展开→发布→执行不丢失；同站同参数不同账号不能被去重成一个。替换旧profileId/profileBinding/sessionPolicy中的账号路由语义，不保留冲突双源。
- [ ] **T7 回归与交付**：`tests/unit/test_browser_pool.py`、`tests/unit/test_browser_space_service.py`、`tests/unit/test_browser_account_service.py`、`tests/integration/test_browser_account_cluster.py`（后两者新）覆盖矩阵真实边界；更新既有workflow/channel断言，不固定文案/内部调用。`scripts/verify_browser_account_capacity.py`（新）作显式结构容量实验。更新已有 `README.md`、`TESTING.md` 的操作/部署/恢复边界及验收说明；本规划不运行这些命令。

**Acceptance Criteria:**
- Given T1百万至千万休眠记录，When分页或claim，Then索引有界、无全量账号反序列化，新增休眠记录不新增进程或轮询项。
- Given T2跨workspace或无权用户，When建立新HTTP/WS连接，Then拒绝访问且不泄露账号资料；Given其他副本持有既有流，When撤销/权限变更提交，Then≤1秒停止转发并断连，无法刷新授权也fail closed，到期按deadline硬断，不要求追回在途帧。
- Given T3两个API/两个调度者及节点断网，When租约过期/中心重启，Then无双写、旧结果不提交、未隔离节点账号不被迁移；同节点重启以持久代际拒绝旧命令。
- Given T4原workflow/pipeline和同站两个账号，When各自按account_id执行，Then分别使用各自真实恢复会话且输出仍进入原处理链；节点不可用不换账号。
- Given编辑器选择同站两账号并发布固定SourceBindingRevision，When重载、HDA展开、HTTP/WS/III及BBX分别执行，Then账号引用不丢失，CDP/remote/daemon/环境均属于正确会话，匿名分支仍只能使用匿名Profile。
- Given T5旧映射归属冲突，When升级，Then数据保留、受影响任务可见阻塞，修复后不再依赖全局site或endpoint默认。
- Given已实测登录规则与有效部署，When用户添加账号后扫码并在手机确认，Then无需点“已登录/保存”即可信验证、自动保存休眠，重启节点后按account_id恢复；平台要求重扫时诚实暂停。
- Given二维码消失/HTTP200/跳转但缺规则身份或认证状态，When探测完成，Then不保存成功；Given重认证身份不是原绑定身份，Thenaccount_identity_mismatch且不覆盖旧Profile/绑定。
- Given旧码刷新与扫码回调/重复成功探测交错，When generation已变化或会话到期，Then旧投影/输入/证据拒绝；当前可信证据最多提交一个版本，刷新停止后才冻结保存。
- Given含密码/验证码的登录区域及错误/超时，When截QR、转发表单输入或输出审计，Then无跨origin候选/其他账号内容，DB/持久消息/日志/trace/事件/落盘截图均无秘密；临时QR流只含当前批准二维码，不夹带表单密码。复杂挑战仍在同一浏览器完成。
- Given登录敏感会话中password被切为text、attribute value/name含秘密及OTP字段，When通用Skill或已挂录制器尝试采集，Thenname/value均不生成敏感输出且模型/事件无数据；退出敏感模式后普通页面原接口仍可正常工作。
- Given受管理配置的新Profile，When在受控真实站点密码登录→退出浏览器→快照→恢复，Then无保存密码/自动填密码行为、未新增浏览器密码凭据且Cookie/session存储仍正常；报告只含策略状态和凭据数量/存在性，不导出密码。策略未生效或旧Profile检查不明必须拒绝对应输入/导入。
- Given缺规则或平台布局改变，When用户添加账号，Then明确非自动/unknown并可同会话原生接管；人工兜底记录manual_fallback，不产生rule_verified证据。

## Spec Change Log

- 2026-09-05：实施合同核查发现 `backend/models/identity.py:40-55` 的WorkspaceMembership没有成员revision，旧T2说明误引了不存在字段；改为有界批量读取当前权限事实及新鲜度判断，避免A依赖虚构schema或另加分叉模型。保留≤500ms检查、≤1秒撤销fail closed、deadline硬断、workspace隔离与非持久敏感通道不变量；用户批准范围、frozen字节、批准历史hash和baseline均不变。

## Design Notes

1. **租约与实际隔离**：PostgreSQL为生产权威；事务CAS或 `FOR UPDATE SKIP LOCKED` 原子领取账号+容量+命令，递增epoch并绑定node boot_id。续租仅匹配owner/epoch；结果同样CAS。节点持久记录最大epoch、启动前核验租约，独立监督器按保守单调时钟deadline停止任务进程树、Chromium与代理，停止前不确认释放；监督器死亡也应由容器/服务管理器停止子进程。无法确认旧写者消失则 quarantined，不因DB租约到期重新分配该Profile。外部副作用结果不确定不得盲目重放；沿用原workflow恢复/人工确认语义。
2. **存储与恢复**：账号分配一次性随机profile_id，DB只存定位/版本/校验清单，目录按workspace/profile分层，拒绝越界/符号链接。归属节点专用持久卷是权威存储，节点重建不能换成空卷；部署准入要求磁盘加密、最小文件权限、容量/配额检查。仅浏览器完全停止后，流式生成不可变快照到独立版本目录，校验文件清单/字节数/hash及完成标记，fsync后原子切换current，再提交DB版本；数据库提交失败按command_id幂等对账，保留上个完整版本。恢复先校验已提交manifest与实际数据，再生成隔离工作目录并原子就位；失败保留证据、不启动。只首次创建新账号允许初始化空Profile；不把巨大归档读入内存或base64塞DB，也不复用字符串Fernet做大文件存储。
  新托管Profile不得新增内置密码管理器保存的密码凭据；快照提交前按停止态元数据核验，异常阻塞提交，不能让秘密随完整Profile成为有效快照。核验不得解密/导出密码；历史项处理遵循T5，不自动删Login Data，Profile会话状态保留不受此限制影响。
  磁盘加密依赖部署设施：上线验收必须由运维提供实际承载Profile卷的加密配置/挂载映射与审查记录；应用仅记录证明和准入结果，不能凭数据库布尔字段、路径或节点自报证明卷已加密，也不宣称已实现自动检测。
3. **可用性边界**：无需虚构对象存储或新备份管理。同节点完整持久卷可自动恢复；节点永久丢盘且无有效离线副本时明确 profile_lost，用户重扫形成新版本，不称旧Profile恢复。显式迁移要求管理员提供并审计旧节点断电或存储/执行网络撤销证据；停止态完整快照通过节点认证TLS流式传输，目标校验和持久化后事务切换归属，旧节点保持隔离。设备绑定/浏览器版本不兼容可要求重扫；不强行读取或删除Singleton锁抢占。
  非正常退出留下的dirty工作目录不可冒充已提交版本；先停净旧栈并隔离留证，恢复操作明确选择核验通过的已提交版本或重新扫码，不静默回滚登录态。保留current与前一完整版本，未引用的中间文件在无活租约后有界回收；这不等于删除账号Profile，也不构成独立备份产品。
4. **登录证据与关联**：成功规则必须同时取得可信已认证端点/身份标识及规则定义的页面认证状态，绑定当前session、精确origin、rule版本、document与view generation；Cookie非空、QR消失、HTTP200、URL变化不能单独或组合冒充可信身份。探测采用有界频率/超时，刷新后废弃旧证据；可疑候选转unknown，不能猜。新账号身份原子关联，workspace/site同身份冲突阻塞提示已有账号而非静默合并；重认证必须匹配原身份，禁止自动换绑。
  没有稳定外部身份探测能力时保持unknown并明确人工兜底，不能为省一次点击推定账号正确；新账号只能由本次会话可信证据自动建立身份关联，人工来源不伪造稳定身份。重登身份不符时丢弃自动提交资格、保留原已提交Profile和关联，隔离本次工作副本等待处理。
  验证通过后CAS进入saving，停止刷新与用户输入、撤销区域控制，冻结同一会话后停止浏览器并提交Profile；保存错误保留证据，不重复登录或把失败显示saved。保存来源rule_verified与平台证据只描述该时刻，不保证未来有效。无可靠规则/复杂挑战无法探测时才允许人工异常confirm；记录确认人/会话/版本，platform_evidence=unknown。用户仍可明确授权此人工会话执行，但UI不能称自动支持；既有渠道可信认证错误仍暂停账号任务。
  saving前过期/撤销拒绝新的成功提交；已CAS进入saving后只撤销控制流，继续幂等完成已验证快照，不能因门户断线遗失保存结果。平台导航产生新的document/view代际后须重新采集完整证据，既不能使用旧成功回调，也不能把允许的正常认证跳转误作永久失败。

## Verification

**Commands:** 实施后在专用环境运行，规划阶段未运行；新命令对应T7交付。
已知基线由独立worker真实模块probe证明：Local三槽未路由获取/释放available为3→0→1；请求缺失A实际得到B；显式存在A为3→2→3；RedisPool实际调用get_agent_url抛AttributeError（未连接Redis）。这些不是分布式浏览器或真实平台验收。
- `uv run pytest --no-cov tests/unit/test_browser_pool.py tests/unit/test_browser_space_service.py tests/unit/test_browser_account_service.py`：回归token守恒、取消释放、严格路由、workspace/确认状态机。
- `uv run pytest --no-cov tests/integration/test_browser_account_cluster.py`：使用独立 `TEST_DATABASE_URL_PG`、两个中心副本和受监督节点验证重复claim、过期命令、断网停机、恢复损坏、端到端权限与原执行链；不得用SQLite证明分布式锁。
- T7新增 `tests/integration/test_browser_account_login.py` 与 `tests/fixtures/browser_account_login_app.py`：受控真实浏览器站点复现多origin/多QR/刷新竞态/身份不符/密码泄漏/挑战及假成功；运行 `uv run pytest --no-cov tests/integration/test_browser_account_login.py`，证明状态与隔离协议，不宣称真实平台已支持。使用已有Bundle/Script Host集成测试补规则版本与打包动作边界，不用mock回声替代浏览器行为。
- `uv run python scripts/verify_browser_account_capacity.py --accounts 10000000 --page-size 100 --claim-size 100`：仅对显式配置的独立容量数据库执行；生成无真实凭据的休眠元数据；保存EXPLAIN ANALYZE、RSS、p95与活跃进程数。同硬件/固定活跃数由100万扩至1000万，查询无Seq Scan/无全量物化，分页返回≤100、claim≤100，中心RSS增量≤20%，活跃进程数不随账号存量增长；性能结果不冒充真实平台吞吐。
- `docker compose -f docker-compose.yml -f docker-compose.build.yml config`：验证两种镜像Profile挂载与代理隔离配置，再分别部署默认/构建组合检查真实持久卷。

**Manual checks:** 对实际已配置且实测通过的规则，完成选择平台→自动登录区域→扫码/手机必要确认→自动可信验证/停止/保存/休眠→节点重启→account_id恢复，确认常规无额外“已登录/保存”；另验无规则unknown接管、复杂挑战、证据失配、旧码竞态、密码不留痕及隔离迁移。逐规则记录平台/版本/时间/证据，不设用户先给平台名单的门槛，也不把受控站点或空规则算真实平台支持。当前Docker daemon不可连接，不能宣称已完成真实QR、容器重启、设备绑定或平台容量验收；实施后须取得对应证据才可宣告完成。
