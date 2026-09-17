const messages: Record<string, string> = {
  capability_missing: '节点缺少启动浏览器所需能力，请管理员检查节点配置后重试。',
  login_rule_unknown: '节点未安装此账号需要的登录规则，请管理员同步运行包后重试。',
  runtime_context_invalid: '浏览器启动上下文校验失败，请刷新账号状态；仍失败时请管理员检查节点配置。',
  runtime_bundle_unavailable: '节点运行包不可用，请管理员检查运行包安装后重试。',
  runtime_bundle_incompatible: '节点运行包与账号要求不兼容，请管理员同步匹配版本后重试。',
  runtime_binary_missing: '节点未找到浏览器程序，请管理员安装或配置浏览器后重试。',
  runtime_platform_unsupported: '节点系统不支持当前浏览器运行方式，请管理员检查部署平台。',
  runtime_configuration_invalid: '节点浏览器配置无效，请管理员检查启动配置后重试。',
  runtime_endpoint_invalid: '浏览器连接地址配置无效，请管理员检查节点连接配置。',
  runtime_process_exited: '浏览器进程在启动时退出，请管理员检查节点浏览器运行环境。',
  runtime_readiness_timeout: '浏览器启动超时，请管理员检查节点资源与浏览器状态后重试。',
  runtime_stop_unconfirmed: '尚未确认浏览器已停止，资源仍被保留，请管理员检查并完成安全停止。',
  profile_locked: '账号浏览器资料仍被占用，请管理员确认原浏览器安全停止后重试。',
  profile_dirty: '账号浏览器资料未正常关闭，请管理员检查并安全恢复资料。',
  profile_missing: '节点缺少账号浏览器资料，请管理员检查资料同步状态。',
  claim_expired: '浏览器启动授权已过期，请刷新账号状态后重试。',
  lease_lost: '浏览器资源租约已失效，请刷新账号状态并等待节点完成安全清理。',
  duplicate_claim: '浏览器启动授权已被使用，请刷新账号状态，避免重复启动。',
  capacity_missing: '节点暂时无法分配浏览器资源，请稍后重试；仍失败时请管理员检查端口占用与节点容量。',
  credential_policy_unverified: '节点凭证保存策略尚未通过验证，请管理员检查凭证策略。',
  stale_boot: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_claim: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_epoch: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_lease: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_owner: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_runtime_binding: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  stale_portal_registration: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  lease_deadline_invalid: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  lease_owner_invalid: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  epoch_invalid: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  epoch_lock_unavailable: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  epoch_state_corrupt: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  epoch_state_invalid: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  node_identity_mismatch: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  session_claim_mismatch: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  result_claim_mismatch: '浏览器租约或节点代际校验失败，请刷新账号状态；仍失败时请管理员检查租约与节点状态。',
  profile_not_stopped: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_path_invalid: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_symlink: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_file_invalid: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_identity_mismatch: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_manifest_incompatible: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_manifest_invalid: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_state_overlap: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_version_ambiguous: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_version_invalid: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  profile_version_missing: '账号浏览器资料校验失败，请管理员检查资料完整性、版本与占用状态后重试。',
  password_database_unlisted: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  password_inventory_blocked: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  password_inventory_changed: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  password_inventory_unbounded: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  password_manifest_mismatch: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  password_manifest_unverified: '账号凭证资料安全校验失败，请管理员核查凭证清单与保存策略后重试。',
  identifier_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  metadata_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  path_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  path_permissions: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  path_symlink: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  state_path_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  runtime_state_corrupt: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  runtime_state_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  runtime_command_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  runtime_supervisor_running: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  runtime_save_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  portal_record_missing: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',
  portal_registration_invalid: '节点浏览器运行状态校验失败，请管理员检查运行配置与状态后重试。',

  login_resources_unavailable: '暂无可用登录资源，节点可能正忙，请稍后重试。',
  capacity_full: '账号节点正忙，请关闭其他登录会话后重试。',
  stale_generation: '账号状态已更新，请刷新后重试。',
  account_identity_mismatch: '登录身份与此账号不一致，请确认使用了正确账号。',
}

export function browserAccountErrorText(error: unknown): string {
  if (error && typeof error === 'object') {
    const detail = error as { code?: unknown; message?: unknown }
    if (detail.code !== undefined) {
      return typeof detail.code === 'string' && Object.hasOwn(messages, detail.code)
        ? messages[detail.code]
        : '浏览器账号操作失败，请刷新账号状态后重试。'
    }
    if (typeof detail.message === 'string' && detail.message.trim() && detail.message !== '[object Object]') return detail.message
  }
  return typeof error === 'string' && error.trim() && error !== '[object Object]'
    ? error : '浏览器账号操作失败，请刷新账号状态后重试。'
}
