'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'next/navigation'
import { CheckCircle2, ExternalLink, Loader2, Plus, RefreshCw, ShieldAlert, Wifi, X } from 'lucide-react'
import { toast } from 'sonner'

import { useAuth } from '@/components/auth/auth-provider'
import { useMyWorkspaces } from '@/lib/api/hooks'
import {
  createBrowserAccount,
  createBrowserLoginSession,
  getBrowserAccount,
  getBrowserLoginSession,
  issueBrowserPortalTicket,
  listBrowserAccounts,
  listBrowserWorkspaceMembers,
  operateBrowserAccount,
  performBrowserSessionAction,
  redeemBrowserPortalTicket,
  type BrowserAccount,
  type BrowserAccountOperationRequest,
  type BrowserAccountStatus,
  type BrowserLoginSession,
  type PortalTicketGrant,
} from '@/lib/api/browser-accounts'
import { BACKEND_HINT, EmptyState, ErrorState, LoadingState } from '@/components/shell/data-states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

const statusLabels: Record<BrowserAccountStatus, string> = {
  opening: '打开中',
  presenting: '展示中',
  refreshing: '刷新中',
  verifying: '验证中',
  challenge: '需处理',
  unknown: '未知',
  saving: '保存中',
  saved: '已保存',
  dormant: '休眠',
  expired: '已过期',
  closed: '已关闭',
  error: '错误',
}

const statusClass: Record<BrowserAccountStatus, string> = {
  opening: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  presenting: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  refreshing: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  verifying: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  challenge: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  unknown: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-700 dark:text-zinc-300',
  saving: 'border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300',
  saved: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  dormant: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-700 dark:text-zinc-300',
  expired: 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
  closed: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-700 dark:text-zinc-300',
  error: 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '浏览器账号操作失败'
}

function AccountStatus({ status }: { status: BrowserAccountStatus }) {
  return <Badge variant="outline" className={statusClass[status]}>{statusLabels[status]}</Badge>
}

