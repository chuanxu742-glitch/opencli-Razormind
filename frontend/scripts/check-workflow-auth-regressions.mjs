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

function installFetchSpy() {
  const requests = []
  globalThis.fetch = async (input, init = {}) => {
    requests.push(new Request(new URL(String(input), 'http://workflow.test').href, init))
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
