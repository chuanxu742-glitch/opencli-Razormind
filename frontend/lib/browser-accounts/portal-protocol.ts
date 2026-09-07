import type { BrowserLoginSession } from '@/lib/api/browser-accounts'

export type PortalTarget = {
  tab_id: string | number
  frame_id: string | number
  document_id: string | number
  origin: string
}

export type PortalBinding = {
  workspace_id: string
  account_id: string
  session_id: string
  epoch: number
  target: PortalTarget
  view_generation: number
}

export type PortalWireControl = PortalBinding & {
  kind: 'field_input' | 'pointer' | 'key' | 'request_view' | 'takeover'
  sequence: number
  field_ref?: string
  focused_field_ref?: string
}

export type PortalWirePixel = PortalBinding & {
  sequence: number
  mime_type: 'image/png' | 'image/jpeg' | 'image/webp'
  region_kind: 'qr' | 'form' | 'approved'
  byte_length: number
  expires_at: string
  frame_bytes?: string
  focused_field_ref?: string
}

export const MAX_PORTAL_FRAME_BYTES = 4_000_000
export const MAX_PORTAL_METADATA_BYTES = 128 * 1024
export const PORTAL_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp'] as const
export const PORTAL_REGION_KINDS = ['qr', 'form', 'approved'] as const

export function makePortalCsrfToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function sessionTarget(session: BrowserLoginSession): PortalTarget | null {
  if (session.tab_id == null || session.frame_id == null || session.document_id == null || !session.origin) return null
  return {
    tab_id: session.tab_id,
    frame_id: session.frame_id,
    document_id: session.document_id,
    origin: session.origin,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readNonEmptyString(value: unknown, maximum = 256): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum ? value : null
}

function readNonNegativeInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function readPositiveInteger(value: unknown, maximum = MAX_PORTAL_FRAME_BYTES): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 && value <= maximum ? value : null
}

function readPortalTarget(value: unknown): PortalTarget | null {
  if (!isRecord(value)) return null
  const targetId = (id: unknown) => typeof id === 'number' && Number.isSafeInteger(id) && id >= 0 ? id : readNonEmptyString(id)
  const tab_id = targetId(value.tab_id)
  const frame_id = targetId(value.frame_id)
  const document_id = targetId(value.document_id)
  const origin = readNonEmptyString(value.origin, 2048)
  return tab_id !== null && frame_id !== null && document_id !== null && origin ? { tab_id, frame_id, document_id, origin } : null
}

export function readPortalBinding(value: unknown): PortalBinding | null {
  if (!isRecord(value)) return null
  const workspace_id = readNonEmptyString(value.workspace_id)
  const account_id = readNonEmptyString(value.account_id)
  const session_id = readNonEmptyString(value.session_id)
  const epoch = readNonNegativeInteger(value.epoch)
  const target = readPortalTarget(value.target)
  const view_generation = readNonNegativeInteger(value.view_generation)
  return workspace_id && account_id && session_id && epoch !== null && target && view_generation !== null
    ? { workspace_id, account_id, session_id, epoch, target, view_generation }
    : null
}

export function samePortalBinding(left: PortalBinding, right: PortalBinding): boolean {
  return left.workspace_id === right.workspace_id
    && left.account_id === right.account_id
    && left.session_id === right.session_id
    && left.epoch === right.epoch
    && left.view_generation === right.view_generation
    && left.target.tab_id === right.target.tab_id
    && left.target.frame_id === right.target.frame_id
    && left.target.document_id === right.target.document_id
    && left.target.origin === right.target.origin
}
export function isNewPortalSequence(sequence: number, lastSequence: number): boolean {
  return sequence > lastSequence
}

function readPortalIdentity(value: unknown): PortalBinding & { sequence: number } | null {

  if (!isRecord(value)) return null
  const binding = readPortalBinding(value)
  const sequence = readPositiveInteger(value.sequence)
  return binding && sequence !== null ? { ...binding, sequence } : null
}

function readPortalControl(value: unknown): PortalWireControl | null {
  if (!isRecord(value)) return null
  const identity = readPortalIdentity(value)
  const kind = value.kind
  if (!identity || (kind !== 'field_input' && kind !== 'pointer' && kind !== 'key' && kind !== 'request_view' && kind !== 'takeover')) return null
  const field_ref = value.field_ref === undefined ? undefined : readNonEmptyString(value.field_ref, 128)
  const focused_field_ref = value.focused_field_ref === undefined ? undefined : readNonEmptyString(value.focused_field_ref, 128)
  if (value.field_ref !== undefined && !field_ref || value.focused_field_ref !== undefined && !focused_field_ref) return null
  return { ...identity, kind, ...(field_ref ? { field_ref } : {}), ...(focused_field_ref ? { focused_field_ref } : {}) }
}

