import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import path from 'node:path'
import { test } from 'node:test'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

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

const { sanitizeReturnTo } = await import('../lib/auth/oidc.ts')
const { encodeShareState, decodeShareState, buildShareUrl, loadShareStateFromUrl } = await import('../lib/flow/share-state.ts')
const { parseWorkflowProject } = await import('../lib/workflow/schema.ts')
const { workflowProjectToReactFlow } = await import('../lib/workflow/to-react-flow.ts')

test('returnTo rejects browser URL normalization escapes and non-path input', () => {
  for (const value of [undefined, null, {}, '', 'https://evil.invalid', '//evil.invalid', '/\\evil.invalid/review', '/\t/evil.invalid', '/\n/evil.invalid', '/\r/evil.invalid', '/\u0000x', '/\u007fx', '/..//evil.invalid']) {
    assert.equal(sanitizeReturnTo(value), '/studio', String(value))
  }
})

test('returnTo preserves legal path/query/hash while normalizing the path', () => {
  for (const value of ['/', '/studio', '/records?project=a%20b&view=graph#item-1', '/records?returnTo=https%3A%2F%2Fexample.com']) {
    assert.equal(sanitizeReturnTo(value), value)
  }
  assert.equal(sanitizeReturnTo('/records/../studio?q=1#x'), '/studio?q=1#x')
})

const project = parseWorkflowProject({ id: 'share', name: 'Share', profile: 'intelligence', nodes: [{ id: 'start', kind: 'schedule', capability: 'trigger' }, { id: 'end', kind: 'sink', capability: 'store' }], edges: [{ id: 'link', source: 'start', target: 'end' }] })
const validState = () => ({ workflowProject: structuredClone(project), ...workflowProjectToReactFlow(project), drawings: [{ id: 'stroke', points: [[0, 1], [2, 3, 0.5]], color: '#fff', size: 2 }] })

test('valid share restores canonical defaults, layout and drawings', () => {
  const state = validState()
  const decoded = decodeShareState(encodeShareState(state))
  assert.ok(decoded)
  assert.deepEqual(decoded.workflowProject, project)
  assert.deepEqual(decoded.nodes.map((node) => node.position), state.nodes.map((node) => node.position))
  assert.deepEqual(decoded.drawings, state.drawings)
  assert.deepEqual(loadShareStateFromUrl(buildShareUrl(state, 'https://local.invalid/studio?x=1')), decoded)
})

test('existing workflow fixtures round-trip through the actual canvas mapper', () => {
  for (const fixture of ['workflow-intelligence.json', 'workflow-a-share-premarket-research.json']) {
    const fixtureProject = parseWorkflowProject(JSON.parse(readFileSync(path.join(frontendRoot, 'lib/workflow/fixtures', fixture), 'utf8')))
    const canvas = workflowProjectToReactFlow(fixtureProject)
    canvas.nodes[0].style = { width: 260, background: '#fff' }
    canvas.nodes[0].sourcePosition = 'right'
    if (canvas.edges[0]) canvas.edges[0].markerEnd = { type: 'arrowclosed', color: '#fff' }
    const decoded = decodeShareState(encodeShareState({ workflowProject: fixtureProject, ...canvas }))
    assert.ok(decoded, fixture)
    assert.deepEqual(decoded.workflowProject, JSON.parse(JSON.stringify(fixtureProject)))
    assert.deepEqual(decoded.nodes, JSON.parse(JSON.stringify(canvas.nodes)))
    assert.deepEqual(decoded.edges, JSON.parse(JSON.stringify(canvas.edges)))
  }
})

test('incomplete and malformed share payloads are rejected before state mutation', () => {
  const cases = [
    (state) => { delete state.workflowProject },
    (state) => { delete state.workflowProject.profile },
    (state) => { state.workflowProject.edges[0].source = 'missing' },
    (state) => { state.workflowProject.nodes.push(state.workflowProject.nodes[0]) },
    (state) => { state.workflowProject.nodes[0].internals = { nodes: [null], edges: [] } },
    (state) => { delete state.nodes },
    (state) => { state.nodes = [null] },
    (state) => { delete state.nodes[0].data },
    (state) => { state.nodes[0].data.label = {} },
    (state) => { state.nodes[0].data.fields = [{}] },
    (state) => { state.nodes[0].data.parameterInterface = {} },
    (state) => { state.nodes[0].position.x = 'bad' },
    (state) => { state.nodes.push(state.nodes[0]) },
    (state) => { state.nodes[0].parentId = state.nodes[1].id; state.nodes[1].parentId = state.nodes[0].id },
    (state) => { state.nodes[0].parentId = 'missing' },
    (state) => { state.edges[0].target = 'missing' },
    (state) => { state.edges.push(state.edges[0]) },
    (state) => { state.edges[0].data = { waypoints: [null] } },
    (state) => { state.drawings[0].points = [[]] },
  ]
  for (const mutate of cases) {
    const state = validState()
    mutate(state)
    assert.equal(decodeShareState(encodeShareState(state)), null, mutate.toString())
  }
  assert.equal(decodeShareState('not-compressed-json'), null)
  assert.equal(decodeShareState(encodeShareState({})), null)
})

test('untrusted runtime data is not restored from a share', () => {
  const state = validState()
  state.nodes[0].data.runtimeEvidenceBatches = { length: 1 }
  state.nodes[0].data.runtimeCapability = { missing: {} }
  const decoded = decodeShareState(encodeShareState(state))
  assert.ok(decoded)
  assert.equal(decoded.nodes[0].data.runtimeEvidenceBatches, undefined)
  assert.equal(decoded.nodes[0].data.runtimeCapability, undefined)
})

