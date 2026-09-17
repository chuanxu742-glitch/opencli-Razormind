import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { afterEach, test } from 'node:test'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const reactMockUrl = `data:text/javascript,${encodeURIComponent(`
export const useState = (...args) => globalThis.__portalHookRunner.useState(...args)
export const useRef = (...args) => globalThis.__portalHookRunner.useRef(...args)
export const useEffect = (...args) => globalThis.__portalHookRunner.useEffect(...args)
`)}`
const apiMockUrl = `data:text/javascript,${encodeURIComponent(`
export const issueBrowserPortalTicket = (...args) => globalThis.__portalApi.issue(...args)
export const redeemBrowserPortalTicket = (...args) => globalThis.__portalApi.redeem(...args)
`)}`

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react') return { url: reactMockUrl, shortCircuit: true }
    if (specifier === '@/lib/api/browser-accounts') return { url: apiMockUrl, shortCircuit: true }
    if (specifier === '@/lib/browser-accounts/portal-protocol') {
      return { url: pathToFileURL(path.join(frontendRoot, 'lib/browser-accounts/portal-protocol.ts')).href, shortCircuit: true }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.ts')) {
      return {
        format: 'module',
        shortCircuit: true,
        source: stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8'), { mode: 'strip', sourceUrl: url }),
      }
    }
    return nextLoad(url, context)
  },
})

const {
  decodePortalBinaryFrame,
  encodePortalControlFrame,
  isNewPortalSequence,
  portalWebSocketUrl,
  portalPointerCoordinates,
} = await import(pathToFileURL(path.join(frontendRoot, 'lib/browser-accounts/portal-protocol.ts')).href)

const { useBrowserAccountPortal } = await import(pathToFileURL(path.join(frontendRoot, 'hooks/use-browser-account-portal.ts')).href)
const invokePortalHook = useBrowserAccountPortal

const binding = {
  workspace_id: 'workspace-1',
  account_id: 'account-1',
  session_id: 'session-1',
  epoch: 4,
  target: { tab_id: 12, frame_id: 'frame-1', document_id: 'document-1', origin: 'https://example.test' },
  view_generation: 9,
}

function binaryFrame({ metadata = {}, payload = new Uint8Array(), encoding = 1, sequence = 1 } = {}) {
  const encodedMetadata = new TextEncoder().encode(JSON.stringify({
    contract_version: 1,
    protocol: 'qrac2.portal.v1',
    sequence,
    encoding: encoding === 0 ? 'control-json' : 'pixel-binary',
    content_type: encoding === 0 ? 'application/json' : 'application/octet-stream',
    mime_type: encoding === 0 ? 'application/json' : 'image/png',
    byte_length: encoding === 0 ? null : payload.byteLength,
    binding,
    ...metadata,
  }))
  const result = new Uint8Array(16 + encodedMetadata.byteLength + payload.byteLength)
  result.set([81, 50, 80, 49, 1, encoding, 0, 0])
  const view = new DataView(result.buffer)
  view.setUint16(8, encodedMetadata.byteLength)
  view.setUint32(10, payload.byteLength)
  result.set(encodedMetadata, 16)
  result.set(payload, 16 + encodedMetadata.byteLength)
  return result.buffer
}

function validPixelFrame({ sequence = 1, pixel = {}, ...overrides } = {}) {
  const payload = new Uint8Array([137, 80, 78, 71])
  return binaryFrame({
    payload,
    sequence,
    metadata: {
      message: {
        ...binding,
        sequence,
        mime_type: 'image/png',
        region_kind: 'qr',
        byte_length: payload.byteLength,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        clip: { x: 40, y: 60, width: 800, height: 600 },
        ...pixel,
      },
      ...overrides,
    },
  })
}
class HookRunner {
  constructor() {
    this.states = []
    this.refs = []
    this.setterCalls = []
    this.stateCursor = 0
    this.refCursor = 0
    this.cleanup = null
    this.effectDependencies = null
  }

  useState(initial) {
    const index = this.stateCursor
    this.stateCursor += 1
    if (!(index in this.states)) this.states[index] = initial
    return [this.states[index], (value) => {
      const next = typeof value === 'function' ? value(this.states[index]) : value
      this.states[index] = next
      this.setterCalls.push({ index, value: next })
    }]
  }

  useRef(initial) {
    const index = this.refCursor
    this.refCursor += 1
    if (!(index in this.refs)) this.refs[index] = { current: initial }
    return this.refs[index]
  }

  useEffect(effect, dependencies) {
    if (this.effectDependencies && dependencies.every((value, index) => Object.is(value, this.effectDependencies[index]))) return
    this.cleanup?.()
    this.effectDependencies = dependencies
    this.cleanup = effect()
  }

