import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import path from 'node:path'
import { test } from 'node:test'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const read = (relativePath) => readFile(path.join(frontendRoot, relativePath), 'utf8')

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (
      (specifier.startsWith('@/') || specifier.startsWith('.')) &&
      context.parentURL?.startsWith('file:')
    ) {
      const candidate = specifier.startsWith('@/')
        ? path.resolve(frontendRoot, specifier.slice(2))
        : path.resolve(path.dirname(fileURLToPath(context.parentURL)), specifier)
      for (const resolvedPath of [candidate, `${candidate}.ts`]) {
        if (existsSync(resolvedPath)) {
          return { url: pathToFileURL(resolvedPath).href, shortCircuit: true }
        }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.ts')) {
      return {
        format: 'module',
        source: stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8'), { mode: 'strip' }),
        shortCircuit: true,
      }
    }
    return nextLoad(url, context)
  },
})

const {
  FLEET_AUTH_ERROR_CODE,
  RecoverySupersededError,
  classifyIdentityRecoveryError,
  createIdentityRecoveryCoordinator,
  getApiErrorStatus,
  hasFleetTransportCredential,
  isAuthRejection,
  isFleetAuthError,
  retryIdentityValidation,
  waitForApiLiveness,
} = await import('../lib/api/recovery.ts')
const {
  classifyStoredOidcUser,
  isOidcIdentity,
  shouldClearIdentityForOidcUnload,
  shouldAcceptOidcRenewal,
} = await import('../lib/auth/oidc.ts')
const { AUTH_REQUIRED_EVENT, shouldInvalidateIdentityForGeneration } = await import(
  '../lib/api/auth-events.ts'
)
const { apiClient } = await import('../lib/api/client.ts')
const { setApiAuthToken } = await import('../lib/api/auth-token.ts')
const {
  clearIdentityToken,
  getIdentityGeneration,
  persistBootstrapIdentityToken,
  setRuntimeIdentityToken,
} = await import('../lib/auth/session.ts')

test('only explicit identity rejection statuses invalidate a retained session', () => {
  assert.equal(getApiErrorStatus(new Error('network unavailable')), undefined)
  assert.equal(getApiErrorStatus({ status: 503 }), 503)
  assert.equal(isAuthRejection({ status: undefined }), false)
  assert.equal(isAuthRejection({ status: 500 }), false)
  assert.equal(isAuthRejection({ status: 401 }), true)
  const fleet401 = {
    status: 401,
    code: FLEET_AUTH_ERROR_CODE,
    fleetTransportCredentialAttached: true,
  }
  assert.equal(isFleetAuthError(fleet401), true)
  assert.equal(isAuthRejection(fleet401), false)
  const expiredLocalSession401 = {
    status: 401,
    code: FLEET_AUTH_ERROR_CODE,
    fleetTransportCredentialAttached: false,
  }
  assert.equal(isFleetAuthError(expiredLocalSession401), false)
  assert.equal(isAuthRejection(expiredLocalSession401), true)
  const fleetCodeWithoutRequestMetadata = { status: 401, code: FLEET_AUTH_ERROR_CODE }
  assert.equal(isFleetAuthError(fleetCodeWithoutRequestMetadata), false)
  assert.equal(isAuthRejection(fleetCodeWithoutRequestMetadata), true)
  assert.equal(classifyIdentityRecoveryError(fleetCodeWithoutRequestMetadata), 'auth-rejection')
  assert.equal(
    isFleetAuthError({
      status: 401,
      code: FLEET_AUTH_ERROR_CODE,
      fleetTransportCredentialAttached: 'true',
    }),
    false,
  )
  assert.equal(isAuthRejection({ status: 403 }), true)
  assert.equal(classifyIdentityRecoveryError(new Error('network unavailable')), 'transient')
  assert.equal(classifyIdentityRecoveryError({ status: 503 }), 'transient')
  assert.equal(classifyIdentityRecoveryError({ status: 401 }), 'auth-rejection')
  assert.equal(classifyIdentityRecoveryError(fleet401), 'incompatible')
  assert.equal(classifyIdentityRecoveryError(expiredLocalSession401), 'auth-rejection')
  assert.equal(classifyIdentityRecoveryError({ status: 403 }), 'auth-rejection')
  assert.equal(classifyIdentityRecoveryError({ status: 408 }), 'transient')
  assert.equal(classifyIdentityRecoveryError({ status: 429 }), 'transient')
  assert.equal(classifyIdentityRecoveryError({ status: 400 }), 'incompatible')
  assert.equal(classifyIdentityRecoveryError({ status: 409 }), 'incompatible')
})

