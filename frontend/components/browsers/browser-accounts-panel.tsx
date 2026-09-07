'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
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
  opening: 'Opening',
  presenting: 'Presenting',
  refreshing: 'Refreshing',
  verifying: 'Verifying',
  challenge: 'Challenge',
  unknown: 'Unknown',
  saving: 'Saving',
  saved: 'Saved',
  dormant: 'Dormant',
  expired: 'Expired',
  closed: 'Closed',
  error: 'Error',
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
  return error instanceof Error ? error.message : 'Browser account operation failed'
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
        <p className="text-xs text-muted-foreground">Auth evidence</p>
        <p className="mt-1 font-medium">{evidence}</p>
        <p className="mt-1 text-xs text-muted-foreground">{account.evidence_source ?? 'not reported'}</p>
      </div>
      <div className="rounded-md border p-3">
        <p className="text-xs text-muted-foreground">Platform identity</p>
        <p className="mt-1 truncate font-medium">{account.platform_identity?.display_name ?? 'Not verified'}</p>
        <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.platform_identity?.provider ?? 'identity unavailable'}</p>
      </div>
      <div className="rounded-md border p-3">
        <p className="text-xs text-muted-foreground">Runtime health</p>
        <p className="mt-1 truncate font-mono text-xs">{account.runtime_bundle_id ?? 'Not assigned'}</p>
        <p className="mt-1 text-xs text-muted-foreground">{account.node_id ? `Node ${account.node_id}` : 'Node pending'}</p>
      </div>
    </div>
  )
}