function EvidenceSummary({ account }: { account: BrowserAccount }) {
  const evidence = typeof account.auth_evidence === 'string'
    ? account.auth_evidence
    : account.auth_evidence?.kind ?? 'unknown'
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <div className="rounded-md border p-3">
        <p className="text-xs text-muted-foreground">认证凭据</p>
        <p className="mt-1 font-medium">{evidence}</p>
        <p className="mt-1 text-xs text-muted-foreground">{account.evidence_source ?? '未报告'}</p>
      </div>
      <div className="rounded-md border p-3">
        <p className="text-xs text-muted-foreground">平台身份</p>
        <p className="mt-1 truncate font-medium">{account.platform_identity?.display_name ?? '未验证'}</p>
        <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.platform_identity?.provider ?? '身份不可用'}</p>
      </div>
      <div className="rounded-md border p-3">
        <p className="text-xs text-muted-foreground">运行资源分配</p>
        <p className="mt-1 truncate font-mono text-xs">{account.runtime_bundle_id ?? '未分配'}</p>
        <p className="mt-1 text-xs text-muted-foreground">{account.node_id ? `节点 ${account.node_id}` : '节点待定'}</p>
      </div>
    </div>
  )
}
function CreateAccountForm({ workspaceId, canManage, onCreated }: { workspaceId: string | null; canManage: boolean; onCreated: (account: BrowserAccount) => void }) {
  const [site, setSite] = useState('')
  const [label, setLabel] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [runtimeBundleId, setRuntimeBundleId] = useState('')
  const [loginRuleId, setLoginRuleId] = useState('')
  const [loginRuleVersion, setLoginRuleVersion] = useState('')
  const mutation = useMutation({
    mutationFn: () => {
      if (!canManage) throw new Error('当前成员角色无权创建浏览器账号')
      if (!workspaceId) throw new Error('请先选择工作区')
      const cleanSite = site.trim()
      const cleanLabel = label.trim()
      if (!cleanSite || !cleanLabel) throw new Error('站点和账号名称不能为空')
      return createBrowserAccount(workspaceId, {
        workspace_id: workspaceId,
        site: cleanSite,
        label: cleanLabel,
        ...(nodeId.trim() ? { node_id: nodeId.trim() } : {}),
        ...(runtimeBundleId.trim() ? { runtime_bundle_id: runtimeBundleId.trim() } : {}),
        ...(loginRuleId.trim() ? { login_rule_id: loginRuleId.trim() } : {}),
        ...(loginRuleVersion.trim() ? { login_rule_version: loginRuleVersion.trim() } : {}),
      }, `browser-account-create-${crypto.randomUUID()}`)
    },
    onSuccess: (account) => {
      setSite('')
      setLabel('')
      onCreated(account)
      toast.success('浏览器账号已创建')
    },
    onError: (error) => toast.error(errorText(error)),
  })
  return (
    <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span>平台 / 站点</span><Input value={site} onChange={(event) => setSite(event.target.value)} placeholder="example.com" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>账号名称</span><Input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="研究账号" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>所属节点 ID <span className="text-muted-foreground">（可选）</span></span><Input value={nodeId} onChange={(event) => setNodeId(event.target.value)} placeholder="不透明节点 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>运行时包 ID <span className="text-muted-foreground">（可选）</span></span><Input value={runtimeBundleId} onChange={(event) => setRuntimeBundleId(event.target.value)} placeholder="已验证的包 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>登录规则 ID <span className="text-muted-foreground">（可选）</span></span><Input value={loginRuleId} onChange={(event) => setLoginRuleId(event.target.value)} placeholder="已验证的规则 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>登录规则版本 <span className="text-muted-foreground">（可选）</span></span><Input value={loginRuleVersion} onChange={(event) => setLoginRuleVersion(event.target.value)} placeholder="v1" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
      </div>
      <p className="text-xs text-muted-foreground">凭据、二维码内容和临时表单输入仅保留在已批准的登录门户中，不会写入此处。</p>
      <Button type="submit" disabled={!workspaceId || !canManage || mutation.isPending}>{mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}{mutation.isPending ? '正在打开账号…' : '添加浏览器账号'}</Button>
    </form>
  )
}

type PortalTarget = {
  tab_id: string | number
  frame_id: string | number
  document_id: string | number
  origin: string
}

type PortalBinding = {
  workspace_id: string
  account_id: string
  session_id: string
  epoch: number
  target: PortalTarget
  view_generation: number
}

type PortalWireControl = PortalBinding & {
  kind: 'field_input' | 'pointer' | 'key' | 'request_view' | 'takeover'
  sequence: number
  field_ref?: string
  focused_field_ref?: string
}

type PortalWirePixel = PortalBinding & {
  sequence: number
  mime_type: 'image/png' | 'image/jpeg' | 'image/webp'
  region_kind: 'qr' | 'form' | 'approved'
  byte_length: number
  expires_at: string
  frame_bytes?: string
  focused_field_ref?: string
}

