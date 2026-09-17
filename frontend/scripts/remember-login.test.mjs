import assert from 'node:assert/strict'
import { test } from 'node:test'

const now = 1_800_000_000_000
const day = 24 * 60 * 60 * 1000
const key = 'opencli.rememberedLocalLogin'
let moduleId = 0
const freshSession = () => import(`../lib/auth/session.ts?remember-test=${++moduleId}`)
const jwt = (expiresAt = now + 30 * day, authMethod = 'local') => `header.${Buffer.from(JSON.stringify({ auth_method: authMethod, exp: expiresAt / 1000 })).toString('base64url')}.signature`

function storage() {
  const entries = new Map()
  return { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value), removeItem: key => entries.delete(key), clear: () => entries.clear() }
}

function setup(t) {
  const originalSession = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const originalLocal = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { value: storage(), configurable: true })
  Object.defineProperty(globalThis, 'localStorage', { value: storage(), configurable: true })
  t.mock.method(Date, 'now', () => now)
  t.after(() => {
    if (originalSession) Object.defineProperty(globalThis, 'sessionStorage', originalSession)
    else delete globalThis.sessionStorage
    if (originalLocal) Object.defineProperty(globalThis, 'localStorage', originalLocal)
    else delete globalThis.localStorage
  })
}

test('default local login stays tab-scoped and never stores password or restores in a fresh tab', async t => {
  setup(t)
  const session = await freshSession()
  session.persistLocalIdentityToken(jwt())
  assert.equal(session.getIdentityAccessToken(), jwt())
  assert.equal(localStorage.getItem(key), null)
  sessionStorage.clear()
  assert.equal((await freshSession()).getBootstrapIdentityToken(), '')
})

test('opt-in restores across tabs, is capped at JWT expiry/30 days, and logout clears every copy', async t => {
  setup(t)
  const session = await freshSession()
  session.persistLocalIdentityToken(jwt(now + 10 * day), true)
  assert.deepEqual(JSON.parse(localStorage.getItem(key)), { token: jwt(now + 10 * day), expiresAt: now + 10 * day })
  assert.equal(sessionStorage.getItem('opencli.bootstrapIdentityToken'), null)
  const restored = await freshSession()
  assert.equal(restored.getBootstrapIdentityToken(), jwt(now + 10 * day))
  restored.setRuntimeIdentityToken(restored.getBootstrapIdentityToken())
  session.clearIdentityToken()
  assert.equal(localStorage.getItem(key), null)
  assert.equal(restored.getIdentityAccessToken(), '')
  restored.persistLocalIdentityToken(jwt(now + 60 * day), true)
  assert.equal(JSON.parse(localStorage.getItem(key)).expiresAt, now + 30 * day)
})

test('expiry clears persisted and restored runtime tokens without extending on reads', async t => {
  setup(t)
  const session = await freshSession()
  const token = jwt(now + day)
  session.persistLocalIdentityToken(token, true)
  const before = localStorage.getItem(key)
  assert.equal(session.getIdentityAccessToken(), token)
  assert.equal(localStorage.getItem(key), before)
  t.mock.method(Date, 'now', () => now + day)
  assert.equal(session.getIdentityAccessToken(), '')
  assert.equal(localStorage.getItem(key), null)
  assert.equal((await freshSession()).getBootstrapIdentityToken(), '')
})

test('unchecked login and manual bootstrap remove earlier remembered login; OIDC tokens cannot opt in', async t => {
  setup(t)
  const session = await freshSession()
  session.persistLocalIdentityToken(jwt(), true)
  session.persistLocalIdentityToken(jwt())
  assert.equal(localStorage.getItem(key), null)
  session.persistLocalIdentityToken(jwt(), true)
  session.persistBootstrapIdentityToken('manual-bootstrap')
  assert.equal(localStorage.getItem(key), null)
  session.persistLocalIdentityToken(jwt(now + day, 'oidc'), true)
  assert.equal(localStorage.getItem(key), null)
  session.clearIdentityToken()
  session.setRuntimeIdentityToken('runtime-oidc')
  session.setDevelopmentSession(true)
  assert.equal(localStorage.getItem(key), null)
})

test('expired, malformed and non-local persisted records fail closed', async t => {
  setup(t)
  for (const value of ['invalid-json', '{}', JSON.stringify({ token: jwt(now - 1), expiresAt: now - 1 }), JSON.stringify({ token: jwt(now + day, 'oidc'), expiresAt: now + day })]) {
    localStorage.setItem(key, value)
    assert.equal((await freshSession()).getBootstrapIdentityToken(), '')
    assert.equal(localStorage.getItem(key), null)
  }
})

test('blocked persistent storage keeps current tab login functional', async t => {
  setup(t)
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('storage denied') } })
  const session = await freshSession()
  session.persistLocalIdentityToken(jwt(), true)
  assert.equal(session.getIdentityAccessToken(), jwt())
  session.clearIdentityToken()
  assert.equal(session.getIdentityAccessToken(), '')
})
