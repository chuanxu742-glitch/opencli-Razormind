import { useEffect, useRef, useState } from 'react'

import {
  issueBrowserPortalTicket,
  redeemBrowserPortalTicket,
  type BrowserLoginSession,
  type PortalTicketGrant,
} from '@/lib/api/browser-accounts'
import {
  decodePortalBinaryFrame,
  encodePortalControlFrame,
  makePortalCsrfToken,
  isNewPortalSequence,
  samePortalBinding,
  sessionTarget,
  portalWebSocketUrl,
  isPortalControlKey,
  type DecodedPortalFrame,
  type PortalBinding,
  type PortalWireControl,
  type PortalClip,
  type PortalPointerAction,
  type PortalSensitivePayload,
} from '@/lib/browser-accounts/portal-protocol'

export type BrowserAccountPortalTransport = 'opening' | 'connected' | 'closed' | 'error'
export type BrowserAccountPortalFrameKind = 'qr' | 'form' | 'approved' | null

export type BrowserAccountPortalState = {
  transport: BrowserAccountPortalTransport
  message: string
  frameUrl: string | null
  frameKind: BrowserAccountPortalFrameKind
  focusedFieldRef: string | null
  input: string
  setInput: (value: string) => void
  sendInput: () => void
  frameClip: PortalClip | null
  requestTakeover: () => void
  sendPointer: (action: PortalPointerAction, x: number, y: number) => void
  sendKey: (key: string) => void
}


export function useBrowserAccountPortal({
  workspaceId,
  accountId,
  session,
}: {
  workspaceId: string
  accountId: string
  session: BrowserLoginSession
}): BrowserAccountPortalState {
  const [transport, setTransport] = useState<BrowserAccountPortalTransport>('opening')
  const [message, setMessage] = useState('正在打开同一会话门户…')
  const [frameUrl, setFrameUrl] = useState<string | null>(null)
  const [frameKind, setFrameKind] = useState<BrowserAccountPortalFrameKind>(null)
  const [focusedFieldRef, setFocusedFieldRef] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [connectionAttempt, setConnectionAttempt] = useState(0)
  const [frameClip, setFrameClip] = useState<PortalClip | null>(null)
  const interactiveFrameRef = useRef<{ clip: PortalClip; expiresAt: number } | null>(null)
  const sequenceRef = useRef(1)
  const sendControlRef = useRef<(kind: PortalWireControl['kind'], value?: string, fieldRef?: string, payload?: PortalSensitivePayload) => void>(() => undefined)

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let currentFrameUrl: string | null = null
    let lastIncomingSequence = 0
    let terminalError = false
    let frameExpiryTimer: ReturnType<typeof setTimeout> | undefined
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined
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
      setFrameClip(null)
      interactiveFrameRef.current = null
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
      setFrameClip(decoded.pixel.clip)
      interactiveFrameRef.current = decoded.pixel.region_kind === 'qr' ? null : { clip: decoded.pixel.clip, expiresAt: Date.parse(decoded.pixel.expires_at) }
      setFocusedFieldRef(decoded.pixel.focused_field_ref ?? null)
      clearTimeout(frameExpiryTimer)
      const expiresAt = Date.parse(decoded.pixel.expires_at)
      const remaining = Math.max(0, expiresAt - Date.now())
      frameExpiryTimer = setTimeout(() => {
        if (disposed) return
        sendControl('request_view')
        frameExpiryTimer = setTimeout(clearProjection, Math.max(0, expiresAt - Date.now()))
      }, Math.min(2000, remaining / 2))
      setMessage(decoded.pixel.region_kind === 'qr' ? '当前二维码投影' : decoded.pixel.region_kind === 'form' ? '已批准的表单投影' : '已批准的门户投影')
    }
    const acceptIncoming = (decoded: DecodedPortalFrame | null): decoded is DecodedPortalFrame => {
      if (!decoded || disposed || !expectedBinding || !samePortalBinding(decoded.binding, expectedBinding) || !isNewPortalSequence(decoded.sequence, lastIncomingSequence)) return false
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
    const sendControl = (kind: PortalWireControl['kind'], value?: string, fieldRef?: string, payload?: PortalSensitivePayload) => {
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
        ...(payload ? { sensitive_payload: payload } : {}),
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
        const websocketUrl = portalWebSocketUrl(grant.websocket_path, typeof window === 'undefined' ? null : window.location)
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
        socket.onclose = (event) => {
          if (!disposed && !terminalError) {
            clearProjection()
            setTransport('closed')
            setMessage('门户已关闭，当前投影和输入已清除。')
            if (event.code === 1000 && event.reason === 'Portal route expired') {
              setMessage('正在更新二维码画面授权…')
              reconnectTimer = setTimeout(() => {
                if (!disposed) setConnectionAttempt((attempt) => attempt + 1)
              }, 500)
            }
          }
        }
      } catch (error) {
        const status = error && typeof error === 'object' && 'status' in error ? error.status : null
        failTransport(status === 403
          ? '登录画面授权被拒绝（403），请检查门户来源配置和账号权限。'
          : status === 409
            ? '登录会话已更新，请重新打开登录画面。'
            : '门户授权或连接失败，已清除当前投影；请重新打开同一会话。')
      }
    }
    void connect()
    return () => {
      disposed = true
      clearTimeout(reconnectTimer)
      socket?.close()
      sendControlRef.current = () => undefined
      clearProjection()
    }
  }, [accountId, connectionAttempt, session.document_id, session.epoch, session.frame_id, session.id, session.origin, session.revision, session.tab_id, session.view_generation, workspaceId])

  const sendInput = () => {
    if (!focusedFieldRef || !input) return
    sendControlRef.current('field_input', input, focusedFieldRef)
    setInput('')
  }

  const sendPointer = (action: PortalPointerAction, x: number, y: number) => {
    const frame = interactiveFrameRef.current
    if (!frame || frame.expiresAt <= Date.now() || !Number.isSafeInteger(x) || !Number.isSafeInteger(y)) return
    if (x < frame.clip.x || y < frame.clip.y || x >= frame.clip.x + frame.clip.width || y >= frame.clip.y + frame.clip.height) return
    sendControlRef.current('pointer', undefined, undefined, { x, y, pointer_action: action })
  }
  const sendKey = (key: string) => {
    const frame = interactiveFrameRef.current
    if (!frame || frame.expiresAt <= Date.now() || !isPortalControlKey(key)) return
    sendControlRef.current('key', undefined, undefined, { key })
  }
  const requestTakeover = () => sendControlRef.current('takeover')

  return { transport, message, frameUrl, frameKind, focusedFieldRef, input, setInput, sendInput, frameClip, requestTakeover, sendPointer, sendKey }
}
