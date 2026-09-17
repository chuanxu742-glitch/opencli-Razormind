import assert from 'node:assert/strict'
import { test } from 'node:test'
import { browserAccountErrorText } from '../lib/browser-accounts/error-text.ts'

test('account detail preserves readable message and maps known codes', () => {
  assert.match(browserAccountErrorText({ code: 'other', message: 'Session is stale' }), /操作失败/)
  assert.match(browserAccountErrorText({ code: 'capacity_full', message: 'capacity_full' }), /节点正忙/)
  assert.match(browserAccountErrorText({ code: 'login_resources_unavailable' }), /暂无可用登录资源/)
})
test('objects and malformed errors never appear as object coercions', () => {
  for (const error of [{ message: {} }, new Error('[object Object]'), null, {}]) {
    assert.match(browserAccountErrorText(error), /操作失败/)
    assert.ok(!browserAccountErrorText(error).includes('[object Object]'))
  }
  assert.equal(browserAccountErrorText(new Error('Readable')), 'Readable')
})

test('node diagnostics use static reasons and never reflect unknown node text', () => {
  for (const code of ['login_rule_unknown', 'runtime_binary_missing', 'runtime_bundle_unavailable', 'runtime_readiness_timeout', 'runtime_stop_unconfirmed', 'stale_lease', 'epoch_state_corrupt']) {
    const text = browserAccountErrorText({ code, message: 'SECRET https://user:password@node/private' })
    assert.match(text, /请/)
    assert.ok(!text.includes('SECRET'))
  }
  for (const code of ['SECRET /private/path', 'toString', '__proto__', null, {}, 42]) {
    assert.equal(browserAccountErrorText({ code, message: 'SECRET' }), '浏览器账号操作失败，请刷新账号状态后重试。')
  }
})
