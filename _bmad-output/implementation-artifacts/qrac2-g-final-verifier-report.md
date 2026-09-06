# QRAC2 G 最终验证修复报告

## 结论

本轮仅处理最终 G verifier follow-up 的四项强制阻塞：H1、H3、H4、H5。实现提交为 `7469b360d519ec5c76d4f37e7b495e923388372d`；未修改冻结 SPEC、并行计划及 A/S/R1/E1/U 的所有权文件。

冻结依据：`_bmad-output/implementation-artifacts/spec-qr-account-cluster.md` 的 `<frozen-after-approval>`、I/O 矩阵第 45—47 行、T2 第 79—82 行、T2a 第 83—86 行、AC02/AC08/AC09/AC10/AC11；并行计划 `plan-qr-account-cluster-parallel.md` 的 C1—C3（第 44—64 行）以及 G 卡“无 stub service、按实际函数/HTTP 路由/错误映射交付”的约束。

按任务说明尝试读取 `local://qrac2-g-review-f2c37cb.md`，本 worker 上下文返回 `Local file not found`，因此没有伪造该附件内容；以下 H1/H3/H4/H5 对应任务中已明确的 verifier findings，并以冻结规范和本次可运行证据为准。

## H1：前置守卫语义

变更：

- `backend/schemas/browser_account.py` 增加 `CommandExecutionGuardV1`，在 command/claim/session 进入执行边界时校验 contract version、workspace/account、command/session/epoch/revision、node/boot、login rule version 和 view generation。
- `backend/services/browser_portal_contract.py` 提供真实 `admit_command_before_side_effects(...)` 函数；只有返回 guard 后，调用者才允许持久化结果、发起浏览器副作用或发事件。该函数不写 DB、不启动浏览器，避免把“校验成功”与副作用混在一起。
- 测试覆盖有效 tuple 通过、unsupported version、workspace ownership mismatch、stale login-rule version 在构造/入口处拒绝。

对应冻结要求：SPEC Always 的 workspace/account/session/target/command/lease 全链路校验；I/O 第 46—47 行旧代际结果拒绝；AC03、AC09。

证据：`tests/unit/test_qrac2_g_contract_repairs.py::test_h1_freezes_command_and_rejects_stale_guard_before_side_effects` 及 7-test 聚焦运行结果。

## H3：首票据、兑换与 HTTP 语义

变更：

- 明确 `PORTAL_TICKET_ISSUE_ROUTE` 和 `PORTAL_TICKET_REDEEM_ROUTE`，并新增 `PortalTicketIssueRequestV1`、`PortalTicketIssuedV1`、`PortalTicketRecordV1`、扩展 `PortalTicketRedeemRequestV1` 的 ticket_id/CSRF/session revision。
- 首次签发和兑换均为 body-only；响应/请求中的 ticket 与 CSRF 用 `SecretStr`，持久记录只允许 ticket/CSRF digest，禁止把秘密放 URL 或持久 payload。
- `PortalTicketIssuedV1`、`PortalTicketGrantV1`、waiting/blocked 结果携带明确 HTTP status；grant 固定 200，waiting 固定 202，blocked 按 `ERROR_HTTP_STATUS` 映射 403/409/410/503。
- `issue_first_portal_ticket(...)`、`ticket_record_from_issue(...)`、`redeem_portal_ticket(...)` 提供可被 A 实际调用的纯边界逻辑：校验 user/account/session/revision/CSRF/ticket digest、过期、已消费重放；不匹配返回 typed blocked，成功才返回 grant。消费标记由 owner 在同一 DB 事务中原子写入，函数不伪造持久化。
- grant 明确 `HttpOnly`、`Secure`、`SameSite`、Origin 和 CSRF binding 约束字段。

对应冻结要求：SPEC T2 第 79—82 行的单次票据、用户/workspace/account/session 绑定、默认 10 分钟/上限 30 分钟、HttpOnly/Secure、Origin/CSRF、404/403/410/409/503 语义；I/O 第 45 行；AC02、AC08、AC09、AC10。

证据：`tests/unit/test_qrac2_g_contract_repairs.py::test_h3_first_ticket_redeem_binds_csrf_session_and_replay` 覆盖首次签发、正常兑换、已消费 replay=410、session mismatch=409。

## H4：线协议、Owner 路由与有界帧

变更：