test('fleet transport evidence requires a non-empty credential value', () => {
  assert.equal(hasFleetTransportCredential(undefined), false)
  assert.equal(hasFleetTransportCredential(''), false)
  assert.equal(hasFleetTransportCredential('   '), false)
  assert.equal(hasFleetTransportCredential(' fleet-token '), true)
})

test('Axios only retains identity for a fleet-coded 401 from a dual-header request', async () => {
  const previousWindow = globalThis.window
  const previousLocalStorage = globalThis.localStorage
  const previousCustomEvent = globalThis.CustomEvent
  const values = new Map()
  const localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
  const windowTarget = new EventTarget()
  class TestCustomEvent extends Event {
    constructor(type, init = {}) {
      super(type)
      this.detail = init.detail
    }
  }
  const authRequiredGenerations = []
  windowTarget.addEventListener(AUTH_REQUIRED_EVENT, (event) => {
    authRequiredGenerations.push(event.detail.identityGeneration)
  })
  globalThis.window = windowTarget
  globalThis.localStorage = localStorage
  globalThis.CustomEvent = TestCustomEvent

  const fleetRejectingAdapter = async (config) => {
    throw Object.assign(new Error('Invalid or missing API token'), {
      config,
      isAxiosError: true,
      response: {
        config,
        data: {
          code: FLEET_AUTH_ERROR_CODE,
          error: 'Invalid or missing API token',
          success: false,
        },
        headers: {},
        status: 401,
        statusText: 'Unauthorized',
      },
    })
  }

  try {
    setRuntimeIdentityToken('retained-identity')
    setApiAuthToken(' stale-fleet-token ')
    await assert.rejects(
      apiClient.get('/system/config', { adapter: fleetRejectingAdapter }),
      (error) => {
        assert.equal(error.code, FLEET_AUTH_ERROR_CODE)
        assert.equal(error.fleetTransportCredentialAttached, true)
        return true
      },
    )
    assert.deepEqual(authRequiredGenerations, [])

    setApiAuthToken('')
    const localRequestGeneration = getIdentityGeneration()
    await assert.rejects(
      apiClient.get('/system/config', { adapter: fleetRejectingAdapter }),
      (error) => {
        assert.equal(error.code, FLEET_AUTH_ERROR_CODE)
        assert.equal(error.fleetTransportCredentialAttached, false)
        return true
      },
    )
    assert.deepEqual(authRequiredGenerations, [localRequestGeneration])
  } finally {
    setApiAuthToken('')
    clearIdentityToken()
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
    if (previousLocalStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = previousLocalStorage
    if (previousCustomEvent === undefined) delete globalThis.CustomEvent
    else globalThis.CustomEvent = previousCustomEvent
  }
})

test('identity recovery epochs retain transient work but prevent stale in-flight commits', async () => {
  const coordinator = createIdentityRecoveryCoordinator()
  const epoch = coordinator.beginEpoch()
  let releaseRecovery
  let committed = false
  const heldRecovery = new Promise((resolve) => {
    releaseRecovery = resolve
  })
  const recovery = coordinator.run(
    'old-token',
    async (operationEpoch) => {
      await heldRecovery
      if (!coordinator.isCurrent(operationEpoch)) throw new RecoverySupersededError()
      committed = true
    },
    epoch,
  )

  await Promise.resolve()
  assert.equal(coordinator.isCurrent(epoch), true)

  coordinator.invalidate()
  assert.equal(coordinator.isCurrent(epoch), false)
  releaseRecovery()
  await assert.rejects(recovery, RecoverySupersededError)
  assert.equal(committed, false)
})

test('same-token recovery coalesces within an epoch and propagates interactive rejection', async () => {
  const coordinator = createIdentityRecoveryCoordinator()
  const epoch = coordinator.beginEpoch()
  const rejection = Object.assign(new Error('identity rejected'), { status: 403 })
  let starts = 0
  let rejectRecovery
  const heldRecovery = new Promise((_, reject) => {
    rejectRecovery = reject
  })
  const recover = async () => {
    starts += 1
    return heldRecovery
  }

  const eventRecovery = coordinator.run('same-token', recover, epoch)
  const interactiveRecovery = coordinator.run('same-token', recover, epoch)
  assert.strictEqual(interactiveRecovery, eventRecovery)
  await Promise.resolve()
  assert.equal(starts, 1)

  rejectRecovery(rejection)
  await assert.rejects(eventRecovery, rejection)
  await assert.rejects(interactiveRecovery, rejection)
})

test('liveness cancellation after a probe prevents every later probe', async () => {
  const coordinator = createIdentityRecoveryCoordinator()
  const epoch = coordinator.beginEpoch()
  let probes = 0
  const result = await waitForApiLiveness(
    async () => {
      probes += 1
      coordinator.invalidate()
      return false
    },
    {
      initialDelayMs: 0,
      intervalMs: 0,
      maxAttempts: 5,
      delay: async () => {},
      isCancelled: () => !coordinator.isCurrent(epoch),
    },
  )

  assert.equal(result, 'cancelled')
  assert.equal(probes, 1)
})

test('a new recovery epoch waits for a held old probe instead of overlapping', async () => {
  const coordinator = createIdentityRecoveryCoordinator()
  let releaseOldProbe
  let reportOldProbeStarted
  const oldProbeStarted = new Promise((resolve) => {
    reportOldProbeStarted = resolve
  })
  const heldOldProbe = new Promise((resolve) => {
    releaseOldProbe = resolve
  })
  let active = 0
  let maximumActive = 0
  let newRecoveryStarted = false

  const oldEpoch = coordinator.beginEpoch()
  const oldRecovery = coordinator.run(
    'old-token',
    async (operationEpoch) => {
      active += 1
      maximumActive = Math.max(maximumActive, active)
      reportOldProbeStarted()
      await heldOldProbe
      active -= 1
      if (!coordinator.isCurrent(operationEpoch)) throw new RecoverySupersededError()
    },
    oldEpoch,
  )
  await oldProbeStarted

  const newEpoch = coordinator.beginEpoch()
  const newRecovery = coordinator.run(
    'new-token',
    async () => {
      newRecoveryStarted = true
      active += 1
      maximumActive = Math.max(maximumActive, active)
      active -= 1
      return 'new-session'
    },
    newEpoch,
  )
  await Promise.resolve()
  assert.equal(newRecoveryStarted, false)
  assert.equal(active, 1)

  releaseOldProbe()
  await assert.rejects(oldRecovery, RecoverySupersededError)
  assert.equal(await newRecovery, 'new-session')
  assert.equal(maximumActive, 1)
})

test('only an OIDC identity requests identity-provider signout', () => {
  assert.equal(isOidcIdentity('oidc'), true)
  assert.equal(isOidcIdentity('bootstrap'), false)
  assert.equal(isOidcIdentity('local'), false)
  assert.equal(isOidcIdentity(undefined), false)
})

test('invalid stored OIDC users fall through instead of trapping session restore', () => {
  assert.equal(classifyStoredOidcUser(null), 'absent')
  assert.equal(classifyStoredOidcUser({ expired: true, id_token: 'old-token' }), 'expired')
  assert.equal(classifyStoredOidcUser({ expired: false }), 'invalid')
  assert.equal(classifyStoredOidcUser({ expired: false, id_token: '   ' }), 'invalid')
  assert.equal(classifyStoredOidcUser({ expired: false, id_token: 'valid-token' }), 'usable')
})

test('OIDC unload only clears an actively owned session outside local removal', () => {
  assert.equal(shouldClearIdentityForOidcUnload(true, false), true)
  assert.equal(shouldClearIdentityForOidcUnload(true, true), false)
  assert.equal(shouldClearIdentityForOidcUnload(false, false), false)
  assert.equal(shouldClearIdentityForOidcUnload(false, true), false)
  assert.equal(shouldClearIdentityForOidcUnload(true, false, 7, 8), false)
  assert.equal(shouldClearIdentityForOidcUnload(true, false, 7, 7, true), false)
})

test('OIDC renewal ownership rejects late events from an older generation', () => {
  assert.equal(shouldAcceptOidcRenewal(true, false, 7, 7), true)
  assert.equal(shouldAcceptOidcRenewal(true, false, 7, 8), false)
  assert.equal(shouldAcceptOidcRenewal(true, true, 7, 7), false)
  assert.equal(shouldAcceptOidcRenewal(false, false, 7, 7), false)
})

test('transient identity failures retry validation after liveness returns', async () => {
  let validations = 0
  let livenessChecks = 0
  const result = await retryIdentityValidation(
    async () => {
      validations += 1
      if (validations < 3) throw new Error('API restarting')
    },
    async () => true,
    async () => {
      livenessChecks += 1
      return 'recovered'
    },
  )

  assert.equal(result, 'completed')
  assert.equal(validations, 3)
  assert.equal(livenessChecks, 2)
})

test('stale 401 generations cannot clear a newer identity', () => {
  const startingGeneration = getIdentityGeneration()
  setRuntimeIdentityToken('older-identity')
  const staleGeneration = getIdentityGeneration()
  assert.equal(staleGeneration, startingGeneration + 1)

  persistBootstrapIdentityToken('newer-identity')
  const currentGeneration = getIdentityGeneration()
  assert.equal(currentGeneration, staleGeneration + 1)

  let clears = 0
  const handleUnauthorized = (requestGeneration) => {
    if (!shouldInvalidateIdentityForGeneration(requestGeneration, getIdentityGeneration())) return
    clearIdentityToken()
    clears += 1
  }

  handleUnauthorized(staleGeneration)
  assert.equal(clears, 0)
  assert.equal(getIdentityGeneration(), currentGeneration)

  handleUnauthorized(currentGeneration)
  assert.equal(clears, 1)
  assert.equal(getIdentityGeneration(), currentGeneration + 1)
})

test('liveness recovery is sequential, bounded, and stops after the API returns', async () => {
  let calls = 0
  let active = 0
  let maximumActive = 0
  const delays = []
  const result = await waitForApiLiveness(
    async () => {
      calls += 1
      active += 1
      maximumActive = Math.max(maximumActive, active)
      await Promise.resolve()
      active -= 1
      if (calls < 3) throw new Error('offline')
      return true
    },
    {
      initialDelayMs: 1_250,
      intervalMs: 10,
      maxAttempts: 5,
      delay: async (milliseconds) => delays.push(milliseconds),
    },
  )

  assert.equal(result, 'recovered')
  assert.equal(calls, 3)
  assert.equal(maximumActive, 1)
  assert.deepEqual(delays, [1_250, 10, 10])
})

test('liveness recovery exposes a retryable timeout after the configured bound', async () => {
  let calls = 0
  const result = await waitForApiLiveness(
    async () => {
      calls += 1
      return false
    },
    { initialDelayMs: 0, intervalMs: 0, maxAttempts: 4, delay: async () => {} },
  )

  assert.equal(result, 'timeout')
  assert.equal(calls, 4)
})

test('restart recovery ignores old-process health until an outage is observed', async () => {
  const observations = [true, true, false, true]
  let calls = 0
  const result = await waitForApiLiveness(
    async () => {
      const observation = observations[calls]
      calls += 1
      return observation
    },
    {
      initialDelayMs: 0,
      intervalMs: 0,
      maxAttempts: observations.length,
      requireUnavailableBeforeRecovery: true,
      delay: async () => {},
    },
  )

  assert.equal(result, 'recovered')
  assert.equal(calls, 4)
})

test('restart recovery times out when only the old process stays healthy', async () => {
  let calls = 0
  const result = await waitForApiLiveness(
    async () => {
      calls += 1
      return true
    },
    {
      initialDelayMs: 0,
      intervalMs: 0,
      maxAttempts: 3,
      requireUnavailableBeforeRecovery: true,
      delay: async () => {},
    },
  )

  assert.equal(result, 'timeout')
  assert.equal(calls, 3)
})

test('session restore retains credentials through network and 5xx recovery', async () => {
  const [provider, gate, types, endpoints, client, oidc, authEvents, session] = await Promise.all([
    read('components/auth/auth-provider.tsx'),
    read('components/auth/auth-gate.tsx'),
    read('lib/auth/types.ts'),
    read('lib/api/endpoints.ts'),
    read('lib/api/client.ts'),
    read('lib/auth/oidc.ts'),
    read('lib/api/auth-events.ts'),
    read('lib/auth/session.ts'),
  ])

  assert.match(types, /'loading' \| 'recovering' \| 'authenticated' \| 'anonymous'/)
  assert.match(provider, /classifyIdentityRecoveryError\(error\)/)
  assert.match(provider, /recoveryCoordinator\.invalidate\(\)/)
  assert.match(provider, /requireCurrentRecovery\(operationEpoch\)/)
  assert.match(provider, /setStatus\('recovering'\)/)
  assert.match(provider, /waitForApiLiveness/)
  assert.match(provider, /retryIdentityValidation/)
  assert.match(provider, /isCancelled: \(\) => !isRecoveryCurrent\(operationEpoch\)/)
  assert.match(provider, /await acceptIdentityToken\(token, owner, operationEpoch\)/)
  assert.match(provider, /const removeOidcUser = useCallback/)
  assert.match(provider, /\.then\(\(\) => manager\.removeUser\(\)\)/)
  assert.match(provider, /oidcRemovalInProgressRef\.current = true/)
  assert.match(provider, /shouldClearIdentityForOidcUnload\(/)
  assert.match(provider, /addAccessTokenExpiring\(onAccessTokenExpiring\)/)
  assert.match(provider, /shouldAcceptOidcRenewal\(/)
  assert.match(provider, /storedOidcState === 'expired' \|\| storedOidcState === 'invalid'/)
  assert.match(provider, /removeOidcUser\(\)[\s\S]*?oidcUser = null/)
  assert.match(
    provider,
    /loginWithPassword\(username, password\)[\s\S]*?claimOidcOwnership\(false\)[\s\S]*?await removeOidcUser\(\)[\s\S]*?requireCurrentRecovery\(epoch\)[\s\S]*?persistLocalIdentityToken\(result\.access_token, rememberLogin\)/,
  )
  assert.match(provider, /if \(!oidcRemoved\) throw new Error\('无法清理旧 OIDC 会话/)
  assert.doesNotMatch(provider, /if \(!oidcUser\.id_token\) throw/)
  assert.match(provider, /oidcOwnershipRef\.current/)
  assert.doesNotMatch(provider, /addUserLoaded|removeUserLoaded|const onUserLoaded/)
  assert.match(provider, /isOidcIdentity\(identity\?\.auth_method\)/)
  assert.match(provider, /signoutRedirect\(\{ id_token_hint: oidcUser\.id_token \}\)/)
  assert.match(provider, /const localSignOutGeneration = oidcOwnershipGenerationRef\.current/)
  assert.match(provider, /oidcOwnershipGenerationRef\.current !== localSignOutGeneration/)
  assert.match(provider, /oidcRemovalInProgressRef\.current = true[\s\S]*?signoutRedirect\([\s\S]*?finally[\s\S]*?oidcRemovalInProgressRef\.current = false/)
  assert.match(provider, /shouldInvalidateIdentityForGeneration\(eventGeneration, getIdentityGeneration\(\)\)/)
  assert.doesNotMatch(provider, /setRuntimeIdentityToken\(''\)/)
  assert.match(gate, /status === 'recovering'/)
  assert.match(gate, /登录状态已保留/)
  assert.match(gate, /系统已停止自动重试/)
  assert.match(gate, /void retrySession\(\)/)
  assert.match(endpoints, /rootClient\s*\.get<ApiHealthSnapshot>\("\/health", \{ timeout, signal \}\)/)
  assert.match(client, /opencliIdentityGeneration = identityGeneration/)
  assert.match(client, /opencliFleetTransportCredentialAttached =/)
  assert.match(client, /config\.headers\.get\?\.\('X-API-Token'\)/)
  assert.match(client, /hasFleetTransportCredential\(fleetTransportCredential\)/)
  assert.match(client, /notifyAuthRequired\(requestGeneration\)/)
  assert.match(client, /FLEET_AUTH_ERROR_CODE/)
  assert.match(
    client,
    /responseCode === FLEET_AUTH_ERROR_CODE && fleetTransportCredentialAttached/,
  )
  assert.match(client, /!retainIdentityForFleetFailure/)
  assert.match(client, /typeof responseCode === 'string'\) normalized\.code = responseCode/)
  assert.match(
    client,
    /normalized\.fleetTransportCredentialAttached = fleetTransportCredentialAttached/,
  )
  assert.match(provider, /isFleetAuthError\(error\)/)
  assert.match(provider, /传输凭据不一致。登录状态已保留，请检查部署配置后重新检查。/)
  assert.match(provider, /result === 'stopped' && !incompatibleFailure/)
  assert.doesNotMatch(client, /status === 403\) notifyAuthRequired/)
  assert.match(authEvents, /detail: \{ identityGeneration \}/)
  assert.match(session, /export function getIdentityGeneration\(\): number/)
  assert.match(oidc, /automaticSilentRenew: false/)
  assert.doesNotMatch(oidc, /automaticSilentRenew: true/)
})

test('restart UI waits past the backend delay, refreshes data, and exposes timeout retry', async () => {
  const restartCard = await read('components/system/restart-api-card.tsx')

  assert.match(restartCard, /type RestartPhase = 'idle' \| 'requested' \| 'waiting' \| 'recovered' \| 'timeout'/)
  assert.match(restartCard, /const RESTART_REQUESTED_DISPLAY_MS = 250/)
  assert.match(restartCard, /setPhase\('requested'\)/)
  assert.match(restartCard, /setPhase\('waiting'\)/)
  assert.match(restartCard, /setPhase\('recovered'\)/)
  assert.match(restartCard, /setPhase\('timeout'\)/)
  assert.match(restartCard, /queryClient\.invalidateQueries\(\{ refetchType: 'active' \}\)/)
  assert.match(restartCard, /orchestrateApiRestartRecovery\(/)
  assert.match(restartCard, /probe: \(signal\) => getHealth\(undefined, signal\)/)
  assert.match(restartCard, /onClick=\{\(\) => void checkRecovery\(false\)\}/)
  assert.match(restartCard, /不会重建容器，也不会应用宿主机/)
  assert.doesNotMatch(restartCard, /refreshing the page|\u5237\u65b0\u9875\u9762前此提示/)
})
