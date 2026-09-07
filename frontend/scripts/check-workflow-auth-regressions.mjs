import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

registerHooks({
  resolve(specifier, context, nextResolve) {
    const candidates = []
    if (specifier.startsWith('@/')) {
      candidates.push(path.join(frontendRoot, specifier.slice(2)))
    } else if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      candidates.push(path.resolve(path.dirname(fileURLToPath(context.parentURL)), specifier))
    }
    for (const candidate of candidates) {
      for (const resolvedPath of [candidate, `${candidate}.ts`, `${candidate}.tsx`]) {
        try {
          readFileSync(resolvedPath)
          return { url: pathToFileURL(resolvedPath).href, shortCircuit: true }
        } catch {
          // Try the next extension candidate.
        }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.ts') || url.endsWith('.tsx')) {
      const source = stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8'), {
        mode: 'strip',
        sourceUrl: url,
      })
      return { format: 'module', source, shortCircuit: true }
    }
    return nextLoad(url, context)
  },
})

const importTypeScript = (relativePath) => import(
  pathToFileURL(path.join(frontendRoot, relativePath)).href
)

const identityToken = 'identity-token'
const fleetToken = 'fleet-token'
const project = { nodes: [], edges: [] }

function installStorage() {
  const stores = {
    local: new Map(),
    session: new Map(),
  }
  globalThis.localStorage = {
    getItem: (key) => stores.local.get(key) ?? null,
    setItem: (key, value) => stores.local.set(key, String(value)),
    removeItem: (key) => stores.local.delete(key),
  }
  globalThis.sessionStorage = {
    getItem: (key) => stores.session.get(key) ?? null,
    setItem: (key, value) => stores.session.set(key, String(value)),
    removeItem: (key) => stores.session.delete(key),
  }
  return stores
}

function installFetchSpy({ responseFactory } = {}) {
  const requests = []
  globalThis.fetch = async (input, init = {}) => {
    requests.push(new Request(new URL(String(input), 'http://workflow.test').href, init))
    const customResponse = responseFactory?.(input, init)
    if (customResponse) return customResponse
    if (String(input).endsWith('/events/stream')) {
      return {
        ok: true,
        status: 200,
        text: async () => '',
      }
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ data: {} }),
    }
  }
  return requests
}

function assertIdentityAndFleet(request, hasFleet) {
  const authorization = request.headers.get('authorization')
  const fleet = request.headers.get('x-api-token')
  assert.equal(authorization === `Bearer ${identityToken}`, true, 'identity Authorization header mismatch')
  assert.equal(hasFleet ? fleet === fleetToken : fleet === null, true, 'fleet header presence mismatch')
}

async function exerciseWorkflowRequests(workflowRuns) {
  await workflowRuns.startWorkflowRun(project)
  await workflowRuns.fetchWorkflowRunProjection('run')
  await workflowRuns.fetchWorkflowRunCheckpoint('run')
  await workflowRuns.queryWorkflowRunTrace('run')
  await workflowRuns.resumeGaojixingWorkflowRun('run')
  await workflowRuns.fetchWorkflowRunEvents('run')
  await workflowRuns.continueWorkflowRunWithSourceOutputs('run', {})
  await workflowRuns.fetchWorkflowResearchLedger('run')
  await workflowRuns.continueWorkflowResearch('run', {
    expectedRevisionId: 'revision',
    proposalId: 'proposal',
    idempotencyKey: 'request',
    sourceOutputs: {},
  })
  await workflowRuns.replayWorkflowRunEventStream('run')
  await workflowRuns.fetchWorkflowEvidenceBatches('run')
  await workflowRuns.fetchWorkflowEvidenceBatchDetail('run', 'batch')
  await workflowRuns.fetchWorkflowEvidenceBatchProjection('run')
}