- `PortalWireFrameV1` 固化 `qrac2.portal.v1`、sequence、control-json/pixel-binary、content type、MIME、bounded byte length，并要求 wire/transient version 一致、pixel MIME/长度与实际帧一致。
- `PortalPixelFrameV1` 仅允许 PNG/JPEG/WebP，最大 4 MiB，校验 magic bytes 和 `byte_length`；敏感输入最大 4 KiB。
- `PortalEntryWaitingV1` 与 `PortalEntryBlockedV1` 是不能误解为 grant 的 typed outcomes。
- `PortalOwnerRouteV1` 携带 owner、node identity、session revision、短期 route deadline、record-backed binding、L-produced region/focus 及帧/输入上限。
- `route_portal_frame(...)` 实际比较 A-owned route 与 R wire frame 的 workspace/account/session/epoch/target/view-generation，拒绝越界或版本不匹配；不是仅声明 schema。

对应冻结要求：SPEC T2 第 80—82 行安全门户/节点代理限制、T2a 第 84—85 行真实裁剪和短期敏感通道、C2 第 58 行 PortalTransient 编码/绑定要求、C3 第 62 行真实 target/generation/focus 检查；I/O 第 45、47 行。

证据：`test_h4_ticket_entry_responses_cannot_be_mistaken_for_grants`、`test_h5_pixel_mime_bytes_and_owner_route_are_bounded`、`test_h5_owner_route_applies_real_wire_binding`；覆盖 MIME 错配、PNG magic/长度、record route 约束和 wire route mismatch。

## H5：真实 session → page → record 与 L focus → R owner

变更：

- `SensitiveSessionBindingV1` 必须携带完整 target、view generation；`PortalOwnerRouteV1` 要求 `record_session_id` 非空，并强制 route binding 与 `PortalRegionFocusV1` target/view generation 相同。
- `PortalRegionFocusV1` 将 L 产生的 approved regions/focused field 显式传入 R-owned route；form projection 缺少 focused field 直接拒绝。
- `route_portal_frame(...)` 将实际 transient frame 的 binding 与 route binding 对照后才允许返回；测试以真实构造的 SessionTarget、record session、region focus 和 pixel wire 走完整函数，而非只检查字段存在。

对应冻结要求：SPEC Always 第 28 行的精确 tab/frame/document 全链路；T2a 第 84 行区域投影和同一浏览器；C3 第 62—64 行完整 SessionTarget/epoch/rule/view_generation、region/focus 和敏感 guard；AC08—AC11。

证据：`test_h5_owner_route_applies_real_wire_binding` 有效链路通过，session mismatch 被 `ValueError` 拒绝；`test_h5_pixel_mime_bytes_and_owner_route_are_bounded` 缺 record session 的 route 被拒绝。

## 验证命令

以下均在实现提交前后针对本次 G 文件执行：

```text
uv run --no-sync python -B -m py_compile backend/schemas/browser_account.py backend/services/browser_portal_contract.py tests/unit/test_qrac2_g_contract_repairs.py
# exit 0

uv run --no-sync pytest -q -o addopts='' --confcutdir=tests/unit tests/unit/test_qrac2_g_contract_repairs.py
.......                                                                  [100%]
7 passed in 1.54s

# git diff --check --上述三个文件
# exit 0
```

直接使用项目默认 pytest 配置时，现有 `tests/conftest.py` 导入链缺少 `python_calamine`；按计划的 G 轻量范围改用 `--confcutdir=tests/unit`，未修改环境依赖，也未把该环境问题伪装成业务通过。

## 未完成的外部验证

- A 的真实 `browser_account_service.py`/API、S 的持久 claim/CAS、R1 节点封套、L/E/U/E1 消费者仍由各自 owner 实施；本提交只提供 G 的可执行 contract gates，不宣称业务端到端完成。
- 未运行全 suite、formatter、lint、build、PostgreSQL 多副本/Redis、Docker、真实 Chromium/真实平台扫码、跨副本撤销计时、10M 容量实验；这些属于计划中的 Q/I 或各 owner 外部前置。
- `local://qrac2-g-review-f2c37cb.md` 在当前 worker 上下文不可读，需协调员在最终集成环境重新核对原始附件与本报告逐项对应关系。

提交：`7469b360d519ec5c76d4f37e7b495e923388372d`。
