const BOOTSTRAP_TOKEN_KEY = 'opencli.bootstrapIdentityToken'
const DEVELOPMENT_SESSION_KEY = 'opencli.developmentSession'
export const REMEMBERED_LOCAL_TOKEN_KEY = 'opencli.rememberedLocalLogin'
const REMEMBER_LOGIN_MS = 30 * 24 * 60 * 60 * 1000

let runtimeIdentityToken = ''
let runtimeDevelopmentSession = false
let identityGeneration = 0
let runtimeRememberedLocalToken = ''

function advanceIdentityGeneration(): void {
  identityGeneration += 1
}

function safeSessionGet(key: string): string {
  try {
    return typeof sessionStorage === 'undefined' ? '' : sessionStorage.getItem(key)?.trim() ?? ''
  } catch {
    return ''
  }
}

function safeSessionSet(key: string, value: string): void {
  try {
    if (typeof sessionStorage === 'undefined') return
    if (value) sessionStorage.setItem(key, value)
    else sessionStorage.removeItem(key)
  } catch {
    // Storage can be unavailable in private browsing or hardened browsers.
  }
}

export function getIdentityAccessToken(): string {
  const bootstrapToken = getBootstrapIdentityToken()
  return runtimeIdentityToken || bootstrapToken
}

export function getIdentityGeneration(): number {
  return identityGeneration
}

export function setRuntimeIdentityToken(token: string): void {
  runtimeIdentityToken = token.trim()
  advanceIdentityGeneration()
}

export function getBootstrapIdentityToken(): string {
  const rememberedToken = readRememberedLocalToken()
  if (runtimeRememberedLocalToken && runtimeRememberedLocalToken !== rememberedToken) {
    if (runtimeIdentityToken === runtimeRememberedLocalToken) runtimeIdentityToken = ''
    if (safeSessionGet(BOOTSTRAP_TOKEN_KEY) === runtimeRememberedLocalToken) safeSessionSet(BOOTSTRAP_TOKEN_KEY, '')
    runtimeRememberedLocalToken = ''
    advanceIdentityGeneration()
  }
  const bootstrapToken = safeSessionGet(BOOTSTRAP_TOKEN_KEY)
  if (!bootstrapToken && rememberedToken) runtimeRememberedLocalToken = rememberedToken
  return bootstrapToken || rememberedToken
}

export function persistBootstrapIdentityToken(token: string): void {
  clearRememberedLocalToken()
  const trimmed = token.trim()
  runtimeIdentityToken = trimmed
  safeSessionSet(BOOTSTRAP_TOKEN_KEY, trimmed)
  advanceIdentityGeneration()
}

export function clearIdentityToken(preserveRememberedLocalLogin = false): void {
  if (preserveRememberedLocalLogin) runtimeRememberedLocalToken = ''
  else clearRememberedLocalToken()
  runtimeIdentityToken = ''
  safeSessionSet(BOOTSTRAP_TOKEN_KEY, '')
  advanceIdentityGeneration()
}

export function isRememberedLocalIdentityToken(token: string): boolean {
  return Boolean(token) && runtimeRememberedLocalToken === token
}

function localTokenExpiry(token: string): number | null {
  try {
    const parts = token.split('.')
    if (parts.length !== 3 || !parts.every(Boolean)) return null
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
    // This is only an expiry/storage filter; the API still verifies the signature.
    return payload.auth_method === 'local' && typeof payload.exp === 'number' && Number.isFinite(payload.exp)
      ? payload.exp * 1000 : null
  } catch {
    return null
  }
}

function clearRememberedLocalToken(): void {
  runtimeRememberedLocalToken = ''
  try { localStorage.removeItem(REMEMBERED_LOCAL_TOKEN_KEY) } catch { /* Storage may be disabled. */ }
}

function readRememberedLocalToken(): string {
  try {
    const raw = localStorage.getItem(REMEMBERED_LOCAL_TOKEN_KEY)
    if (!raw) return ''
    const record = JSON.parse(raw)
    const token = typeof record?.token === 'string' ? record.token : ''
    const tokenExpiry = localTokenExpiry(token)
    if (tokenExpiry === null || typeof record.expiresAt !== 'number' || !Number.isFinite(record.expiresAt)
      || record.expiresAt > tokenExpiry || record.expiresAt > Date.now() + REMEMBER_LOGIN_MS
      || record.expiresAt <= Date.now()) {
      localStorage.removeItem(REMEMBERED_LOCAL_TOKEN_KEY)
      if (runtimeIdentityToken === token) runtimeIdentityToken = ''
      if (safeSessionGet(BOOTSTRAP_TOKEN_KEY) === token) safeSessionSet(BOOTSTRAP_TOKEN_KEY, '')
      return ''
    }
    return token
  } catch {
    try { localStorage.removeItem(REMEMBERED_LOCAL_TOKEN_KEY) } catch { /* Storage may be disabled. */ }
    return ''
  }
}

export function persistLocalIdentityToken(token: string, rememberLogin = false): void {
  persistBootstrapIdentityToken(token)
  const tokenExpiry = localTokenExpiry(token.trim())
  if (!rememberLogin || tokenExpiry === null || tokenExpiry <= Date.now()) return
  try {
    localStorage.setItem(REMEMBERED_LOCAL_TOKEN_KEY, JSON.stringify({
      token: token.trim(), expiresAt: Math.min(tokenExpiry, Date.now() + REMEMBER_LOGIN_MS),
    }))
    safeSessionSet(BOOTSTRAP_TOKEN_KEY, '')
    runtimeRememberedLocalToken = token.trim()
  } catch {
    // Login still works for this tab if persistent storage is unavailable.
  }
}

export function hasDevelopmentSession(): boolean {
  return runtimeDevelopmentSession || safeSessionGet(DEVELOPMENT_SESSION_KEY) === '1'
}

export function setDevelopmentSession(enabled: boolean): void {
  runtimeDevelopmentSession = enabled
  safeSessionSet(DEVELOPMENT_SESSION_KEY, enabled ? '1' : '')
}

export function isDevelopmentLoginAllowed(): boolean {
  return (
    process.env.NODE_ENV !== 'production' &&
    process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV === 'true'
  )
}