const MAX_PORTAL_FRAME_BYTES = 4_000_000
const MAX_PORTAL_METADATA_BYTES = 128 * 1024
const PORTAL_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp'] as const
const PORTAL_REGION_KINDS = ['qr', 'form', 'approved'] as const
function makePortalCsrfToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function sessionTarget(session: BrowserLoginSession): PortalTarget | null {
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

function readPortalBinding(value: unknown): PortalBinding | null {
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

function samePortalBinding(left: PortalBinding, right: PortalBinding): boolean {
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

type DecodedPortalFrame =
  | { kind: 'control'; binding: PortalBinding; sequence: number; control: PortalWireControl }
  | { kind: 'pixel'; binding: PortalBinding; sequence: number; pixel: PortalWirePixel; bytes: Uint8Array }

function portalWebSocketUrl(path: string): string | null {
  if (typeof window === 'undefined' || window.location.protocol !== 'https:' || !path.startsWith('/') || path.startsWith('//')) return null
  try {
    const url = new URL(path, window.location.origin)
    if (url.origin !== window.location.origin) return null
    url.protocol = 'wss:'
    return url.toString()
  } catch {
    return null
  }
}

function decodePortalBinaryFrame(data: ArrayBuffer, expectedBinding: PortalBinding): DecodedPortalFrame | null {
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

function encodePortalControlFrame(
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
function PortalView({ workspaceId, accountId, session, onClose }: { workspaceId: string; accountId: string; session: BrowserLoginSession; onClose: () => void }) {
  const [transport, setTransport] = useState<'opening' | 'connected' | 'closed' | 'error'>('opening')
  const [message, setMessage] = useState('正在打开同一会话门户…')
  const [frameUrl, setFrameUrl] = useState<string | null>(null)
  const [frameKind, setFrameKind] = useState<'qr' | 'form' | 'approved' | null>(null)
  const [focusedFieldRef, setFocusedFieldRef] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const sequenceRef = useRef(1)
  const sendControlRef = useRef<(kind: PortalWireControl['kind'], value?: string, fieldRef?: string) => void>(() => undefined)

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let currentFrameUrl: string | null = null
    let lastIncomingSequence = 0
    let terminalError = false
    let frameExpiryTimer: ReturnType<typeof setTimeout> | undefined
    const target = sessionTarget(session)
    const expectedBinding: PortalBinding | null = target
      ? { workspace_id: workspaceId, account_id: accountId, session_id: session.id, epoch: session.epoch, target, view_generation: session.view_generation }
      : null

    const clearProjection = () => {
      clearTimeout(frameExpiryTimer)
      if (currentFrameUrl) {
        URL.revokeObjectURL(currentFrameUrl)
        currentFrameUrl = null
      }
      setFrameUrl(null)
      setFrameKind(null)
      setFocusedFieldRef(null)
      setInput('')
    }
    const failTransport = (nextMessage: string) => {
      if (disposed) return
      terminalError = true
      clearProjection()
      setTransport('error')
      setMessage(nextMessage)
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close()
    }
    const showFrame = (decoded: Extract<DecodedPortalFrame, { kind: 'pixel' }>) => {
      if (disposed) return
      if (currentFrameUrl) URL.revokeObjectURL(currentFrameUrl)
      currentFrameUrl = URL.createObjectURL(new Blob([new Uint8Array(decoded.bytes).buffer as ArrayBuffer], { type: decoded.pixel.mime_type }))
      setFrameUrl(currentFrameUrl)
      setFrameKind(decoded.pixel.region_kind)
      setFocusedFieldRef(decoded.pixel.focused_field_ref ?? null)
      clearTimeout(frameExpiryTimer)
      frameExpiryTimer = setTimeout(clearProjection, Math.max(0, Date.parse(decoded.pixel.expires_at) - Date.now()))
      setMessage(decoded.pixel.region_kind === 'qr' ? '当前二维码投影' : decoded.pixel.region_kind === 'form' ? '已批准的表单投影' : '已批准的门户投影')
    }
    const acceptIncoming = (decoded: DecodedPortalFrame | null): decoded is DecodedPortalFrame => {
      if (!decoded || disposed || !expectedBinding || !samePortalBinding(decoded.binding, expectedBinding) || decoded.sequence <= lastIncomingSequence) return false
      lastIncomingSequence = decoded.sequence
      return true
    }
    const handleDecoded = (decoded: DecodedPortalFrame | null) => {
      if (!acceptIncoming(decoded)) {
        failTransport('门户消息校验失败，已清除当前投影。')
        return
      }
      if (decoded.kind === 'control') {
        setFocusedFieldRef(decoded.control.focused_field_ref ?? decoded.control.field_ref ?? null)
        setMessage(decoded.control.kind === 'request_view' ? '已请求门户投影，等待当前批准的画面…' : '门户控制已确认。')
        return
      }
      showFrame(decoded)
    }
    const sendControl = (kind: PortalWireControl['kind'], value?: string, fieldRef?: string) => {
      if (disposed || !socket || socket.readyState !== WebSocket.OPEN) return
      const currentSequence = sequenceRef.current
      sequenceRef.current += 1
      const control: Record<string, unknown> = {
        kind,
        workspace_id: workspaceId,
        account_id: accountId,
        session_id: session.id,
        epoch: session.epoch,
        target,
        contract_version: 1,
        view_generation: session.view_generation,
        sequence: currentSequence,
        ...(fieldRef ? { field_ref: fieldRef } : {}),
        ...(value !== undefined ? { sensitive_payload: { value } } : {}),
      }
      const binding = {
        contract_version: 1,
        workspace_id: workspaceId,
        account_id: accountId,
        session_id: session.id,
        epoch: session.epoch,
        target,
        view_generation: session.view_generation,
      }
      socket.send(encodePortalControlFrame(control, binding, currentSequence, value))
    }
    sendControlRef.current = sendControl

    const connect = async () => {
      try {
        if (!expectedBinding) {
          failTransport('门户已阻止：当前会话缺少完整的标签页、框架、文档或来源绑定。')
          return
        }
        const issued = await issueBrowserPortalTicket(workspaceId, accountId, session.id, session.revision, makePortalCsrfToken())
        if (disposed) return
        const grant: PortalTicketGrant = await redeemBrowserPortalTicket(workspaceId, accountId, session.id, {
          contract_version: 1,
          first_entry: 'initial',
          expected_session_revision: issued.session_revision,
          ticket_id: issued.ticket_id,
          ticket: issued.ticket,
          csrf_token: issued.csrf_token,
        })
        if (disposed) return
        const websocketUrl = portalWebSocketUrl(grant.websocket_path)
        if (!websocketUrl) {
          failTransport('门户地址未通过同源安全校验，已拒绝连接。')
          return
        }
        socket = new WebSocket(websocketUrl)
        socket.binaryType = 'arraybuffer'
        socket.onopen = () => {
          if (disposed) return
          setTransport('connected')
          setMessage('门户已连接，等待批准的二维码或表单投影…')
          sendControl('request_view')
        }
        socket.onmessage = (event) => {
          if (disposed) return
          if (typeof event.data === 'string') {
            failTransport('门户发送了非约定的消息格式，已清除当前投影。')
            return
          }
          if (event.data instanceof ArrayBuffer) {
            handleDecoded(expectedBinding ? decodePortalBinaryFrame(event.data, expectedBinding) : null)
            return
          }
          failTransport('门户发送了不支持的消息类型，已清除当前投影。')
        }
        socket.onerror = () => {
          failTransport('门户传输失败，已清除当前投影；请重新打开同一会话授权。')
        }
        socket.onclose = () => {
          if (!disposed && !terminalError) {
            clearProjection()
            setTransport('closed')
            setMessage('门户已关闭，当前投影和输入已清除。')
          }
        }
      } catch {
        failTransport('门户授权或连接失败，已清除当前投影；请重新打开同一会话。')
      }
    }
    void connect()
    return () => {
      disposed = true
      socket?.close()
      sendControlRef.current = () => undefined
      clearProjection()
    }
  }, [accountId, session.document_id, session.epoch, session.frame_id, session.id, session.origin, session.revision, session.tab_id, session.view_generation, workspaceId])

  const sendInput = () => {
    if (!focusedFieldRef || !input) return
    sendControlRef.current('field_input', input, focusedFieldRef)
    setInput('')
  }

  return (
    <div className="space-y-3 rounded-md border border-primary/30 bg-primary/5 p-4" data-testid="browser-account-portal">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><p className="font-medium">同一会话门户</p><p className="mt-1 text-xs text-muted-foreground">在当前登录会话中查看二维码或填写表单，关闭后会立即清除画面和输入。</p></div>
        <div className="flex items-center gap-2"><Badge variant="outline"><Wifi className="mr-1 size-3" />{transport === 'connected' ? '已连接' : transport === 'opening' ? '连接中' : transport === 'closed' ? '已关闭' : '错误'}</Badge><Button size="icon-xs" variant="ghost" onClick={onClose} aria-label="关闭门户"><X className="size-3" /></Button></div>
      </div>
      <p role="status" className="text-xs text-muted-foreground">{message}</p>
      {frameUrl ? <div className="overflow-hidden rounded-md border bg-black p-2"><img src={frameUrl} alt={`${frameKind === 'qr' ? '二维码' : frameKind === 'form' ? '表单' : '已批准'}门户投影`} className="mx-auto max-h-96 max-w-full object-contain" /></div> : null}
      {focusedFieldRef ? <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); sendInput() }}><label className="min-w-56 flex-1 space-y-1 text-xs"><span>已批准的临时字段</span><Input type="password" autoComplete="off" value={input} onChange={(event) => setInput(event.target.value)} placeholder="输入仅保存在内存中，发送后立即清除" disabled={transport !== 'connected'} /></label><Button type="submit" size="sm" disabled={!input || transport !== 'connected'}>发送一次</Button></form> : null}
      <p className="text-3xs text-muted-foreground">缺少或存在歧义的投影会保持未知；界面不会根据二维码消失、HTTP 状态、URL 变化或 Cookie 推断登录成功。</p>
    </div>
  )
}

