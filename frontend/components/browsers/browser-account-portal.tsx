'use client'

import { Wifi, X } from 'lucide-react'
import { useRef, type PointerEvent } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useBrowserAccountPortal } from '@/hooks/use-browser-account-portal'
import type { BrowserLoginSession } from '@/lib/api/browser-accounts'
import { isPortalControlKey, portalPointerCoordinates, type PortalPointerAction } from '@/lib/browser-accounts/portal-protocol'

export function BrowserAccountPortal({ workspaceId, accountId, session, onClose }: { workspaceId: string; accountId: string; session: BrowserLoginSession; onClose: () => void }) {
  const {
    transport,
    message,
    frameUrl,
    frameKind,
    focusedFieldRef,
    input,
    setInput,
    sendInput,
    frameClip,
    requestTakeover,
    sendPointer,
    sendKey,
  } = useBrowserAccountPortal({ workspaceId, accountId, session })
  const activePointer = useRef<number | null>(null)
  const interactive = transport === 'connected' && frameClip !== null && (frameKind === 'form' || frameKind === 'approved')
  const forwardPointer = (event: PointerEvent<HTMLImageElement>, action: PortalPointerAction) => {
    if (!interactive || !frameClip) return
    if (action === 'down') {
      if (!event.isPrimary || event.button !== 0 || activePointer.current !== null) return
      activePointer.current = event.pointerId
      event.currentTarget.focus()
      event.currentTarget.setPointerCapture(event.pointerId)
    } else if (activePointer.current !== event.pointerId) return
    event.preventDefault()
    const point = portalPointerCoordinates(frameClip, event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY)
    if (point) sendPointer(action, point.x, point.y)
    if (action === 'up') {
      activePointer.current = null
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  return (
    <div className="space-y-3 rounded-md border border-primary/30 bg-primary/5 p-4" data-testid="browser-account-portal">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><p className="font-medium">同一会话门户</p><p className="mt-1 text-xs text-muted-foreground">在当前登录会话中查看二维码或填写表单，关闭后会立即清除画面和输入。</p></div>
        <div className="flex items-center gap-2"><Badge variant="outline"><Wifi className="mr-1 size-3" />{transport === 'connected' ? '已连接' : transport === 'opening' ? '连接中' : transport === 'closed' ? '已关闭' : '错误'}</Badge><Button size="icon-xs" variant="ghost" onClick={onClose} aria-label="关闭门户"><X className="size-3" /></Button></div>
      </div>
      <p role="status" className="text-xs text-muted-foreground">{message}</p>
      {frameUrl ? <div className="overflow-hidden rounded-md border bg-black p-2"><img
        src={frameUrl}
        alt={`${frameKind === 'qr' ? '二维码' : frameKind === 'form' ? '表单' : '已批准'}门户投影`}
        className="mx-auto max-h-96 max-w-full object-contain focus-visible:outline-2 focus-visible:outline-primary"
        style={interactive ? { touchAction: 'none', cursor: 'crosshair' } : undefined}
        draggable={false}
        tabIndex={interactive ? 0 : undefined}
        aria-describedby={interactive ? 'portal-interaction-help' : undefined}
        onPointerDown={interactive ? (event) => forwardPointer(event, 'down') : undefined}
        onPointerMove={interactive ? (event) => forwardPointer(event, 'move') : undefined}
        onPointerUp={interactive ? (event) => forwardPointer(event, 'up') : undefined}
        onPointerCancel={interactive ? (event) => forwardPointer(event, 'up') : undefined}
        onLostPointerCapture={(event) => {
          if (activePointer.current === event.pointerId) forwardPointer(event, 'up')
          activePointer.current = null
        }}
        onKeyDown={interactive ? (event) => {
          const key = event.key === ' ' ? 'Space' : event.key
          if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || !isPortalControlKey(key)) return
          event.preventDefault()
          sendKey(key)
          if (key === 'Escape') event.currentTarget.blur()
        } : undefined}
      /></div> : null}
      {frameKind === 'qr' ? <Button type="button" size="sm" variant="outline" disabled={transport !== 'connected'} onClick={requestTakeover}>需要其他验证</Button> : null}
      {interactive ? <p id="portal-interaction-help" className="text-xs text-muted-foreground">可点击画面、手动拖动滑块。点击输入框后在下方填写短信验证码或密码；画面聚焦时可用 Tab、Enter、退格等按键操作，Esc 退出画面键盘操作。</p> : null}
      {focusedFieldRef ? <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); sendInput() }}><label className="min-w-56 flex-1 space-y-1 text-xs"><span>短信验证码 / 密码（一次性输入）</span><Input type="password" autoComplete="off" value={input} onChange={(event) => setInput(event.target.value)} placeholder="输入仅保存在内存中，发送后立即清除" disabled={transport !== 'connected'} /></label><Button type="submit" size="sm" disabled={!input || transport !== 'connected'}>发送一次</Button></form> : null}
      <p className="text-3xs text-muted-foreground">缺少或存在歧义的投影会保持未知；界面不会根据二维码消失、HTTP 状态、URL 变化或 Cookie 推断登录成功。</p>
    </div>
  )
}
