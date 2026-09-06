# QRAC2 G H1-H5 聚焦修复报告

- 修复提交：`f2c37cb4fb3338807b60491a3ff9895c85599f23`
- 依赖既有 G 提交：`52ada5ea480e0f0a1ee90a6228774ff2a083cbb0`
- 冻结规格：`_bmad-output/implementation-artifacts/spec-qr-account-cluster.md`
- 实施计划：`_bmad-output/implementation-artifacts/plan-qr-account-cluster-parallel.md`
- 变更范围：既有 G 合同文件 `backend/schemas/browser_account.py`，以及必要的聚焦回归 `tests/unit/test_qrac2_g_contract_repairs.py`。
- 未修改 A、S、R1、E1、U、服务实现、API 路由或冻结 SPEC。

## H1：C3 命令/Schema 冻结与前置守卫

新增 `QRAC2_CONTRACT_VERSION=1`，持久命令、Claim、Result、观察、门户帧和票据合同拒绝未知版本。新增 `CommandExecutionGuardV1`，在任何持久化或副作用调用前校验 command/claim/session 的 workspace、account、command、session、node、boot、epoch、expected revision 全链路关联；同时校验 login-rule version 和 refresh view-generation，过期或不一致直接拒绝。

证据：`test_h1_freezes_command_and_rejects_stale_guard_before_side_effects` 覆盖 unsupported contract version、workspace mismatch、stale rule version；5-test 聚焦运行通过。

## H2：可信 AccountStateView

新增 `AccountStateSnapshotV1` 作为同一可信 DB 快照，携带 account/session revision、epoch、trusted identity、auth evidence、account status、real target、rule/version、view generation 和不超过 1 秒的新鲜窗口。`AccountStateViewV1` 同时暴露这些字段并逐字段与 snapshot 比较；身份、认证状态、目标、generation、revision、来源或 freshness 不一致均 fail closed，VALID evidence 无可信身份时拒绝。

证据：`test_h2_state_view_requires_same_trusted_snapshot` 覆盖一致快照和 revision mismatch；schema validator 同时覆盖无身份认证状态。

## H3：Observation ↔ S Claim/Node 关联

`NodeClaimV1` 现在显式携带 workspace/account/command/session/node/boot/epoch/revision。`LoginObservationV1` 必须携带 typed `NodeClaimV1` 与 authenticated `NodeIdentityV1`，并在构造阶段校验 workspace、account、session、epoch、node、boot、claim lifetime、rule、target、view generation 和 trusted identity；不匹配或已过 claim deadline 不允许进入 A/S CAS。

证据：`test_h3_observation_requires_claim_and_authenticated_node_linkage` 覆盖有效关联和 account mismatch 拒绝。

## H4：C2 ticket/CSRF first-entry

`PortalTicketRedeemRequestV1` 明确 `first_entry`、AccountRef、session id、expected session revision、body-only ticket 和 CSRF secret。成功响应 `PortalTicketGrantV1` 固定 `status="granted"`；等待和阻塞分别使用 `PortalEntryWaitingV1`、`PortalEntryBlockedV1`，携带原因/错误码但不携带 grant 路由事实，不能被误解为登录成功。

证据：`test_h4_ticket_entry_responses_cannot_be_mistaken_for_grants` 证明 waiting/blocked 与 granted 是不相等的判别状态。

## H5：MIME/bytes 与 A→R owner boundary

`PortalPixelFrameV1` 增加 allowlisted MIME、显式 byte length、最大 4 MB 限制及 PNG/JPEG/WebP magic-byte 校验。新增 `PortalRegionFocusV1`，承载 L 的 approved regions、focus、target 和 generation；新增 `PortalOwnerRouteV1`，由 A 侧 owner 标识、节点身份、session revision、过期时间、4 MB frame/4 KB input 上限以及 session→page→record (`record_session_id`) lineage，并拒绝目标/generation 不一致或没有 record binding 的路由。

证据：`test_h5_pixel_mime_bytes_and_owner_route_are_bounded` 覆盖有效 PNG、错误 MIME 和缺 record session 拒绝。

## 验证命令与结果

通过：

```text
uv run --no-sync pytest -q -o addopts='' --confcutdir=tests/unit tests/unit/test_qrac2_g_contract_repairs.py
5 passed in 1.14s

uv run --no-sync python -B -m py_compile backend/schemas/browser_account.py tests/unit/test_qrac2_g_contract_repairs.py

git diff --check
```

首次直接运行 pytest 受到仓库全局 `tests/conftest.py` 导入链缺失 `python_calamine` 阻断；使用 `--confcutdir=tests/unit` 和空 `addopts` 后，仅运行上述 G 聚焦测试并通过。未运行全套测试、formatter、lint、build、重型 PostgreSQL/Alembic、Redis、容器或真实浏览器验证。

## 剩余依赖

A/S/R1/E1/U 仍需消费该合同并实现真实服务/路由/节点/执行链/前端；综合验证仍需 Main 保留的重型 PG 多副本、节点 fencing、WS/浏览器、容器和跨片回归槽。合同和聚焦测试不宣称这些业务或基础设施已完成。