function readPortalPixel(value: unknown, bytes: Uint8Array): PortalWirePixel | null {
  if (!isRecord(value) || bytes.byteLength < 1 || bytes.byteLength > MAX_PORTAL_FRAME_BYTES) return null
  const identity = readPortalIdentity(value)
  const mime_type = typeof value.mime_type === 'string' ? PORTAL_MIME_TYPES.find((candidate) => candidate === value.mime_type) : undefined
  const region_kind = typeof value.region_kind === 'string' ? PORTAL_REGION_KINDS.find((candidate) => candidate === value.region_kind) : undefined
  const byte_length = readPositiveInteger(value.byte_length)
  const expires_at = typeof value.expires_at === 'string' ? value.expires_at : ''
  if (!identity || !mime_type || !region_kind || byte_length !== bytes.byteLength || !Number.isFinite(Date.parse(expires_at)) || Date.parse(expires_at) <= Date.now()) return null
  const focused_field_ref = value.focused_field_ref === undefined ? undefined : readNonEmptyString(value.focused_field_ref, 128)
  if (value.focused_field_ref !== undefined && !focused_field_ref) return null
  return { ...identity, mime_type, region_kind, byte_length, expires_at, ...(focused_field_ref ? { focused_field_ref } : {}) }
}

export type DecodedPortalFrame =
  | { kind: 'control'; binding: PortalBinding; sequence: number; control: PortalWireControl }
  | { kind: 'pixel'; binding: PortalBinding; sequence: number; pixel: PortalWirePixel; bytes: Uint8Array }

export function decodePortalBinaryFrame(data: ArrayBuffer, expectedBinding: PortalBinding): DecodedPortalFrame | null {
  const bytes = new Uint8Array(data)
  if (bytes.byteLength < 16 || bytes.byteLength > MAX_PORTAL_FRAME_BYTES + MAX_PORTAL_METADATA_BYTES + 16 || new TextDecoder().decode(bytes.slice(0, 4)) !== 'Q2P1') return null
  const view = new DataView(data)
  if (view.getUint8(4) !== 1 || view.getUint8(6) !== 0 || view.getUint8(7) !== 0 || view.getUint16(14) !== 0) return null
  const encoding = view.getUint8(5)
  const metadataBytes = view.getUint16(8)
  const payloadBytes = view.getUint32(10)
  if (!metadataBytes || metadataBytes > MAX_PORTAL_METADATA_BYTES || 16 + metadataBytes + payloadBytes !== bytes.byteLength || payloadBytes > MAX_PORTAL_FRAME_BYTES) return null
  try {
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes.slice(16, 16 + metadataBytes)))
    if (!isRecord(parsed) || parsed.contract_version !== 1 || parsed.protocol !== 'qrac2.portal.v1' || parsed.encoding !== (encoding === 0 ? 'control-json' : encoding === 1 ? 'pixel-binary' : 'unsupported')) return null
    const binding = readPortalBinding(parsed.binding)
    const sequence = readPositiveInteger(parsed.sequence)
    const rawMessage = parsed.message
    if (!binding || !samePortalBinding(binding, expectedBinding) || sequence === null || !isRecord(rawMessage)) return null
    if (encoding === 0) {
      if (payloadBytes !== 0 || parsed.content_type !== 'application/json' || parsed.byte_length !== null) return null
      const control = readPortalControl(rawMessage)
      return control && samePortalBinding(control, binding) && control.sequence === sequence ? { kind: 'control', binding, sequence, control } : null
    }
    if (parsed.content_type !== 'application/octet-stream' || parsed.mime_type !== rawMessage.mime_type || parsed.byte_length !== payloadBytes) return null
    const payload = bytes.slice(16 + metadataBytes)
    const pixel = readPortalPixel(rawMessage, payload)
    return pixel && samePortalBinding(pixel, binding) && pixel.sequence === sequence ? { kind: 'pixel', binding, sequence, pixel, bytes: payload } : null
  } catch {
    return null
  }
}

export function encodePortalControlFrame(
  control: Record<string, unknown>,
  binding: Record<string, unknown>,
  sequence: number,
  inputValue?: string,
): ArrayBuffer {
  const payload = inputValue ? new TextEncoder().encode(inputValue) : new Uint8Array()
  const message = {
    ...control,
    ...(inputValue !== undefined ? {
      sensitive_payload: { value_present: true, key: null, x: null, y: null },
    } : {}),
  }
  const metadata = new TextEncoder().encode(JSON.stringify({
    contract_version: 1,
    protocol: 'qrac2.portal.v1',
    sequence,
    encoding: 'control-json',
    content_type: 'application/json',
    mime_type: 'application/json',
    byte_length: null,
    binding,
    message,
  }))
  const header = new ArrayBuffer(16)
  const view = new DataView(header)
  new Uint8Array(header).set([81, 50, 80, 49])
  view.setUint8(4, 1)
  view.setUint8(5, 0)
  view.setUint16(8, metadata.byteLength)
  view.setUint32(10, payload.byteLength)
  const result = new Uint8Array(16 + metadata.byteLength + payload.byteLength)
  result.set(new Uint8Array(header))
  result.set(metadata, 16)
  result.set(payload, 16 + metadata.byteLength)
  return result.buffer
}

export type PortalLocation = Pick<Location, 'protocol' | 'origin'>

export function portalWebSocketUrl(path: string, location: PortalLocation | null): string | null {
  if (!location || location.protocol !== 'https:' || !path.startsWith('/') || path.startsWith('//')) return null
  try {
    const url = new URL(path, location.origin)
    if (url.origin !== location.origin) return null
    url.protocol = 'wss:'
    return url.toString()
  } catch {
    return null
  }
}