test('workflow requests separate identity Authorization from fleet transport credentials', async () => {
  const stores = installStorage()
  process.env.NEXT_PUBLIC_API_AUTH_TOKEN = ''
  const { setRuntimeIdentityToken } = await importTypeScript('lib/auth/session.ts')
  const { API_AUTH_TOKEN_KEY } = await importTypeScript('lib/api/auth-token.ts')
  const workflowRuns = await importTypeScript('lib/workflow/backend-runs.ts')

  setRuntimeIdentityToken(identityToken)
  stores.local.set(API_AUTH_TOKEN_KEY, fleetToken)
  const requestsWithFleet = installFetchSpy()
  await exerciseWorkflowRequests(workflowRuns)
  assert.equal(requestsWithFleet.length, 13, 'unexpected workflow request count')
  for (const request of requestsWithFleet) assertIdentityAndFleet(request, true)

  const requestsWithOverride = installFetchSpy()
  await workflowRuns.fetchWorkflowRunProjection('run', { authorization: 'Bearer explicit-override' })
  const overrideRequest = requestsWithOverride[0]
  assert.equal(overrideRequest.headers.get('authorization') === 'Bearer explicit-override', true, 'explicit Authorization override was not preserved')
  assert.equal(overrideRequest.headers.get('x-api-token') === fleetToken, true, 'fleet transport credential was not preserved with override')

  stores.local.delete(API_AUTH_TOKEN_KEY)
  const requestsWithoutFleet = installFetchSpy()
  await workflowRuns.startWorkflowRun(project)
  await workflowRuns.queryWorkflowRunTrace('run')
  await workflowRuns.replayWorkflowRunEventStream('run')
  for (const request of requestsWithoutFleet) assertIdentityAndFleet(request, false)
})

test('插件目录请求在三种登录状态下发送共享鉴权头', async () => {
  const stores = installStorage()
  const originalNodeEnv = process.env.NODE_ENV
  const originalBuildToken = process.env.NEXT_PUBLIC_API_AUTH_TOKEN
  const originalDevelopmentFlag = process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV
  process.env.NODE_ENV = 'production'
  process.env.NEXT_PUBLIC_API_AUTH_TOKEN = ''
  process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV = ''

  const { clearIdentityToken, setRuntimeIdentityToken } = await importTypeScript('lib/auth/session.ts')
  const { API_AUTH_TOKEN_KEY } = await importTypeScript('lib/api/auth-token.ts')
  const {
    fetchPluginInstallations,
    importDifyPluginPackage,
    updatePluginInstallation,
  } = await importTypeScript('lib/plugins/backend-plugin-catalog.ts')

  const exerciseCatalogRequests = async () => {
    const requests = installFetchSpy({
      responseFactory: (input) => String(input).includes('/plugins')
        ? {
            ok: true,
            status: 200,
            json: async () => ({ success: true, data: {} }),
          }
        : null,
    })
    await fetchPluginInstallations('team/one')
    await importDifyPluginPackage('team/one', new Blob(['plugin'], { type: 'application/zip' }))
    await updatePluginInstallation('team/one', 'installation-1', { enabled: true })
    return requests
  }

  const assertCatalogRequests = (requests, expectedAuthorization, expectedFleet) => {
    assert.equal(requests.length, 3, 'unexpected plugin catalog request count')
    assert.deepEqual(
      requests.map((request) => request.method),
      ['GET', 'POST', 'PATCH'],
      'plugin catalog operation methods changed',
    )
    for (const request of requests) {
      assert.equal(
        request.headers.get('authorization'),
        expectedAuthorization,
        'plugin catalog identity Authorization header mismatch',
      )
      assert.equal(
        request.headers.get('x-api-token'),
        expectedFleet,
        'plugin catalog fleet transport credential mismatch',
      )
    }
    assert.equal(
      requests[0].url,
      'http://workflow.test/api/v1/workspaces/team%2Fone/plugins',
      'plugin catalog workspace path changed',
    )
    assert.match(
      requests[1].headers.get('content-type') ?? '',
      /^multipart\/form-data; boundary=/,
      'Dify import must preserve FormData content type',
    )
    assert.equal(
      requests[2].headers.get('content-type'),
      'application/json',
      'plugin installation update content type changed',
    )
  }

  try {
    setRuntimeIdentityToken(identityToken)
    const identityOnlyRequests = await exerciseCatalogRequests()
    assertCatalogRequests(identityOnlyRequests, `Bearer ${identityToken}`, null)

    stores.local.set(API_AUTH_TOKEN_KEY, fleetToken)
    const identityAndFleetRequests = await exerciseCatalogRequests()
    assertCatalogRequests(identityAndFleetRequests, `Bearer ${identityToken}`, fleetToken)

    stores.local.delete(API_AUTH_TOKEN_KEY)
    clearIdentityToken()
    const unauthenticatedRequests = await exerciseCatalogRequests()
    assertCatalogRequests(unauthenticatedRequests, null, null)
  } finally {
    clearIdentityToken()
    stores.local.clear()
    stores.session.clear()
    if (originalNodeEnv === undefined) delete process.env.NODE_ENV
    else process.env.NODE_ENV = originalNodeEnv
    if (originalBuildToken === undefined) delete process.env.NEXT_PUBLIC_API_AUTH_TOKEN
    else process.env.NEXT_PUBLIC_API_AUTH_TOKEN = originalBuildToken
    if (originalDevelopmentFlag === undefined) delete process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV
    else process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV = originalDevelopmentFlag
  }
})

