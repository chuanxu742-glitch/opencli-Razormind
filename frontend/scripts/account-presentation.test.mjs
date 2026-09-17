import assert from 'node:assert/strict'
import test from 'node:test'
import { presentBrowserAccount } from '../lib/browser-accounts/account-presentation.ts'

const account = {
  id: 'a', workspace_id: 'w', site: 'example.com', label: '账号', node_id: null,
  profile_id: 'profile', profile_version: 1, profile_manifest_id: 'manifest',
  runtime_bundle_id: null, runtime_bundle_version: null, login_rule_id: null, login_rule_version: null,
  auth_required: false, platform_identity: { provider: 'example', subject: 'user' },
  auth_evidence: 'valid', evidence_source: 'rule_verified', evidence_observed_at: null,
  manual_confirmed_by: null, status: 'saved', revision: 1, paused: false, status_reason_code: null,
  created_at: '', updated_at: '',
}

test('only a trusted rule-verified committed profile is presented as verified', () => {
  assert.equal(presentBrowserAccount({ ...account, status: 'verifying' }).label, '已登录')
  assert.equal(presentBrowserAccount({ ...account, status: 'verifying', auth_evidence: 'unknown' }).label, '正在验证')
  assert.equal(presentBrowserAccount(account).label, '已验证')
  assert.equal(presentBrowserAccount({ ...account, profile_manifest_id: null }).label, '尚未验证')
  assert.equal(presentBrowserAccount({ ...account, profile_version: 0 }).label, '尚未验证')
  assert.equal(presentBrowserAccount({ ...account, evidence_source: 'manual_fallback' }).label, '尚未验证')
  assert.equal(presentBrowserAccount({ ...account, platform_identity: null }).label, '尚未验证')
})

test('operational state takes priority over historical evidence and reauthentication', () => {
  for (const status of ['closed', 'expired', 'opening', 'presenting', 'saving', 'verifying']) {
    assert.equal(presentBrowserAccount({ ...account, auth_evidence: 'unknown', status }).canStartLogin, false)
  }
  for (const status of ['opening', 'presenting', 'challenge', 'saving']) {
    assert.notEqual(presentBrowserAccount({ ...account, status, auth_required: true }).action, 'relogin')
  }
  assert.equal(presentBrowserAccount({ ...account, auth_evidence: 'unknown', paused: true }).label, '已暂停')
  assert.equal(presentBrowserAccount({ ...account, auth_evidence: 'unknown', status: 'challenge' }).action, 'verify')
  assert.equal(presentBrowserAccount({ ...account, auth_evidence: 'unknown', status: 'dormant' }).label, '待登录')
  assert.equal(presentBrowserAccount({ ...account, status: 'presenting' }).label, '正在登录')
  assert.equal(presentBrowserAccount({ ...account, status: 'saved', paused: true }).label, '已暂停')
  assert.equal(presentBrowserAccount({ ...account, status: 'saved', auth_required: true }).label, '需要重新认证')
  for (const auth_required of [true, false]) {
    const failed = presentBrowserAccount({ ...account, status: 'error', auth_required })
    assert.equal(failed.label, '登录异常')
    assert.equal(failed.action, 'relogin')
    assert.equal(failed.canStartLogin, true)
  }
})

test('visible webpage login is distinct from trusted identity and cannot hide challenge or failure', () => {
  const observed = { ...account, auth_evidence: 'unknown', evidence_source: null,
    platform_identity: null, status: 'presenting', status_reason_code: 'browser_login_observed' }
  assert.equal(presentBrowserAccount(observed).label, '网页已登录')
  for (const status of ['challenge', 'error', 'saving', 'closed', 'expired']) {
    assert.notEqual(presentBrowserAccount({ ...observed, status }).label, '网页已登录')
  }
  assert.notEqual(presentBrowserAccount({ ...observed, auth_required: true }).label, '网页已登录')
  assert.equal(presentBrowserAccount({ ...observed, status_reason_code: null }).label, '正在登录')
})


test('authenticated platform session is shown independently of trusted task identity', () => {
  const detected = { ...account, auth_evidence: 'unknown', evidence_source: null,
    platform_identity: null, status: 'presenting', status_reason_code: 'browser_session_verified' }
  for (const status of ['presenting', 'unknown', 'verifying']) {
    assert.equal(presentBrowserAccount({ ...detected, status }).label, '已登录')
  }
  for (const status of ['challenge', 'error', 'saving', 'closed', 'expired', 'saved']) {
    assert.notEqual(presentBrowserAccount({ ...detected, status }).label, '已登录')
  }
  assert.notEqual(presentBrowserAccount({ ...detected, auth_required: true }).label, '已登录')
  assert.notEqual(presentBrowserAccount({ ...detected, status_reason_code: null }).label, '已登录')
})