function SessionCard({ workspaceId, account, session, canOperate, onChanged }: { workspaceId: string; account: BrowserAccount; session: BrowserLoginSession; canOperate: boolean; onChanged: (session: BrowserLoginSession) => void }) {
  const [pending, setPending] = useState<'view' | 'takeover' | 'confirm' | 'close' | null>(null)
  const [portalSession, setPortalSession] = useState<BrowserLoginSession | null>(null)
  const run = async (action: 'view' | 'takeover' | 'confirm' | 'close') => {
    setPending(action)
    try {
      const next = await performBrowserSessionAction(workspaceId, account.id, session.id, action, session.revision, `browser-session-${action}-${session.id}-${session.revision}`)
      onChanged(next)
      if (action === 'view' || action === 'takeover') setPortalSession(next)
      if (action === 'close') setPortalSession(null)
      toast.success(action === 'view' || action === 'takeover' ? '登录窗口已打开' : `Session ${action} accepted`)
    } catch (error) {
      toast.error(errorText(error))
    } finally {
      setPending(null)
    }
  }
  const challenge = session.status === 'challenge' || session.status === 'unknown'
  if (!canOperate) {
    return <div className="rounded-md border p-4 text-xs text-muted-foreground">当前成员角色仅可读取会话状态，无法打开门户或执行会话操作。</div>
  }
  return (
    <div className="space-y-3 rounded-md border p-4" data-testid="browser-account-session">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">登录会话</p><p className="mt-1 font-mono text-xs text-muted-foreground">{session.id} · revision {session.revision}</p></div><AccountStatus status={session.status} /></div>
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Lease / epoch</p><p className="mt-1 font-mono text-xs">{session.lease_id ?? 'pending'} / {session.epoch}</p></div><div><p className="text-xs text-muted-foreground">Node / boot</p><p className="mt-1 truncate font-mono text-xs">{session.node_id ?? 'pending'} / {session.node_boot_id ?? 'pending'}</p></div><div><p className="text-xs text-muted-foreground">Target tab / frame</p><p className="mt-1 font-mono text-xs">{session.tab_id ?? 'pending'} / {session.frame_id ?? 'pending'}</p></div></div>
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Profile</p><p className="mt-1 font-mono text-xs">{session.profile_id ?? 'new'} · v{session.profile_version ?? '—'} · {session.profile_state}</p></div><div><p className="text-xs text-muted-foreground">View generation</p><p className="mt-1 font-mono text-xs">{session.view_generation}</p></div><div><p className="text-xs text-muted-foreground">Origin</p><p className="mt-1 truncate font-mono text-xs">{session.origin ?? 'not established'}</p></div></div>
      {challenge ? <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200"><ShieldAlert className="mt-0.5 size-4 shrink-0" /><p>请在当前会话中完成平台验证，核对身份后确认。</p></div> : null}
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('view')}>{pending === 'view' ? <Loader2 className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}打开登录窗口</Button>{challenge ? <Button size="sm" variant="outline" disabled={pending !== null} onClick={() => void run('takeover')}>{pending === 'takeover' ? <Loader2 className="size-4 animate-spin" /> : null}接管当前会话</Button> : null}{challenge ? <Button size="sm" variant="outline" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('confirm')}>{pending === 'confirm' ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}确认已核对身份</Button> : null}<Button size="sm" variant="ghost" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('close')}>关闭会话</Button></div>
      {portalSession && !['closed', 'expired', 'error'].includes(session.status) ? <PortalView workspaceId={workspaceId} accountId={account.id} session={session.id === portalSession.id ? session : portalSession} onClose={() => setPortalSession(null)} /> : null}
    </div>
  )
}