test('插件目录不可用时保留后端提供的错误信息', async () => {
  const stores = installStorage()
  const originalBuildToken = process.env.NEXT_PUBLIC_API_AUTH_TOKEN
  process.env.NEXT_PUBLIC_API_AUTH_TOKEN = ''
  const { clearIdentityToken } = await importTypeScript('lib/auth/session.ts')
  const { fetchPluginInstallations } = await importTypeScript('lib/plugins/backend-plugin-catalog.ts')

  try {
    clearIdentityToken()
    const requests = installFetchSpy({
      responseFactory: () => ({
        ok: false,
        status: 503,
        json: async () => ({
          success: false,
          detail: { code: 'PLUGIN_CATALOG_UNAVAILABLE', message: '插件目录暂不可用' },
        }),
      }),
    })
    await assert.rejects(
      () => fetchPluginInstallations('team/one'),
      (error) => {
        assert.equal(error.message, '插件目录暂不可用')
        return true
      },
    )
    assert.equal(requests.length, 1)
  } finally {
    clearIdentityToken()
    stores.local.clear()
    stores.session.clear()
    if (originalBuildToken === undefined) delete process.env.NEXT_PUBLIC_API_AUTH_TOKEN
    else process.env.NEXT_PUBLIC_API_AUTH_TOKEN = originalBuildToken
  }
})

test('development bypass requires an exact non-production flag', async () => {
  const stores = installStorage()
  const { clearIdentityToken, isDevelopmentLoginAllowed, setDevelopmentSession, setRuntimeIdentityToken } =
    await importTypeScript('lib/auth/session.ts')
  const { getApiAuthHeaders } = await importTypeScript('lib/api/auth-headers.ts')
  const originalNodeEnv = process.env.NODE_ENV
  const originalFlag = process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV
  const flagValues = [undefined, 'false', 'true', '1', 'TRUE', 'yes']

  try {
    for (const nodeEnv of ['development', 'test', 'production']) {
      process.env.NODE_ENV = nodeEnv
      for (const flag of flagValues) {
        if (flag === undefined) delete process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV
        else process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV = flag
        assert.equal(
          isDevelopmentLoginAllowed(),
          nodeEnv !== 'production' && flag === 'true',
          `${nodeEnv} with ${flag ?? 'absent'} flag`,
        )
      }
    }

    process.env.NODE_ENV = 'development'
    process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV = 'true'
    setRuntimeIdentityToken(identityToken)
    setDevelopmentSession(true)
    const developmentHeaders = getApiAuthHeaders()
    assert.equal(developmentHeaders.Authorization, `Bearer ${identityToken}`)
    assert.equal(developmentHeaders['X-OpenCLI-Development-Identity'], 'local-development')

    setDevelopmentSession(false)
    const authenticatedHeaders = getApiAuthHeaders()
    assert.equal(authenticatedHeaders.Authorization, `Bearer ${identityToken}`)
    assert.equal(authenticatedHeaders['X-OpenCLI-Development-Identity'], undefined)

    setDevelopmentSession(true)
    process.env.NODE_ENV = 'production'
    const productionHeaders = getApiAuthHeaders()
    assert.equal(productionHeaders.Authorization, `Bearer ${identityToken}`)
    assert.equal(productionHeaders['X-OpenCLI-Development-Identity'], undefined)
  } finally {
    clearIdentityToken()
    setDevelopmentSession(false)
    stores.local.clear()
    stores.session.clear()
    if (originalNodeEnv === undefined) delete process.env.NODE_ENV
    else process.env.NODE_ENV = originalNodeEnv
    if (originalFlag === undefined) delete process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV
    else process.env.NEXT_PUBLIC_ALLOW_UNAUTHENTICATED_DEV = originalFlag
  }
})