  render() {
    this.stateCursor = 0
    this.refCursor = 0
    return invokePortalHook({ workspaceId: binding.workspace_id, accountId: binding.account_id, session: portalSession })
  }

  unmount() {
    this.cleanup?.()
    this.cleanup = null
  }
}

class MockWebSocket {
  static instances = []
  static OPEN = 1
  static CLOSING = 2

  constructor(url) {
    this.url = url
    this.readyState = 0
    this.sent = []
    this.closeCalls = 0
    MockWebSocket.instances.push(this)
  }

  send(data) {
    this.sent.push(data)
  }

  open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.({})
  }

  message(data) {
    this.onmessage?.({ data })
  }

  close() {
    this.closeCalls += 1
    this.readyState = 3
    this.onclose?.({})
  }
}

const portalSession = {
  id: binding.session_id,
  workspace_id: binding.workspace_id,
  account_id: binding.account_id,
  instance_id: null,
  node_id: null,
  node_boot_id: null,
  lease_id: null,
  epoch: binding.epoch,
  revision: 3,
  profile_id: null,
  profile_version: null,
  profile_state: 'new',
  login_rule_id: null,
  login_rule_version: null,
  tab_id: binding.target.tab_id,
  frame_id: binding.target.frame_id,
  document_id: binding.target.document_id,
  origin: binding.target.origin,
  view_generation: binding.view_generation,
  purpose: 'login',
  execution_id: null,
  command_id: null,
  status: 'challenge',
  expires_at: null,
  closed_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const issuedTicket = {
  session_revision: portalSession.revision,
  ticket_id: 'ticket-id',
  ticket: 'ticket',
  csrf_token: 'csrf',
}
const grantedTicket = { websocket_path: '/portal/ws?ticket=ticket' }
let activeHarness = null

function deferred() {
  let resolve
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

function installPortalHarness({ issue, redeem } = {}) {
  const previous = {
    WebSocket: globalThis.WebSocket,
    window: globalThis.window,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    createObjectURL: URL.createObjectURL,
    revokeObjectURL: URL.revokeObjectURL,
  }
  const timers = { next: 1, active: new Map(), cleared: [] }
  const api = {
    issueCalls: 0,
    redeemCalls: 0,
    issue: (...args) => {
      api.issueCalls += 1
      return issue ? issue(...args) : Promise.resolve(issuedTicket)
    },
    redeem: (...args) => {
      api.redeemCalls += 1
      return redeem ? redeem(...args) : Promise.resolve(grantedTicket)
    },
  }
  const runner = new HookRunner()
  globalThis.__portalApi = api
  globalThis.__portalHookRunner = runner
  globalThis.window = { location: { protocol: 'https:', origin: binding.target.origin } }
  globalThis.WebSocket = MockWebSocket
  MockWebSocket.instances = []
  globalThis.setTimeout = (callback, delay) => {
    const id = timers.next
    timers.next += 1
    timers.active.set(id, { callback, delay })
    return id
  }
  globalThis.clearTimeout = (id) => {
    if (id !== undefined) timers.cleared.push(id)
    timers.active.delete(id)
  }
  let objectUrlId = 0
  URL.createObjectURL = () => `blob:portal-${++objectUrlId}`
  const revokedUrls = []
  URL.revokeObjectURL = (url) => revokedUrls.push(url)
  activeHarness = { api, runner, timers, revokedUrls, restore: () => {
    globalThis.WebSocket = previous.WebSocket
    globalThis.window = previous.window
    globalThis.setTimeout = previous.setTimeout
    globalThis.clearTimeout = previous.clearTimeout
    URL.createObjectURL = previous.createObjectURL
    URL.revokeObjectURL = previous.revokeObjectURL
    delete globalThis.__portalApi
    delete globalThis.__portalHookRunner
    activeHarness = null
  } }
  return activeHarness
}

async function flushAsyncWork() {
  for (let index = 0; index < 5; index += 1) await Promise.resolve()
}


afterEach(() => activeHarness?.restore())
test('valid binary portal control and pixel frames decode with payload and binding intact', () => {
  const decoded = decodePortalBinaryFrame(validPixelFrame(), binding)
  assert.equal(decoded?.kind, 'pixel')
  assert.deepEqual([...decoded.bytes], [137, 80, 78, 71])
  assert.equal(decoded.pixel.region_kind, 'qr')
  assert.equal(decoded.pixel.mime_type, 'image/png')
  assert.deepEqual(decoded.pixel.clip, { x: 40, y: 60, width: 800, height: 600 })
  assert.deepEqual(decoded.binding, binding)

  const control = decodePortalBinaryFrame(binaryFrame({
    encoding: 0,
    metadata: { message: { ...binding, sequence: 1, kind: 'request_view' } },
  }), binding)
  assert.equal(control?.kind, 'control')
  assert.equal(control.control.kind, 'request_view')
  const nullableControl = decodePortalBinaryFrame(binaryFrame({
    encoding: 0,
    metadata: { message: { ...binding, sequence: 1, kind: 'request_view', field_ref: null, focused_field_ref: null } },
  }), binding)
  assert.equal(nullableControl?.kind, 'control')
  assert.equal(nullableControl.control.focused_field_ref, null)
  const nullablePixel = decodePortalBinaryFrame(validPixelFrame({ pixel: { focused_field_ref: null } }), binding)
  assert.equal(nullablePixel?.kind, 'pixel')
  assert.equal(nullablePixel.pixel.focused_field_ref, null)
})

test('portal coordinates use the actual rendered bounds and approved clip', () => {
  const clip = { x: 300, y: 100, width: 1200, height: 600 }
  const bounds = { left: 20, top: 40, width: 300, height: 150 }
  assert.deepEqual(portalPointerCoordinates(clip, bounds, 170, 115), { x: 900, y: 400 })
  assert.deepEqual(portalPointerCoordinates(clip, bounds, -50, 900), { x: 300, y: 699 })
  assert.equal(portalPointerCoordinates(clip, { ...bounds, width: 0 }, 170, 115), null)
  for (const invalid of [null, {}, { ...clip, width: 0 }, { ...clip, x: -1 }, { ...clip, y: 9000 }]) {
    assert.equal(decodePortalBinaryFrame(validPixelFrame({ pixel: { clip: invalid } }), binding), null)
  }
})

function outgoingControl(frame) {
  const view = new DataView(frame)
  return JSON.parse(new TextDecoder().decode(new Uint8Array(frame).slice(16, 16 + view.getUint16(8)))).message
}

test('QR takeover and form pointer/key controls preserve sequence and transient payloads', async () => {
  const harness = installPortalHarness()
  harness.runner.render()
  await flushAsyncWork()
  const socket = MockWebSocket.instances[0]
  socket.open()
  socket.message(validPixelFrame({ sequence: 2 }))
  let state = harness.runner.render()
  state.sendPointer('down', 100, 100)
  state.sendKey('Enter')
  assert.equal(socket.sent.length, 1, 'QR image must remain display-only')
  state.requestTakeover()
  assert.equal(outgoingControl(socket.sent.at(-1)).kind, 'takeover')
  socket.message(validPixelFrame({ sequence: 3, pixel: { region_kind: 'form', focused_field_ref: 'field-1' } }))
  state = harness.runner.render()
  assert.deepEqual(state.frameClip, { x: 40, y: 60, width: 800, height: 600 })
  state.sendPointer('down', 100, 100)
  state.sendPointer('move', 200, 100)
  state.sendPointer('up', 300, 100)
  for (const key of ['Tab', 'Enter', 'Backspace', 'Escape']) state.sendKey(key)
  const controls = socket.sent.map(outgoingControl)
  assert.deepEqual(controls.slice(2, 5).map((control) => control.sensitive_payload), [
    { value_present: false, key: null, x: 100, y: 100, pointer_action: 'down' },
    { value_present: false, key: null, x: 200, y: 100, pointer_action: 'move' },
    { value_present: false, key: null, x: 300, y: 100, pointer_action: 'up' },
  ])
  assert.deepEqual(controls.slice(5).map((control) => control.sensitive_payload.key), ['Tab', 'Enter', 'Backspace', 'Escape'])
  assert.deepEqual(controls[5].sensitive_payload, { value_present: false, key: 'Tab', x: null, y: null })
  assert.deepEqual(controls.map((control) => control.sequence), controls.map((_, index) => index + 1))
  const sent = socket.sent.length
  state.sendPointer('down', 1, 1)
  state.sendPointer('down', 100.5, 100)
  state.sendKey('a')
  assert.equal(socket.sent.length, sent)
  state.setInput('temporary-code')
  state = harness.runner.render()
  state.sendInput()
  assert.equal(outgoingControl(socket.sent.at(-1)).field_ref, 'field-1')
  assert.equal(harness.runner.render().input, '')
  const frame = socket.sent.at(-1)
  const offset = 16 + new DataView(frame).getUint16(8)
  assert.equal(new TextDecoder().decode(new Uint8Array(frame).slice(offset)), 'temporary-code')
  socket.message(validPixelFrame({ sequence: 4, pixel: { region_kind: 'approved' } }))
  harness.runner.render().sendKey('Tab')
  assert.equal(outgoingControl(socket.sent.at(-1)).sensitive_payload.key, 'Tab')
  harness.runner.unmount()
  const afterUnmount = socket.sent.length
  state.sendKey('Enter')
  state.sendPointer('down', 100, 100)
  assert.equal(socket.sent.length, afterUnmount)
})

test('malformed, header, and declared-length frames are rejected', () => {
  const malformed = new Uint8Array(16)
  malformed.set([81, 50, 80, 49, 1, 1])
  assert.equal(decodePortalBinaryFrame(malformed.buffer, binding), null)

  const wrongMagic = new Uint8Array(validPixelFrame())
  wrongMagic[0] = 0
  assert.equal(decodePortalBinaryFrame(wrongMagic.buffer, binding), null)

  const badVersion = new Uint8Array(validPixelFrame())
  badVersion[4] = 2
  assert.equal(decodePortalBinaryFrame(badVersion.buffer, binding), null)

  const wrongLength = new Uint8Array(validPixelFrame())
  const view = new DataView(wrongLength.buffer)
  view.setUint32(10, 999)
  assert.equal(decodePortalBinaryFrame(wrongLength.buffer, binding), null)
})

test('identity, epoch, target, view-generation, and expired frames are rejected', () => {
  for (const field of ['workspace_id', 'account_id', 'session_id', 'epoch', 'view_generation']) {
    const changed = { ...binding, [field]: field === 'epoch' || field === 'view_generation' ? binding[field] + 1 : `${binding[field]}-other` }
    const frame = validPixelFrame({ binding: changed })
    assert.equal(decodePortalBinaryFrame(frame, binding), null, `changed ${field}`)
  }
  for (const field of ['tab_id', 'frame_id', 'document_id', 'origin']) {
    const changedTarget = { ...binding.target, [field]: field === 'tab_id' ? 99 : `${binding.target[field]}-other` }
    const frame = validPixelFrame({ binding: { ...binding, target: changedTarget } })
    assert.equal(decodePortalBinaryFrame(frame, binding), null, `changed target ${field}`)
  }
  const expired = validPixelFrame({ message: { ...binding, sequence: 1, mime_type: 'image/png', region_kind: 'qr', byte_length: 4, expires_at: new Date(Date.now() - 1).toISOString() } })
  assert.equal(decodePortalBinaryFrame(expired, binding), null)
})

test('incoming sequence acceptance rejects replay and non-increasing frames', () => {
  assert.equal(isNewPortalSequence(1, 0), true)
  assert.equal(isNewPortalSequence(2, 1), true)
  assert.equal(isNewPortalSequence(1, 1), false)
  assert.equal(isNewPortalSequence(1, 2), false)
})

test('outgoing sensitive input stays in payload and never in metadata', () => {
  const secret = 'correct horse battery staple'
  const frame = encodePortalControlFrame({ kind: 'field_input', sensitive_payload: { value: secret } }, binding, 7, secret)
  const bytes = new Uint8Array(frame)
  const view = new DataView(frame)
  const metadataBytes = view.getUint16(8)
  const metadataText = new TextDecoder().decode(bytes.slice(16, 16 + metadataBytes))
  const metadata = JSON.parse(metadataText)
  assert.doesNotMatch(metadataText, /correct horse battery staple/)
  assert.deepEqual(metadata.message.sensitive_payload, { value_present: true, key: null, x: null, y: null })
  assert.equal(new TextDecoder().decode(bytes.slice(16 + metadataBytes)), secret)
})

test('HTTPS portal URL accepts only same-origin relative websocket paths', () => {
  const location = { protocol: 'https:', origin: 'https://portal.example.test' }
  assert.equal(portalWebSocketUrl('/api/portal/ws?ticket=1', location), 'wss://portal.example.test/api/portal/ws?ticket=1')
  assert.equal(portalWebSocketUrl('https://attacker.test/ws', location), null)
  assert.equal(portalWebSocketUrl('//attacker.test/ws', location), null)
  assert.equal(portalWebSocketUrl('/api/portal/ws', { protocol: 'http:', origin: 'http://portal.example.test' }), null)
})

test('hook connects and requests the current view after authorization', async () => {
  const harness = installPortalHarness()
  const state = harness.runner.render()
  await flushAsyncWork()
  assert.equal(harness.api.issueCalls, 1)
  assert.equal(harness.api.redeemCalls, 1)
  assert.equal(MockWebSocket.instances.length, 1)
  const socket = MockWebSocket.instances[0]
  socket.open()
  assert.equal(harness.runner.states[0], 'connected')
  assert.equal(socket.sent.length, 1)
  const request = decodePortalBinaryFrame(socket.sent[0], binding)
  assert.equal(request?.kind, 'control')
  assert.equal(request.control.kind, 'request_view')
  assert.equal(state.transport, 'opening')
})

test('invalid or repeated frames clear projection, input, and expiry timer', async () => {
  const harness = installPortalHarness()
  const state = harness.runner.render()
  await flushAsyncWork()
  const socket = MockWebSocket.instances[0]
  socket.open()
  socket.message(validPixelFrame({ sequence: 2 }))
  const frameUrl = harness.runner.states[2]
  assert.match(frameUrl, /^blob:portal-/)
  assert.equal(harness.timers.active.size, 1)
  const timerId = [...harness.timers.active.keys()][0]
  state.setInput('secret')
  socket.message(validPixelFrame({ sequence: 2 }))
  assert.equal(harness.runner.states[0], 'error')
  assert.equal(harness.runner.states[2], null)
  assert.equal(harness.runner.states[5], '')
  assert.equal(harness.timers.active.size, 0)
  assert.ok(harness.timers.cleared.includes(timerId))
  assert.deepEqual(harness.revokedUrls, [frameUrl])
  assert.equal(socket.closeCalls, 1)
})

test('normal close and unmount release portal projection and socket resources', async () => {
  const harness = installPortalHarness()
  harness.runner.render()
  await flushAsyncWork()
  const socket = MockWebSocket.instances[0]
  socket.open()
  socket.message(validPixelFrame({ sequence: 2 }))
  const frameUrl = harness.runner.states[2]
  socket.close()
  assert.equal(harness.runner.states[0], 'closed')
  assert.equal(harness.runner.states[2], null)
  assert.deepEqual(harness.revokedUrls, [frameUrl])
  assert.equal(harness.timers.active.size, 0)
  harness.runner.unmount()
  assert.ok(socket.closeCalls >= 2)
})

test('authorization finishing after unmount never redeems a ticket', async () => {
  const ticket = deferred()
  const harness = installPortalHarness({ issue: () => ticket.promise })
  harness.runner.render()
  harness.runner.unmount()
  ticket.resolve(issuedTicket)
  await flushAsyncWork()
  assert.equal(harness.api.redeemCalls, 0)
  assert.equal(MockWebSocket.instances.length, 0)
})

test('only explicit normal route expiry schedules fresh authorization', async () => {
  const harness = installPortalHarness()
  harness.runner.render()
  await flushAsyncWork()
  const socket = MockWebSocket.instances[0]
  socket.open()
  socket.message(validPixelFrame({ sequence: 2 }))
  socket.onclose({ code: 1000, reason: 'Portal route expired' })
  assert.equal(harness.runner.states[2], null)
  assert.equal(harness.timers.active.size, 1)
  const timer = [...harness.timers.active.values()][0]
  assert.equal(timer.delay, 500)
  timer.callback()
  harness.runner.cleanup()
  harness.runner.render()
  await flushAsyncWork()
  assert.equal(harness.api.issueCalls, 2)
  assert.equal(harness.api.redeemCalls, 2)
  assert.equal(MockWebSocket.instances.length, 2)
  MockWebSocket.instances[1].onclose({ code: 4403, reason: 'Portal route expired' })
  assert.equal(harness.timers.active.size, 0)
  harness.runner.unmount()
})

test('pixel refresh requests a new view without extending the old pixel lifetime', async () => {
  const harness = installPortalHarness()
  harness.runner.render()
  await flushAsyncWork()
  const socket = MockWebSocket.instances[0]
  socket.open()
  socket.message(validPixelFrame({ sequence: 2 }))
  const [timerId, timer] = [...harness.timers.active][0]
  assert.ok(timer.delay <= 2000)
  harness.timers.active.delete(timerId)
  timer.callback()
  assert.equal(socket.sent.length, 2)
  assert.equal(decodePortalBinaryFrame(socket.sent[1], binding).control.kind, 'request_view')
  const expiry = [...harness.timers.active.values()][0]
  expiry.callback()
  assert.equal(harness.runner.states[2], null)
  harness.runner.unmount()
})

test('redemption finishing after unmount never opens a socket', async () => {
  const grant = deferred()
  const harness = installPortalHarness({ redeem: () => grant.promise })
  harness.runner.render()
  await flushAsyncWork()
  assert.equal(harness.api.redeemCalls, 1)
  harness.runner.unmount()
  grant.resolve(grantedTicket)
  await flushAsyncWork()
  assert.equal(MockWebSocket.instances.length, 0)
})