function AccountDetail({ workspaceId, account, canManage, canOperate, onRefresh }: { workspaceId: string; account: BrowserAccount; canManage: boolean; canOperate: boolean; onRefresh: () => void }) {
  const queryClient = useQueryClient()
  const [session, setSession] = useState<BrowserLoginSession | null>(null)
  const [sourceBindingRevisionId, setSourceBindingRevisionId] = useState('')
  const detailQuery = useQuery({ queryKey: ['browser-account', workspaceId, account.id], queryFn: () => getBrowserAccount(workspaceId, account.id), refetchInterval: 2_000 })
  const current = detailQuery.data ?? account
  const sessionQuery = useQuery({ queryKey: ['browser-login-session', workspaceId, current.id, session?.id], queryFn: () => getBrowserLoginSession(workspaceId, current.id, session?.id as string), enabled: Boolean(session?.id), refetchInterval: 2_000 })
  const login = useMutation({
    mutationFn: () => {
      if (!canOperate) throw new Error('当前成员角色无权打开登录会话')
      return createBrowserLoginSession(workspaceId, current.id, { purpose: 'login', expected_revision: current.revision, ...(sourceBindingRevisionId.trim() ? { source_binding_revision_id: sourceBindingRevisionId.trim() } : {}) }, `browser-login-${current.id}-${current.revision}`)
    },
    onSuccess: (next) => { setSession(next); void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, current.id] }); toast.success('登录会话已打开') },
    onError: (error) => toast.error(errorText(error)),
  })
  const accountOperation = useMutation({
    mutationFn: (operation: BrowserAccountOperationRequest['operation']) => {
      if (!canManage) throw new Error('当前成员角色无权修改浏览器账号')
      return operateBrowserAccount(workspaceId, current.id, { account_ref: { workspace_id: workspaceId, account_id: current.id }, expected_revision: current.revision, operation, ...(operation === 'auth_required' ? { auth_required: !current.auth_required } : {}) }, `browser-account-${operation}-${current.id}-${current.revision}`)
    },
    onSuccess: () => { onRefresh(); void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, current.id] }) },
    onError: (error) => toast.error(errorText(error)),
  })
  const effectiveSession = sessionQuery.data ?? session
  return (
    <section className="space-y-4" data-testid="browser-account-detail">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-lg font-semibold">{current.label}</p><p className="mt-1 font-mono text-xs text-muted-foreground">{current.site} · account_id {current.id}</p></div><div className="flex items-center gap-2"><AccountStatus status={current.status} /><span className="font-mono text-xs text-muted-foreground">revision {current.revision}</span></div></div>
      <EvidenceSummary account={current} />
      {current.status === 'challenge' || current.status === 'unknown' ? <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm"><p className="font-medium">需要人工处理</p><p className="mt-1 text-xs text-muted-foreground">当前账号尚未验证。请在同一会话中处理登录，核对平台身份后再确认。</p></div> : null}
      {current.status_reason_code ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">{current.status_reason_code}</p> : null}
      <div className="flex flex-wrap items-end gap-2 rounded-md border p-3"><label className="min-w-64 flex-1 space-y-1 text-sm"><span>绑定来源版本 <span className="text-muted-foreground">（可选）</span></span><Input value={sourceBindingRevisionId} onChange={(event) => setSourceBindingRevisionId(event.target.value)} placeholder="已固定版本 ID" disabled={!canOperate || login.isPending || current.status === 'closed'} /></label><Button disabled={!canOperate || login.isPending || current.status === 'closed'} onClick={() => login.mutate()}>{login.isPending ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}{login.isPending ? '正在打开…' : '打开登录会话'}</Button><Button variant="outline" disabled={!canManage || accountOperation.isPending || current.status === 'closed'} onClick={() => accountOperation.mutate(current.auth_required ? 'resume' : 'auth_required')}>{accountOperation.isPending ? <Loader2 className="size-4 animate-spin" /> : null}{current.auth_required ? '恢复账号' : '要求重新认证'}</Button></div>
      <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">登录验证和保存结果以服务端状态为准。保存失败会显示错误，请检查后处理。</div>
      {effectiveSession ? <SessionCard workspaceId={workspaceId} account={current} session={effectiveSession} canOperate={canOperate} onChanged={setSession} /> : null}
      {detailQuery.error ? <ErrorState message={errorText(detailQuery.error)} hint={BACKEND_HINT} /> : null}
    </section>
  )
}

export function BrowserAccountsPanel() {
  const { identity } = useAuth()
  const searchParams = useSearchParams()
  const workspaces = useMyWorkspaces()
  const workspaceId = searchParams.get('workspace') ?? workspaces.data?.[0]?.id ?? null
  const queryClient = useQueryClient()
  const membersQuery = useQuery({ queryKey: ['browser-workspace-members', workspaceId], queryFn: () => listBrowserWorkspaceMembers(workspaceId as string), enabled: Boolean(workspaceId), refetchInterval: 10_000 })
  const workspace = workspaces.data?.find((candidate) => candidate.id === workspaceId)
  const currentMember = membersQuery.data?.find((member) => member.subject === identity?.subject)
  const membershipKnown = membersQuery.isSuccess && (identity?.is_platform_admin === true || Boolean(currentMember))
  const hasBrowserAccountReadPermission = Boolean(workspace?.active && membershipKnown && !currentMember?.disabled)
  const canManageBrowserAccounts = hasBrowserAccountReadPermission && (identity?.is_platform_admin === true || currentMember?.role === 'admin' || currentMember?.role === 'maintainer')
  const canOperateBrowserAccounts = hasBrowserAccountReadPermission && (identity?.is_platform_admin === true || currentMember?.role === 'admin' || currentMember?.role === 'maintainer' || currentMember?.role === 'operator')
  const accountsQuery = useInfiniteQuery({
    queryKey: ['browser-accounts', workspaceId, 'pages'],
    queryFn: ({ pageParam }) => listBrowserAccounts(workspaceId as string, { cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: hasBrowserAccountReadPermission,
    refetchInterval: 5_000,
  })
  const accounts = useMemo(() => accountsQuery.data?.pages.flatMap((page) => page.items) ?? [], [accountsQuery.data])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = selectedId ? accounts.find((account) => account.id === selectedId) ?? null : null
  useEffect(() => { setSelectedId(null) }, [workspaceId])
  useEffect(() => { if (!selectedId && accounts[0]) setSelectedId(accounts[0].id); if (selectedId && !accounts.some((account) => account.id === selectedId)) setSelectedId(accounts[0]?.id ?? null) }, [accounts, selectedId])
  if (!hasBrowserAccountReadPermission) {
    return (
      <Card className="overflow-hidden py-0">
        <CardHeader className="border-b bg-muted/20 py-4"><CardTitle className="text-base">浏览器账号</CardTitle><CardDescription>账号、登录会话和门户投影都必须在工作区读取权限确认后才能显示。</CardDescription></CardHeader>
        <CardContent>{workspaces.isLoading || membersQuery.isLoading ? <LoadingState /> : membersQuery.error ? <ErrorState message="无法读取工作区权限，请重试。" hint={BACKEND_HINT} /> : <EmptyState title={workspaceId ? '无权访问此工作区' : '尚无可用工作区'} description={workspaceId ? '请切换到有访问权限的工作区，或联系管理员。' : '创建或加入工作区后即可管理浏览器账号。'} />}</CardContent>
      </Card>
    )
  }
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['browser-accounts', workspaceId] }); if (selected) void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, selected.id] }) }
  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="border-b bg-muted/20 py-4"><CardTitle className="text-base">工作区浏览器账号</CardTitle><CardDescription>每个账号拥有独立的认证资料和登录会话；账号认证状态与资源分配分别展示；资源已分配不代表节点健康。</CardDescription><CardAction><Badge variant="outline">{identity?.subject ? '操作员会话' : '工作区上下文'}</Badge></CardAction></CardHeader>
      <CardContent className="grid gap-6 p-4 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.2fr)]">
        <section className="space-y-4" aria-labelledby="browser-account-create-title"><div><h3 id="browser-account-create-title" className="font-medium">添加账号</h3><p className="mt-1 text-xs text-muted-foreground">普通登录会开启一个真实会话；二维码刷新和原生表单只会在已批准的会话中展示。</p></div><CreateAccountForm workspaceId={workspaceId} canManage={canManageBrowserAccounts} onCreated={(account) => { setSelectedId(account.id); refresh() }} /><div className="border-t pt-4"><div className="flex items-center justify-between gap-2"><h3 className="font-medium">账号</h3><Button size="xs" variant="ghost" onClick={refresh} disabled={accountsQuery.isFetching}><RefreshCw className={accountsQuery.isFetching ? 'size-3 animate-spin' : 'size-3'} /></Button></div>{accountsQuery.isLoading ? <LoadingState /> : accountsQuery.error ? <ErrorState message={errorText(accountsQuery.error)} hint={BACKEND_HINT} /> : accounts.length === 0 ? <EmptyState title="暂无浏览器账号" description="创建工作区账号后即可开始已验证的登录会话。" /> : <div className="mt-2 space-y-2">{accounts.map((account) => <button key={account.id} type="button" onClick={() => setSelectedId(account.id)} className={`w-full rounded-md border p-3 text-left transition-colors ${selected?.id === account.id ? 'border-primary bg-primary/5' : 'hover:bg-muted/40'}`}><div className="flex items-center justify-between gap-2"><span className="truncate font-medium">{account.label}</span><AccountStatus status={account.status} /></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.site} · {account.id}</p><p className="mt-1 text-xs text-muted-foreground">{account.evidence_source ?? '认证凭据未知'} · revision {account.revision}</p></button>)}</div>}{accountsQuery.hasNextPage ? <Button variant="outline" size="sm" disabled={accountsQuery.isFetchingNextPage} onClick={() => void accountsQuery.fetchNextPage()}>{accountsQuery.isFetchingNextPage ? "加载中…" : "加载更多账号"}</Button> : null}</div></section>
        <section className="min-w-0" aria-labelledby="browser-account-detail-title"><h3 id="browser-account-detail-title" className="sr-only">所选账号详情</h3>{!workspaceId ? <EmptyState title="选择工作区" description="账号操作需要明确的工作区范围。" /> : !selected ? <EmptyState title="选择账号" description="创建或选择工作区账号以查看认证凭据和登录会话。" /> : <AccountDetail key={`${workspaceId}:${selected.id}`} workspaceId={workspaceId} account={selected} canManage={canManageBrowserAccounts} canOperate={canOperateBrowserAccounts} onRefresh={refresh} />}</section>
      </CardContent>
    </Card>
  )
}
