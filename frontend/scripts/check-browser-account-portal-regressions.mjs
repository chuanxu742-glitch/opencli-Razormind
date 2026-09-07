import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

registerHooks({
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
} = await import(pathToFileURL(path.join(frontendRoot, 'lib/browser-accounts/portal-protocol.ts')).href)

const binding = {
  workspace_id: 'workspace-1',
  account_id: 'account-1',
  session_id: 'session-1',
  epoch: 4,
  target: { tab_id: 12, frame_id: 'frame-1', document_id: 'document-1', origin: 'https://example.test' },
  view_generation: 9,
}

function binaryFrame({ metadata = {}, payload = new Uint8Array(), encoding = 1 } = {}) {
  const encodedMetadata = new TextEncoder().encode(JSON.stringify({
    contract_version: 1,
    protocol: 'qrac2.portal.v1',
    sequence: 1,
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

function validPixelFrame(overrides = {}) {
  const payload = new Uint8Array([137, 80, 78, 71])
  return binaryFrame({
    payload,
    metadata: {
      message: {
        ...binding,
        sequence: 1,
        mime_type: 'image/png',
        region_kind: 'qr',
        byte_length: payload.byteLength,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      },
      ...overrides,
    },
  })
}

test('valid binary portal control and pixel frames decode with payload and binding intact', () => {
  const decoded = decodePortalBinaryFrame(validPixelFrame(), binding)
  assert.equal(decoded?.kind, 'pixel')
  assert.deepEqual([...decoded.bytes], [137, 80, 78, 71])
  assert.equal(decoded.pixel.region_kind, 'qr')
  assert.equal(decoded.pixel.mime_type, 'image/png')
  assert.deepEqual(decoded.binding, binding)

  const control = decodePortalBinaryFrame(binaryFrame({
    encoding: 0,
    metadata: { message: { ...binding, sequence: 1, kind: 'request_view' } },
  }), binding)
  assert.equal(control?.kind, 'control')
  assert.equal(control.control.kind, 'request_view')
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