function CreateAccountForm({ workspaceId, onCreated }: { workspaceId: string | null; onCreated: (account: BrowserAccount) => void }) {
  const [site, setSite] = useState('')
  const [label, setLabel] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [runtimeBundleId, setRuntimeBundleId] = useState('')
  const [loginRuleId, setLoginRuleId] = useState('')
  const [loginRuleVersion, setLoginRuleVersion] = useState('')
  const mutation = useMutation({
    mutationFn: () => {
      if (!workspaceId) throw new Error('Select a workspace first')
      const cleanSite = site.trim()
      const cleanLabel = label.trim()
      if (!cleanSite || !cleanLabel) throw new Error('Site and account label are required')
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
      toast.success('Browser account created')
    },
    onError: (error) => toast.error(errorText(error)),
  })
  return (
    <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span>Platform / site</span><Input value={site} onChange={(event) => setSite(event.target.value)} placeholder="example.com" disabled={!workspaceId || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>Account label</span><Input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Research account" disabled={!workspaceId || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>Owning node ID <span className="text-muted-foreground">(optional)</span></span><Input value={nodeId} onChange={(event) => setNodeId(event.target.value)} placeholder="opaque node id" disabled={!workspaceId || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>Runtime bundle ID <span className="text-muted-foreground">(optional)</span></span><Input value={runtimeBundleId} onChange={(event) => setRuntimeBundleId(event.target.value)} placeholder="verified bundle id" disabled={!workspaceId || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>Login rule ID <span className="text-muted-foreground">(optional)</span></span><Input value={loginRuleId} onChange={(event) => setLoginRuleId(event.target.value)} placeholder="verified rule id" disabled={!workspaceId || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>Login rule version <span className="text-muted-foreground">(optional)</span></span><Input value={loginRuleVersion} onChange={(event) => setLoginRuleVersion(event.target.value)} placeholder="v1" disabled={!workspaceId || mutation.isPending} /></label>
      </div>
      <p className="text-xs text-muted-foreground">Credentials, QR contents, and transient form input stay inside the approved login portal and are never stored here.</p>
      <Button type="submit" disabled={!workspaceId || mutation.isPending}>{mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}{mutation.isPending ? 'Opening account...' : 'Add browser account'}</Button>
    </form>
  )
}

type PortalWireControl = {
  kind?: 'field_input' | 'pointer' | 'key' | 'request_view' | 'takeover'
  field_ref?: string
  focused_field_ref?: string
}

type PortalWireMessage = {
  encoding?: 'control-json' | 'pixel-binary'
  transient?: { control?: PortalWireControl; pixel?: PortalWirePixel }
  control?: PortalWireControl
  pixel?: PortalWirePixel
}

function makePortalCsrfToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function sessionTarget(session: BrowserLoginSession) {
  if (!session.tab_id || !session.frame_id || !session.document_id || !session.origin) return null
  return {
    tab_id: session.tab_id,
    frame_id: session.frame_id,
    document_id: session.document_id,
    origin: session.origin,
  }
}

function portalWebSocketUrl(path: string): string {
  if (path.startsWith('ws://') || path.startsWith('wss://')) return path
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path.startsWith('/') ? path : `/${path}`}`
}

function decodeBase64(value: string): Uint8Array | null {
  try {
    const decoded = atob(value)
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0))
  } catch {
    return null
  }
}

type DecodedPortalFrame =
  | { kind: 'control'; control: PortalWireControl }
  | { kind: 'pixel'; bytes: Uint8Array; mimeType: 'image/png' | 'image/jpeg' | 'image/webp'; regionKind: 'qr' | 'form' | 'approved' }

function decodePortalBinaryFrame(data: ArrayBuffer): DecodedPortalFrame | null {
  const bytes = new Uint8Array(data)
  if (bytes.byteLength < 16 || new TextDecoder().decode(bytes.slice(0, 4)) !== 'Q2P1') return null
  const view = new DataView(data)
  if (view.getUint8(4) !== 1 || view.getUint8(6) !== 0 || view.getUint8(7) !== 0 || view.getUint16(14) !== 0) return null
  const encoding = view.getUint8(5)
  const metadataBytes = view.getUint16(8)
  const payloadBytes = view.getUint32(10)
  if (!metadataBytes || 16 + metadataBytes + payloadBytes !== bytes.byteLength) return null
  try {
    const envelope = JSON.parse(new TextDecoder().decode(bytes.slice(16, 16 + metadataBytes))) as {
      encoding?: string
      message?: PortalWireControl & PortalWirePixel
    }
    if (envelope.encoding === 'control-json' && encoding === 0 && envelope.message) {
      return { kind: 'control', control: envelope.message }
    }
    if (envelope.encoding !== 'pixel-binary' || encoding !== 1 || !envelope.message?.mime_type || !envelope.message.region_kind) return null
    const payload = bytes.slice(16 + metadataBytes)
    if (payload.byteLength !== payloadBytes || envelope.message.byte_length !== payloadBytes) return null
    return {
      kind: 'pixel',
      bytes: payload,
      mimeType: envelope.message.mime_type,
      regionKind: envelope.message.region_kind,
    }
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
  const [message, setMessage] = useState('Opening same-session portal…')
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
    const target = sessionTarget(session)
    if (!target) {
      setTransport('error')
      setMessage('Portal blocked: the server has not bound a complete tab/frame/document/origin target.')
      return () => undefined
    }

    const showFrame = (bytes: Uint8Array, mimeType: string, kind: 'qr' | 'form' | 'approved') => {
      if (disposed) return
      if (currentFrameUrl) URL.revokeObjectURL(currentFrameUrl)
      currentFrameUrl = URL.createObjectURL(new Blob([bytes], { type: mimeType }))
      setFrameUrl(currentFrameUrl)
      setFrameKind(kind)
      setMessage(kind === 'qr' ? 'Current QR projection' : kind === 'form' ? 'Approved native form projection' : 'Approved portal projection')
    }

    const handleJsonMessage = (value: string) => {
      let parsed: PortalWireMessage
      try {
        parsed = JSON.parse(value) as PortalWireMessage
      } catch {
        setMessage('Portal sent an invalid control frame; no input was sent.')
        return
      }
      const control = parsed.transient?.control ?? parsed.control
      const pixel = parsed.transient?.pixel ?? parsed.pixel
      const nextField = control?.focused_field_ref ?? control?.field_ref ?? pixel?.focused_field_ref
      if (nextField) setFocusedFieldRef(nextField)
      if (!pixel?.frame_bytes) {
        if (control?.kind === 'request_view') setMessage('Portal view requested; waiting for the current approved projection…')
        else setMessage('Portal control acknowledged; waiting for the current approved projection…')
        return
      }
      const bytes = decodeBase64(pixel.frame_bytes)
      if (!bytes || !pixel.mime_type || (pixel.byte_length !== undefined && bytes.byteLength !== pixel.byte_length)) {
        setMessage('Portal frame failed its declared byte-length or encoding check.')
        return
      }
      showFrame(bytes, pixel.mime_type, pixel.region_kind ?? 'approved')
    }
    const sendControl = (kind: PortalWireControl['kind'], value?: string, fieldRef?: string) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return
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
        socket = new WebSocket(portalWebSocketUrl(grant.websocket_path))
        socket.binaryType = 'arraybuffer'
        socket.onopen = () => {
          setTransport('connected')
          setMessage('Portal connected; waiting for an approved QR/form projection…')
          sendControl('request_view')
        }
        socket.onmessage = (event) => {
          if (typeof event.data === 'string') {
            handleJsonMessage(event.data)
            return
          }
          if (event.data instanceof ArrayBuffer) {
            const decoded = decodePortalBinaryFrame(event.data)
            if (!decoded) {
              setMessage('Portal sent an invalid Q2P1 frame; it was not displayed.')
              return
            }
            if (decoded.kind === 'control') {
              const nextField = decoded.control.focused_field_ref ?? decoded.control.field_ref
              if (nextField) setFocusedFieldRef(nextField)
              setMessage(decoded.control.kind === 'request_view' ? 'Portal view requested; waiting for the current approved projection…' : 'Portal control acknowledged.')
              return
            }
            showFrame(decoded.bytes, decoded.mimeType, decoded.regionKind)
            return
          }
          setMessage('Portal sent an unsupported frame type; it was not displayed.')
        }
        socket.onerror = () => {
          if (!disposed) {
            setTransport('error')
            setMessage('Portal transport failed; re-open the same session to re-authorize.')
          }
        }
        socket.onclose = () => {
          if (!disposed) {
            setTransport('closed')
            setMessage('Portal closed; no stale frame or input is retained.')
          }
        }
      }
    }
    void connect()
    return () => {
      disposed = true
      socket?.close()
      sendControlRef.current = () => undefined
      if (currentFrameUrl) URL.revokeObjectURL(currentFrameUrl)
    }
  }, [accountId, session.id, session.revision, workspaceId])

  const sendInput = () => {
    if (!focusedFieldRef || !input) return
    // The field ref comes from the server's approved focus projection; arbitrary
    // capability names and endpoints never enter this transient input path.
    sendControlRef.current('field_input', input, focusedFieldRef)
    setInput('')
  }

  return (
    <div className="space-y-3 rounded-md border border-primary/30 bg-primary/5 p-4" data-testid="browser-account-portal">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><p className="font-medium">Same-session portal</p><p className="mt-1 text-xs text-muted-foreground">Ticket redeemed in the response body; HttpOnly cookie and CSRF binding remain server-owned.</p></div>
        <div className="flex items-center gap-2"><Badge variant="outline"><Wifi className="mr-1 size-3" />{transport}</Badge><Button size="icon-xs" variant="ghost" onClick={onClose} aria-label="Close portal"><X className="size-3" /></Button></div>
      </div>
      <p role="status" className="text-xs text-muted-foreground">{message}</p>
      {frameUrl ? <div className="overflow-hidden rounded-md border bg-black p-2"><img src={frameUrl} alt={`${frameKind ?? 'approved'} portal projection`} className="mx-auto max-h-96 max-w-full object-contain" /></div> : null}
      {focusedFieldRef ? <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); sendInput() }}><label className="min-w-56 flex-1 space-y-1 text-xs"><span>Approved transient field</span><Input type="password" autoComplete="off" value={input} onChange={(event) => setInput(event.target.value)} placeholder="Input stays in memory and clears on send" disabled={transport !== 'connected'} /></label><Button type="submit" size="sm" disabled={!input || transport !== 'connected'}>Send once</Button></form> : null}
      <p className="text-3xs text-muted-foreground">A missing/ambiguous projection remains unknown. The UI does not infer login success from QR disappearance, HTTP status, URL changes, or cookies.</p>
    </div>
  )
}

function SessionCard({ workspaceId, account, session, onChanged }: { workspaceId: string; account: BrowserAccount; session: BrowserLoginSession; onChanged: (session: BrowserLoginSession) => void }) {
  const [pending, setPending] = useState<'view' | 'takeover' | 'confirm' | 'close' | null>(null)
  const [portalSession, setPortalSession] = useState<BrowserLoginSession | null>(null)
  const run = async (action: 'view' | 'takeover' | 'confirm' | 'close') => {
    setPending(action)
    try {
      const next = await performBrowserSessionAction(workspaceId, account.id, session.id, action, session.revision, `browser-session-${action}-${session.id}-${session.revision}`)
      onChanged(next)
      if (action === 'view' || action === 'takeover') setPortalSession(next)
      if (action === 'close') setPortalSession(null)
      toast.success(action === 'view' || action === 'takeover' ? 'Same-session portal authorized' : `Session ${action} accepted`)
    } catch (error) {
      toast.error(errorText(error))
    } finally {
      setPending(null)
    }
  }
  const challenge = session.status === 'challenge' || session.status === 'unknown'
  return (
    <div className="space-y-3 rounded-md border p-4" data-testid="browser-account-session">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">Login session</p><p className="mt-1 font-mono text-xs text-muted-foreground">{session.id} · revision {session.revision}</p></div><AccountStatus status={session.status} /></div>
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Lease / epoch</p><p className="mt-1 font-mono text-xs">{session.lease_id ?? 'pending'} / {session.epoch}</p></div><div><p className="text-xs text-muted-foreground">Node / boot</p><p className="mt-1 truncate font-mono text-xs">{session.node_id ?? 'pending'} / {session.node_boot_id ?? 'pending'}</p></div><div><p className="text-xs text-muted-foreground">Target tab / frame</p><p className="mt-1 font-mono text-xs">{session.tab_id ?? 'pending'} / {session.frame_id ?? 'pending'}</p></div></div>
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Profile</p><p className="mt-1 font-mono text-xs">{session.profile_id ?? 'new'} · v{session.profile_version ?? '—'} · {session.profile_state}</p></div><div><p className="text-xs text-muted-foreground">View generation</p><p className="mt-1 font-mono text-xs">{session.view_generation}</p></div><div><p className="text-xs text-muted-foreground">Origin</p><p className="mt-1 truncate font-mono text-xs">{session.origin ?? 'not established'}</p></div></div>
      {challenge ? <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200"><ShieldAlert className="mt-0.5 size-4 shrink-0" /><p>Challenge or unknown state stays in this same session. Take over the approved portal, then confirm the observed identity; no new session or account is guessed.</p></div> : null}
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('view')}>{pending === 'view' ? <Loader2 className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}Open portal</Button>{challenge ? <Button size="sm" variant="outline" disabled={pending !== null} onClick={() => void run('takeover')}>{pending === 'takeover' ? <Loader2 className="size-4 animate-spin" /> : null}Take over session</Button> : null}{challenge ? <Button size="sm" variant="outline" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('confirm')}>{pending === 'confirm' ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}Confirm observed identity</Button> : null}<Button size="sm" variant="ghost" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('close')}>Close session</Button></div>
      {portalSession ? <PortalView workspaceId={workspaceId} accountId={account.id} session={portalSession} onClose={() => setPortalSession(null)} /> : null}
    </div>
  )
}

function AccountDetail({ workspaceId, account, onRefresh }: { workspaceId: string; account: BrowserAccount; onRefresh: () => void }) {
  const queryClient = useQueryClient()
  const [session, setSession] = useState<BrowserLoginSession | null>(null)
  const [sourceBindingRevisionId, setSourceBindingRevisionId] = useState('')
  const detailQuery = useQuery({ queryKey: ['browser-account', workspaceId, account.id], queryFn: () => getBrowserAccount(workspaceId, account.id), refetchInterval: 2_000 })
  const current = detailQuery.data ?? account
  const sessionQuery = useQuery({ queryKey: ['browser-login-session', workspaceId, current.id, session?.id], queryFn: () => getBrowserLoginSession(workspaceId, current.id, session?.id as string), enabled: Boolean(session?.id), refetchInterval: 2_000 })
  const login = useMutation({
    mutationFn: () => createBrowserLoginSession(workspaceId, current.id, { purpose: 'login', expected_revision: current.revision, ...(sourceBindingRevisionId.trim() ? { source_binding_revision_id: sourceBindingRevisionId.trim() } : {}) }, `browser-login-${current.id}-${current.revision}`),
    onSuccess: (next) => { setSession(next); void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, current.id] }); toast.success('Login session opened') },
    onError: (error) => toast.error(errorText(error)),
  })
  const accountOperation = useMutation({
    mutationFn: (operation: BrowserAccountOperationRequest['operation']) => operateBrowserAccount(workspaceId, current.id, { account_ref: { workspace_id: workspaceId, account_id: current.id }, expected_revision: current.revision, operation, ...(operation === 'auth_required' ? { auth_required: !current.auth_required } : {}) }, `browser-account-${operation}-${current.id}-${current.revision}`),
    onSuccess: () => { onRefresh(); void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, current.id] }) },
    onError: (error) => toast.error(errorText(error)),
  })
  const effectiveSession = sessionQuery.data ?? session
  return (
    <section className="space-y-4" data-testid="browser-account-detail">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-lg font-semibold">{current.label}</p><p className="mt-1 font-mono text-xs text-muted-foreground">{current.site} · account_id {current.id}</p></div><div className="flex items-center gap-2"><AccountStatus status={current.status} /><span className="font-mono text-xs text-muted-foreground">revision {current.revision}</span></div></div>
      <EvidenceSummary account={current} />
      {current.status === 'challenge' || current.status === 'unknown' ? <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm"><p className="font-medium">Manual attention required</p><p className="mt-1 text-xs text-muted-foreground">The account is not treated as verified. Continue in the same session and confirm only after platform identity and page origin agree.</p></div> : null}
      {current.status_reason_code ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">{current.status_reason_code}</p> : null}
      <div className="flex flex-wrap items-end gap-2 rounded-md border p-3"><label className="min-w-64 flex-1 space-y-1 text-sm"><span>Source Binding Revision <span className="text-muted-foreground">(optional)</span></span><Input value={sourceBindingRevisionId} onChange={(event) => setSourceBindingRevisionId(event.target.value)} placeholder="Pinned revision id" disabled={login.isPending || current.status === 'closed'} /></label><Button disabled={login.isPending || current.status === 'closed'} onClick={() => login.mutate()}>{login.isPending ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}{login.isPending ? 'Opening...' : 'Open login session'}</Button><Button variant="outline" disabled={accountOperation.isPending || current.status === 'closed'} onClick={() => accountOperation.mutate(current.auth_required ? 'resume' : 'auth_required')}>{accountOperation.isPending ? <Loader2 className="size-4 animate-spin" /> : null}{current.auth_required ? 'Resume account' : 'Require re-auth'}</Button></div>
      <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">Lifecycle is server-owned: opening → presenting → refreshing/verifying → saving → saved/dormant. A failed save remains visible as saving/error and never reports success or retries transient input.</div>
      {effectiveSession ? <SessionCard workspaceId={workspaceId} account={current} session={effectiveSession} onChanged={setSession} /> : null}
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
  const accountsQuery = useQuery({ queryKey: ['browser-accounts', workspaceId], queryFn: () => listBrowserAccounts(workspaceId as string), enabled: Boolean(workspaceId), refetchInterval: 5_000 })
  const accounts = useMemo(() => accountsQuery.data?.items ?? [], [accountsQuery.data])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = accounts.find((account) => account.id === selectedId) ?? accounts[0] ?? null
  useEffect(() => { if (!selectedId && accounts[0]) setSelectedId(accounts[0].id); if (selectedId && !accounts.some((account) => account.id === selectedId)) setSelectedId(accounts[0]?.id ?? null) }, [accounts, selectedId])
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['browser-accounts', workspaceId] }); if (selected) void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, selected.id] }) }
  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="border-b bg-muted/20 py-4"><CardTitle className="text-base">Workspace browser accounts</CardTitle><CardDescription>Each account owns its verified profile and login session. Account identity, evidence, and runtime health remain separate signals.</CardDescription><CardAction><Badge variant="outline">{identity?.subject ? 'Operator session' : 'Workspace context'}</Badge></CardAction></CardHeader>
      <CardContent className="grid gap-6 p-4 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.2fr)]">
        <section className="space-y-4" aria-labelledby="browser-account-create-title"><div><h3 id="browser-account-create-title" className="font-medium">Add account</h3><p className="mt-1 text-xs text-muted-foreground">A normal login starts one real session. QR refresh and native forms are presented only inside that approved session.</p></div><CreateAccountForm workspaceId={workspaceId} onCreated={(account) => { setSelectedId(account.id); refresh() }} /><div className="border-t pt-4"><div className="flex items-center justify-between gap-2"><h3 className="font-medium">Accounts</h3><Button size="xs" variant="ghost" onClick={refresh} disabled={accountsQuery.isFetching}><RefreshCw className={accountsQuery.isFetching ? 'size-3 animate-spin' : 'size-3'} /></Button></div>{accountsQuery.isLoading ? <LoadingState /> : accountsQuery.error ? <ErrorState message={errorText(accountsQuery.error)} hint={BACKEND_HINT} /> : accounts.length === 0 ? <EmptyState title="No browser accounts" description="Create a workspace-scoped account to begin a verified login session." /> : <div className="mt-2 space-y-2">{accounts.map((account) => <button key={account.id} type="button" onClick={() => setSelectedId(account.id)} className={`w-full rounded-md border p-3 text-left transition-colors ${selected?.id === account.id ? 'border-primary bg-primary/5' : 'hover:bg-muted/40'}`}><div className="flex items-center justify-between gap-2"><span className="truncate font-medium">{account.label}</span><AccountStatus status={account.status} /></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.site} · {account.id}</p><p className="mt-1 text-xs text-muted-foreground">{account.evidence_source ?? 'evidence unknown'} · revision {account.revision}</p></button>)}</div>}{accountsQuery.data?.next_cursor ? <p className="text-3xs text-muted-foreground">More accounts are available through the keyset cursor; this view intentionally does not issue an unbounded scan.</p> : null}</div></section>
        <section className="min-w-0" aria-labelledby="browser-account-detail-title"><h3 id="browser-account-detail-title" className="sr-only">Selected account detail</h3>{!workspaceId ? <EmptyState title="Select a workspace" description="Account operations require an explicit workspace scope." /> : !selected ? <EmptyState title="Select an account" description="Create or select a workspace account to inspect its evidence and session." /> : <AccountDetail workspaceId={workspaceId} account={selected} onRefresh={refresh} />}</section>
      </CardContent>
    </Card>
  )
}
