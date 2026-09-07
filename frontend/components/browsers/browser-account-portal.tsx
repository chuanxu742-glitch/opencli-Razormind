'use client'

import { Wifi, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useBrowserAccountPortal } from '@/hooks/use-browser-account-portal'
import type { BrowserLoginSession } from '@/lib/api/browser-accounts'

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
  } = useBrowserAccountPortal({ workspaceId, accountId, session })

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
