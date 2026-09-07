const BOOTSTRAP_TOKEN_KEY = 'opencli.bootstrapIdentityToken'
const DEVELOPMENT_SESSION_KEY = 'opencli.developmentSession'

let runtimeIdentityToken = ''
let runtimeDevelopmentSession = false
let identityGeneration = 0

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
  return runtimeIdentityToken || safeSessionGet(BOOTSTRAP_TOKEN_KEY)
}

export function getIdentityGeneration(): number {
  return identityGeneration
}

export function setRuntimeIdentityToken(token: string): void {
  runtimeIdentityToken = token.trim()
  advanceIdentityGeneration()
}

export function getBootstrapIdentityToken(): string {
  return safeSessionGet(BOOTSTRAP_TOKEN_KEY)
}

export function persistBootstrapIdentityToken(token: string): void {
  const trimmed = token.trim()
  runtimeIdentityToken = trimmed
  safeSessionSet(BOOTSTRAP_TOKEN_KEY, trimmed)
  advanceIdentityGeneration()
}

export function clearIdentityToken(): void {
  runtimeIdentityToken = ''
  safeSessionSet(BOOTSTRAP_TOKEN_KEY, '')
  advanceIdentityGeneration()
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
