import type { BrowserAccount, BrowserAccountStatus } from '@/lib/api/browser-accounts'

export type AccountPrimaryAction = 'login' | 'relogin' | 'continue' | 'verify' | 'view'

export interface AccountPresentation {
  label: string
  status: BrowserAccountStatus
  action: AccountPrimaryAction
  canStartLogin: boolean
}

const labels: Record<BrowserAccountStatus, string> = {
  opening: '正在登录', presenting: '正在登录', refreshing: '正在登录', verifying: '正在验证',
  challenge: '需要处理', unknown: '尚未验证', saving: '正在保存', saved: '尚未验证',
  dormant: '休眠中', expired: '已过期', closed: '已关闭', error: '登录异常',
}

export function hasTrustedBrowserAccountIdentity(account: BrowserAccount) {
  const evidence = typeof account.auth_evidence === 'string' ? account.auth_evidence : account.auth_evidence.kind
  return evidence === 'valid'
    && account.evidence_source === 'rule_verified'
    && Boolean(account.platform_identity?.provider && account.platform_identity?.subject)
    && Boolean(account.profile_id && (account.profile_version ?? 0) > 0 && account.profile_manifest_id)
    && !account.auth_required
}

export function presentBrowserAccount(account: BrowserAccount): AccountPresentation {
  if (account.paused) return { label: '已暂停', status: account.status, action: 'view', canStartLogin: false }
  if (account.status === 'closed' || account.status === 'expired') return { label: labels[account.status], status: account.status, action: 'view', canStartLogin: false }
  if (account.status === 'error') return { label: labels.error, status: account.status, action: 'relogin', canStartLogin: true }
  if (account.status_reason_code === 'browser_session_verified' && !account.auth_required
    && ['presenting', 'unknown', 'verifying'].includes(account.status)) {
    return { label: '已登录', status: 'saved', action: 'continue', canStartLogin: false }
  }
  if (account.status_reason_code === 'browser_login_observed' && !account.auth_required
    && ['presenting', 'unknown'].includes(account.status)) {
    return { label: '网页已登录', status: 'unknown', action: 'continue', canStartLogin: false }
  }
  if (account.status === 'challenge' || account.status === 'unknown') return { label: labels[account.status], status: account.status, action: 'verify', canStartLogin: true }
  if (account.status === 'verifying' && hasTrustedBrowserAccountIdentity(account)) {
    return { label: '已登录', status: 'saved', action: 'continue', canStartLogin: false }
  }
  if (['opening', 'presenting', 'refreshing', 'verifying'].includes(account.status)) return { label: labels[account.status], status: account.status, action: 'continue', canStartLogin: false }
  if (account.status === 'saving') return { label: labels[account.status], status: account.status, action: 'view', canStartLogin: false }
  if (account.auth_required) return { label: '需要重新认证', status: account.status, action: 'relogin', canStartLogin: true }
  if (hasTrustedBrowserAccountIdentity(account) && ['saved', 'dormant'].includes(account.status)) return { label: '已验证', status: account.status, action: 'login', canStartLogin: true }
  if (account.status === 'dormant') return { label: '待登录', status: account.status, action: 'login', canStartLogin: true }
  return { label: labels[account.status], status: account.status, action: 'login', canStartLogin: true }
}

export function accountActionLabel(action: AccountPrimaryAction) {
  return { login: '打开浏览器', relogin: '打开浏览器', continue: '打开浏览器', verify: '打开浏览器', view: '查看' }[action]
}
