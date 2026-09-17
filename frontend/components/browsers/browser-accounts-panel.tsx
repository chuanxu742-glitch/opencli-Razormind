'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter, useSearchParams } from 'next/navigation'
import { ExternalLink, Loader2, Plus, RefreshCw, ShieldAlert } from 'lucide-react'
import { toast } from 'sonner'

import { useAuth } from '@/components/auth/auth-provider'
import {
  createBrowserAccount,
  createBrowserLoginSession,
  getBrowserAccount,
  getBrowserLoginSession,
  getBrowserLoginReadiness,
  getBrowserNativeWindowSupport,
  openBrowserNativeWindow,
  listBrowserLoginOptions,
  listBrowserLoginSessions,
  listBrowserAccounts,
  listBrowserAccountWorkspaces,
  listBrowserWorkspaceMembers,
  operateBrowserAccount,
  renameBrowserAccount,
  performBrowserSessionAction,
  type BrowserAccount,
  type BrowserAccountOperationRequest,
  type BrowserAccountStatus,
  type BrowserLoginSession,
} from '@/lib/api/browser-accounts'
import { BrowserAccountPortal } from '@/components/browsers/browser-account-portal'
import { browserAccountErrorText } from '@/lib/browser-accounts/error-text'
import { accountActionLabel, presentBrowserAccount } from '@/lib/browser-accounts/account-presentation'
import { sessionTarget } from '@/lib/browser-accounts/portal-protocol'
import { BACKEND_HINT, EmptyState, ErrorState, LoadingState } from '@/components/shell/data-states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

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
  return browserAccountErrorText(error)
}

function AccountStatus({ account }: { account: BrowserAccount }) {
  const presentation = presentBrowserAccount(account)
  return <Badge variant="outline" className={statusClass[presentation.status]}>{presentation.label}</Badge>
}

function SessionStatus({ status }: { status: BrowserAccountStatus }) {
  const labels: Record<BrowserAccountStatus, string> = { opening: '打开中', presenting: '展示中', refreshing: '刷新中', verifying: '验证中', challenge: '需处理', unknown: '未知', saving: '保存中', saved: '已保存', dormant: '休眠', expired: '已过期', closed: '已关闭', error: '错误' }
  return <Badge variant="outline" className={statusClass[status]}>{labels[status]}</Badge>
}
function CreateAccountForm({ workspaceId, canManage, onCreated }: { workspaceId: string | null; canManage: boolean; onCreated: (account: BrowserAccount, startLogin: boolean) => void }) {
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [site, setSite] = useState('')
  const [platformId, setPlatformId] = useState('')
  const options = useQuery({ queryKey: ['browser-login-options', workspaceId], queryFn: () => listBrowserLoginOptions(workspaceId as string), enabled: Boolean(workspaceId && canManage), refetchInterval: 5000 })
  const platform = options.data?.find((item) => item.id === platformId)
  const customPlatform = platformId === 'custom'
  const nativeSupport = useQuery({ queryKey: ['browser-native-window-support', workspaceId], queryFn: () => getBrowserNativeWindowSupport(workspaceId as string), enabled: Boolean(workspaceId && canManage), staleTime: 30_000 })
  const canStartLogin = nativeSupport.data?.available === true && (customPlatform || Boolean(platform?.available || ['capacity_full', 'pool_pending'].includes(platform?.reason_code ?? '')))
  const [label, setLabel] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [runtimeBundleId, setRuntimeBundleId] = useState('')
  const [loginRuleId, setLoginRuleId] = useState('')
  const [loginRuleVersion, setLoginRuleVersion] = useState('')
  const mutation = useMutation({
    mutationFn: () => {
      if (!canManage) throw new Error('当前成员角色无权创建浏览器账号')
      if (!workspaceId) throw new Error('请先选择工作区')
      if (!customPlatform && !platform?.browser_login_supported) throw new Error('此平台尚无受管登录入口')
      const cleanSite = customPlatform ? site.trim() : platform?.site ?? ''
      const cleanLabel = label.trim()
      if (!cleanSite || !cleanLabel) throw new Error('站点和账号名称不能为空')
      if (customPlatform && Boolean(loginRuleId.trim()) !== Boolean(loginRuleVersion.trim())) throw new Error('登录规则 ID 和版本需要同时填写，或同时留空')
      return createBrowserAccount(workspaceId, {
        workspace_id: workspaceId,
        site: cleanSite,
        label: cleanLabel,
        ...(customPlatform && nodeId.trim() ? { node_id: nodeId.trim() } : {}),
        ...(customPlatform && runtimeBundleId.trim() ? { runtime_bundle_id: runtimeBundleId.trim() } : {}),
        ...(customPlatform && loginRuleId.trim() ? { login_rule_id: loginRuleId.trim() } : {}),
        ...(customPlatform && loginRuleVersion.trim() ? { login_rule_version: loginRuleVersion.trim() } : {}),
      }, `browser-account-create-${crypto.randomUUID()}`)
    },
    onSuccess: (account) => {
      if (!mounted.current) return
      setSite('')
      setLabel('')
      onCreated(account, canStartLogin)
      toast.success('浏览器账号已创建')
    },
    onError: (error) => toast.error(errorText(error)),
  })
  return (
    <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span>平台</span><select aria-label="平台" className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={platformId} onChange={(event) => setPlatformId(event.target.value)} required disabled={!workspaceId || !canManage || mutation.isPending || !options.isSuccess}><option value="" disabled>{options.isPending ? '正在加载平台…' : options.isError ? '平台加载失败' : '选择平台'}</option><optgroup label="常用社媒平台">{options.data?.slice(0, 8).map((item) => <option key={item.id} value={item.id} disabled={!item.browser_login_supported}>{item.label}{!item.available ? (item.reason_code === 'capacity_full' ? ' · 登录槽位占用中' : ' · 登录资源未就绪') : ''}</option>)}</optgroup><optgroup label="其他浏览器平台">{options.data?.slice(8).filter((item) => item.browser_login_supported).map((item) => <option key={item.id} value={item.id}>{item.label}{!item.available ? (item.reason_code === 'capacity_full' ? ' · 登录槽位占用中' : ' · 登录资源未就绪') : ''}</option>)}</optgroup><optgroup label="其他认证方式 · 暂无受管登录入口">{options.data?.filter((item) => !item.browser_login_supported).map((item) => <option key={item.id} value={item.id} disabled>{item.label} · {item.requires_configuration ? '需配置地址' : item.category === 'local_app' ? '本地应用认证' : item.category === 'api_or_local' ? 'API / 适配器认证' : '尚无登录入口'}</option>)}</optgroup><option value="custom">自定义测试站点</option></select></label>
        <label className="space-y-1 text-sm"><span>账号名称</span><Input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="例如 小红书运营号" required disabled={!workspaceId || !canManage || mutation.isPending} /></label>
      </div>
      {customPlatform ? <label className="block space-y-1 text-sm"><span>测试站点地址</span><Input value={site} onChange={(event) => setSite(event.target.value)} required placeholder="管理员配置的完整测试站点 origin" disabled={mutation.isPending} /></label> : null}
      {options.error ? <div role="alert" className="text-sm text-destructive">平台加载失败：{errorText(options.error)}<Button type="button" variant="ghost" size="sm" onClick={() => void options.refetch()}>重试</Button></div> : null}
      {platform ? <div className="rounded-md border bg-muted/30 p-3 text-sm"><p className="font-medium">{platform.label} · {platform.site}</p><p className="mt-1 text-xs text-muted-foreground">{platform.message}</p>{!platform.available ? <Button type="button" variant="ghost" size="sm" disabled={options.isFetching} onClick={() => void options.refetch()}>重新检查登录资源</Button> : null}{platform.note ? <p className="mt-2 text-xs text-muted-foreground">{platform.note}</p> : null}</div> : null}
      {customPlatform ? <details className="rounded-md border p-3">
        <summary className="cursor-pointer text-sm font-medium">高级配置</summary>
        <p className="my-3 text-xs leading-relaxed text-muted-foreground">仅在管理员提供了配置标识时填写。留空表示不指定节点、运行时包或登录规则，由服务端检查可用配置；若未配置可用资源，登录时会提示原因。登录规则 ID 和版本需同时填写或同时留空。</p>
        <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span>所属节点 ID <span className="text-muted-foreground">（可选）</span></span><Input value={nodeId} onChange={(event) => setNodeId(event.target.value)} placeholder="管理员提供的节点 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>运行时包 ID <span className="text-muted-foreground">（可选）</span></span><Input value={runtimeBundleId} onChange={(event) => setRuntimeBundleId(event.target.value)} placeholder="已验证的包 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>登录规则 ID <span className="text-muted-foreground">（可选）</span></span><Input value={loginRuleId} onChange={(event) => setLoginRuleId(event.target.value)} placeholder="已验证的规则 ID" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
        <label className="space-y-1 text-sm"><span>登录规则版本 <span className="text-muted-foreground">（可选）</span></span><Input value={loginRuleVersion} onChange={(event) => setLoginRuleVersion(event.target.value)} placeholder="v1" disabled={!workspaceId || !canManage || mutation.isPending} /></label>
      </div>
      </details> : null}
      {mutation.error ? <p role="alert" className="text-sm text-destructive">{errorText(mutation.error)}</p> : null}
      <p className="text-xs text-muted-foreground">登录操作在账号各自的浏览器环境中完成，无需在此填写平台密码。</p>
      <Button type="submit" disabled={!workspaceId || !canManage || mutation.isPending || !platformId || !options.isSuccess || (!customPlatform && !platform?.browser_login_supported)}>{mutation.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}{mutation.isPending ? '正在创建…' : canStartLogin ? ['capacity_full', 'pool_pending'].includes(platform?.reason_code ?? '') ? '创建并启动浏览器' : '创建并打开浏览器' : '保存账号'}</Button>
    </form>
  )
}


function SessionCard({ workspaceId, account, session, canOperate, nativeAvailable, openRequest, onChanged }: { workspaceId: string; account: BrowserAccount; session: BrowserLoginSession; canOperate: boolean; nativeAvailable: boolean; openRequest: string | null; onChanged: (session: BrowserLoginSession) => void }) {
  const [pending, setPending] = useState<'view' | 'takeover' | 'confirm' | 'close' | null>(null)
  const [portalSession, setPortalSession] = useState<BrowserLoginSession | null>(null)
  const autoOpenedSession = useRef<string | null>(null)
  const saving = session.status === 'saving' || account.status === 'saving'
  const desktopReady = session.purpose === 'browser' && ['presenting', 'refreshing', 'verifying', 'challenge', 'unknown'].includes(session.status) && Boolean(session.lease_id && session.node_id && session.node_boot_id && session.profile_id && session.command_id)
  const nativeWindow = useMutation({
    mutationFn: () => {
      if (!canOperate || !nativeAvailable || !desktopReady || saving || pending !== null) throw new Error('独立浏览器尚未就绪')
      return openBrowserNativeWindow(workspaceId, account.id, session.id)
    },
    onSuccess: (result) => toast.message(result.message),
    onError: (error) => toast.error(errorText(error)),
  })
  const openNativeWindow = nativeWindow.mutate
  useEffect(() => {
    const requestKey = `${session.id}:${openRequest ?? 'initial'}`
    if (!canOperate || saving || pending === 'close' || ['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status) || autoOpenedSession.current === requestKey) return
    if (session.purpose === 'browser') {
      // A selected detail or a page refresh must never spawn a desktop window.
      if (!openRequest || !desktopReady || !nativeAvailable || nativeWindow.isPending) return
      autoOpenedSession.current = requestKey
      openNativeWindow()
    } else if (sessionTarget(session)) {
      autoOpenedSession.current = requestKey
      setPortalSession(session)
    }
  }, [canOperate, saving, pending, desktopReady, session, openRequest, nativeAvailable, nativeWindow.isPending, openNativeWindow])
  const run = async (action: 'view' | 'takeover' | 'confirm' | 'close') => {
    if (pending !== null || saving) return
    setPending(action)
    try {
      const next = await performBrowserSessionAction(workspaceId, account.id, session.id, action, session.revision, `browser-session-${action}-${session.id}-${session.revision}`)
      onChanged(next)
      if (action === 'view' || action === 'takeover') setPortalSession(next)
      if (action === 'close') setPortalSession(null)
      toast.message(action === 'view' || action === 'takeover' ? (next.purpose === 'browser' ? '正在连接隔离浏览器' : sessionTarget(next) ? '正在连接登录画面' : '浏览器尚未提供登录画面') : action === 'confirm' ? '身份确认已提交' : action === 'close' && session.purpose === 'browser' ? '安全停止与保存请求已提交' : '关闭请求已提交')
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
      {!desktopReady && !sessionTarget(session) && session.status === 'opening' ? <p role="status" className="rounded-md border bg-muted/20 p-3 text-sm">正在等待账号浏览器启动。页面会自动更新，请勿重复打开。</p> : null}
      {saving ? <p role="status" className="rounded-md border border-violet-500/30 bg-violet-500/10 p-3 text-sm" data-testid="browser-saving-status">正在安全停止浏览器并保存账号数据，请勿重复关闭或重新打开。保存完成后，页面会自动更新。</p> : null}
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{session.purpose === 'browser' ? '独立浏览器' : '登录会话'}</p><p className="mt-1 text-xs text-muted-foreground">{session.status === 'opening' && !session.lease_id ? '请求正在排队，休眠资源会自动唤醒，准备完成后会打开桌面窗口。' : session.purpose === 'browser' ? '在独立桌面窗口中使用此账号的 Chromium。关闭窗口后自动保存，单次会话最长 30 分钟。' : '在同一个登录窗口完成验证，确认后等待保存结果。'}</p></div><SessionStatus status={session.status} /></div>
      <details className="rounded-md border p-3"><summary className="cursor-pointer text-xs text-muted-foreground">会话诊断信息</summary><p className="my-2 break-all font-mono text-xs">{session.id} · revision {session.revision}</p><div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Lease / epoch</p><p className="mt-1 font-mono text-xs">{session.lease_id ?? 'pending'} / {session.epoch}</p></div><div><p className="text-xs text-muted-foreground">Node / boot</p><p className="mt-1 truncate font-mono text-xs">{session.node_id ?? 'pending'} / {session.node_boot_id ?? 'pending'}</p></div><div><p className="text-xs text-muted-foreground">Target tab / frame</p><p className="mt-1 font-mono text-xs">{session.tab_id ?? 'pending'} / {session.frame_id ?? 'pending'}</p></div></div>
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Profile</p><p className="mt-1 font-mono text-xs">{session.profile_id ?? 'new'} · v{session.profile_version ?? '—'} · {session.profile_state}</p></div><div><p className="text-xs text-muted-foreground">View generation</p><p className="mt-1 font-mono text-xs">{session.view_generation}</p></div><div><p className="text-xs text-muted-foreground">Origin</p><p className="mt-1 truncate font-mono text-xs">{session.origin ?? 'not established'}</p></div></div></details>
      {challenge ? <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200"><ShieldAlert className="mt-0.5 size-4 shrink-0" /><p>{account.login_rule_id?.startsWith('official-') ? '可在官方窗口处理登录；此平台身份校验尚未接入，不能保存为已验证账号。' : '请在当前会话中完成平台验证，核对身份后确认。'}</p></div> : null}
      <div className="flex flex-wrap gap-2">{desktopReady || sessionTarget(session) ? <Button size="sm" disabled={saving || pending !== null || nativeWindow.isPending || (session.purpose === 'browser' && !nativeAvailable) || ['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status)} onClick={() => session.purpose === 'browser' ? openNativeWindow() : void run(challenge ? 'takeover' : 'view')}>{pending === 'view' || pending === 'takeover' || nativeWindow.isPending ? <Loader2 className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}{nativeWindow.isPending ? '正在打开桌面窗口…' : session.purpose === 'browser' ? '打开浏览器' : challenge ? '处理验证' : '查看登录画面'}</Button> : null}<DropdownMenu><DropdownMenuTrigger render={<Button size="sm" variant="ghost" />}>更多</DropdownMenuTrigger><DropdownMenuContent align="end">{challenge && session.purpose !== 'browser' && !account.login_rule_id?.startsWith('official-') ? <DropdownMenuItem disabled={pending !== null || session.status === 'closed'} onClick={() => void run('confirm')}>确认已核对身份</DropdownMenuItem> : null}<DropdownMenuItem disabled={saving || pending !== null || ['closed', 'expired', 'error', 'saved', 'dormant'].includes(session.status)} onClick={() => void run('close')}>{session.status === 'opening' && !session.lease_id ? '取消排队' : session.purpose === 'browser' ? '关闭浏览器并保存' : '关闭会话'}</DropdownMenuItem></DropdownMenuContent></DropdownMenu></div>
      {session.purpose === 'browser' && nativeWindow.isSuccess && nativeWindow.data.session_id === session.id && !['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status) ? <p role="status" className="text-sm text-muted-foreground" data-testid="browser-native-window-status">独立浏览器窗口已打开，可在桌面任务栏切换。账号数据仍保留在原隔离环境中。</p> : null}
      {nativeWindow.error ? <p role="alert" className="text-sm text-destructive">{errorText(nativeWindow.error)}</p> : null}
      {portalSession && session.purpose !== 'browser' && !['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status) ? <BrowserAccountPortal workspaceId={workspaceId} accountId={account.id} session={session.id === portalSession.id ? session : portalSession} onClose={() => setPortalSession(null)} /> : null}
    </div>
  )
}

function AccountDetail({ workspaceId, account, canManage, canOperate, loginRequest, onLoginRequestHandled, onRefresh }: { workspaceId: string; account: BrowserAccount; canManage: boolean; canOperate: boolean; loginRequest: string | null; onLoginRequestHandled: () => void; onRefresh: () => void }) {
  const queryClient = useQueryClient()
  const [localSessions, setLocalSessions] = useState<BrowserLoginSession[]>([])
  const session = localSessions[0] ?? null
  const setSession = (next: BrowserLoginSession) => setLocalSessions((previous) => [next, ...previous.filter((item) => item.id !== next.id)])
  const [portalRequest, setPortalRequest] = useState<{ id: string; sessionId: string } | null>(null)
  const [sourceBindingRevisionId, setSourceBindingRevisionId] = useState('')
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameDraft, setRenameDraft] = useState('')
  const detailQuery = useQuery({ queryKey: ['browser-account', workspaceId, account.id], queryFn: () => getBrowserAccount(workspaceId, account.id), refetchInterval: 2_000 })
  const current = detailQuery.data && detailQuery.data.revision >= account.revision ? detailQuery.data : account
  const rename = useMutation({
    mutationFn: (name: string) => {
      if (!canManage) throw new Error('当前成员角色无权重命名账号')
      return renameBrowserAccount(workspaceId, current.id, name.trim(), current.revision)
    },
    onSuccess: (next) => {
      queryClient.setQueryData(['browser-account', workspaceId, current.id], (previous: BrowserAccount | undefined) => previous && previous.revision > next.revision ? previous : next)
      setRenameOpen(false)
      onRefresh()
      toast.success('账号名称已更新')
    },
    onError: (error) => { toast.error(errorText(error)); void detailQuery.refetch() },
  })
  const readiness = useQuery({ queryKey: ['browser-login-readiness', workspaceId, current.id, current.revision], queryFn: () => getBrowserLoginReadiness(workspaceId, current.id), enabled: canOperate, refetchInterval: 5_000 })
  const nativeSupport = useQuery({ queryKey: ['browser-native-window-support', workspaceId], queryFn: () => getBrowserNativeWindowSupport(workspaceId), enabled: canOperate, staleTime: 30_000 })
  const sessions = useQuery({ queryKey: ['browser-login-sessions', workspaceId, current.id], queryFn: () => listBrowserLoginSessions(workspaceId, current.id), refetchInterval: 2_000 })
  const sessionQuery = useQuery({ queryKey: ['browser-login-session', workspaceId, current.id, session?.id], queryFn: () => getBrowserLoginSession(workspaceId, current.id, session?.id as string), enabled: Boolean(session?.id), refetchInterval: 2_000 })
  const sessionSnapshots = useMemo(() => {
    const newest = new Map<string, BrowserLoginSession>()
    // Prefer acknowledged mutations over equal-revision polling snapshots.
    for (const item of [...localSessions, sessionQuery.data, ...(sessions.data ?? [])]) {
      if (item && item.revision > (newest.get(item.id)?.revision ?? -1)) newest.set(item.id, item)
    }
    return newest
  }, [sessions.data, localSessions, sessionQuery.data])
  const activeSession = [...sessionSnapshots.values()].find((item) => !['closed', 'expired', 'error', 'saved', 'dormant'].includes(item.status))
  const saving = current.status === 'saving' || activeSession?.status === 'saving'
  const browserEligible = !saving && !current.paused && !['closed', 'expired', 'saving'].includes(current.status) && sessions.isSuccess
  const browserOpenable = browserEligible && nativeSupport.data?.available === true
  const login = useMutation({
    mutationFn: (requestId: string) => {
      if (!canOperate) throw new Error('当前成员角色无权打开隔离浏览器')
      if (!browserOpenable) throw new Error('账号状态已更新，暂时无法打开隔离浏览器')
      return createBrowserLoginSession(workspaceId, current.id, { purpose: 'browser', expected_revision: current.revision, ...(sourceBindingRevisionId.trim() ? { source_binding_revision_id: sourceBindingRevisionId.trim() } : {}) }, `browser-desktop-${current.id}-${current.revision}-${requestId}`)
    },
    onSuccess: (next) => { setSession(next); setPortalRequest({ id: crypto.randomUUID(), sessionId: next.id }); void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, current.id] }); void queryClient.invalidateQueries({ queryKey: ['browser-login-sessions', workspaceId, current.id] }); onRefresh(); toast.message('正在准备账号浏览器，随后打开独立桌面窗口') },
    onError: (error) => toast.error(errorText(error)),
  })
  const handledLoginRequest = useRef<string | null>(null)
  const startLogin = login.mutate
  useEffect(() => {
    if (loginRequest !== null && saving) {
      handledLoginRequest.current = loginRequest
      onLoginRequestHandled()
      return
    }
    if (loginRequest !== null && canOperate && browserOpenable && !login.isPending && handledLoginRequest.current !== loginRequest) {
      handledLoginRequest.current = loginRequest
      onLoginRequestHandled()
      if (activeSession?.purpose === 'browser') setPortalRequest({ id: loginRequest, sessionId: activeSession.id })
      else startLogin(loginRequest)
    }
  }, [loginRequest, saving, canOperate, browserOpenable, activeSession, login.isPending, onLoginRequestHandled, startLogin])
  const qrLogin = useMutation({
    mutationFn: (requestId: string) => {
      if (!canOperate) throw new Error('当前成员角色无权打开扫码登录')
      if (saving || activeSession || !readiness.data?.ready) throw new Error(readiness.data?.message ?? '扫码登录尚未就绪')
      return createBrowserLoginSession(workspaceId, current.id, { purpose: 'login', expected_revision: current.revision, ...(sourceBindingRevisionId.trim() ? { source_binding_revision_id: sourceBindingRevisionId.trim() } : {}) }, `browser-qr-login-${current.id}-${current.revision}-${requestId}`)
    },
    onSuccess: (next) => { setSession(next); setPortalRequest({ id: crypto.randomUUID(), sessionId: next.id }); onRefresh(); toast.message('扫码登录请求已提交') },
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
  const effectiveSession = activeSession ?? (session ? sessionSnapshots.get(session.id) : undefined)
  return (
    <section className="space-y-4" data-testid="browser-account-detail">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-lg font-semibold">{current.label}</p><p className="mt-1 break-all text-xs text-muted-foreground">{current.site}</p></div><div className="flex items-center gap-2"><AccountStatus account={current} /></div></div>
       {canOperate && activeSession?.purpose !== 'browser' && (readiness.isPending || readiness.error || !readiness.data?.ready) ? <div className="rounded-md border bg-muted/20 p-3" data-testid="login-readiness"><p className="text-sm font-medium">{readiness.isPending ? '正在检查登录条件…' : readiness.error ? '登录条件检查失败' : readiness.data?.code === 'session_queued' ? '等待浏览器资源' : ['capacity_full', 'pool_pending'].includes(readiness.data?.code ?? '') ? '可排队登录' : '暂时无法登录'}</p><p role="status" className="mt-1 text-xs text-muted-foreground">{readiness.error ? errorText(readiness.error) : readiness.data?.message}</p><Button type="button" variant="ghost" size="sm" disabled={readiness.isFetching} onClick={() => void readiness.refetch()}><RefreshCw className="size-3" />重新检查</Button></div> : null}
      {canOperate && !nativeSupport.data?.available ? <div className="rounded-md border bg-muted/20 p-3" data-testid="native-window-support"><p role="status" className="text-sm">{nativeSupport.isPending ? '正在检查桌面窗口组件…' : nativeSupport.error ? '无法检查桌面窗口组件，请重试。' : nativeSupport.data?.message}</p><Button variant="ghost" size="sm" disabled={nativeSupport.isFetching} onClick={() => void nativeSupport.refetch()}>重新检查桌面组件</Button></div> : null}
      {current.login_rule_id?.startsWith('official-') ? <div className="rounded-md border bg-muted/30 p-3 text-sm"><p className="font-medium">身份校验待接入</p><p className="mt-1 text-xs text-muted-foreground">可使用官方网页登录；当前不能保存为已验证账号，人工确认不会替代身份校验。</p></div> : current.status === 'challenge' || (current.status === 'unknown' && current.status_reason_code !== 'browser_session_verified') ? <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm"><p className="font-medium">需要人工处理</p><p className="mt-1 text-xs text-muted-foreground">当前账号尚未验证。请在同一会话中处理登录，核对平台身份后再确认。</p></div> : null}
       {current.status_reason_code === 'browser_session_verified' ? (presentBrowserAccount(current).label === '已登录' ? <p role="status" className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">平台接口已确认登录会话有效，可以继续使用浏览器。自动任务所需的账号身份校验单独进行。</p> : null) : current.status_reason_code === 'browser_login_observed' ? (
         presentBrowserAccount(current).label === '网页已登录'
           ? <p role="status" className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">已识别网页上的个人入口，可以继续使用浏览器；平台身份尚未通过接口验证。</p>
           : null
       ) : current.status_reason_code ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">{browserAccountErrorText({ code: current.status_reason_code })}</p> : null}
       <details className="rounded-md border p-3"><summary className="cursor-pointer text-sm text-muted-foreground">高级登录配置与诊断</summary><p className="my-3 break-all font-mono text-xs text-muted-foreground">账号 ID：{current.id} · revision {current.revision}</p><div className="grid gap-2 text-xs sm:grid-cols-2"><p>平台身份：{current.platform_identity?.display_name ?? '未验证'} <span className="font-mono text-muted-foreground">{current.platform_identity ? `${current.platform_identity.provider}/${current.platform_identity.subject}` : ''}</span></p><p>证据来源：{current.evidence_source === 'rule_verified' ? '规则验证' : current.evidence_source === 'manual_fallback' ? '人工确认' : '无'}</p><p className="break-all">节点：{current.node_id ?? '未分配'} · 包：{current.runtime_bundle_id ?? '未分配'}</p><p className="break-all">规则：{current.login_rule_id ?? '未配置'} · {current.login_rule_version ?? '—'}</p><p className="break-all">浏览器数据：{current.profile_id ?? '无'} · v{current.profile_version ?? '—'} · {current.profile_manifest_id ?? '未提交快照'}</p>{current.status_reason_code ? <p className="font-mono">原始状态码：{current.status_reason_code}</p> : null}</div><label className="mt-3 block space-y-1 text-sm"><span>绑定来源版本（可选）</span><Input value={sourceBindingRevisionId} onChange={(event) => setSourceBindingRevisionId(event.target.value)} placeholder="管理员提供的固定版本 ID" disabled={!canOperate || login.isPending || current.status === 'closed'} /></label><p className="mt-2 text-xs text-muted-foreground">留空表示不指定绑定来源版本，使用服务端的账号登录配置。</p></details>
       <div className="flex flex-wrap gap-2">{browserEligible && !activeSession ? <Button disabled={!canOperate || !browserOpenable || login.isPending} onClick={() => login.mutate(crypto.randomUUID())}>{login.isPending ? <Loader2 className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}{login.isPending ? '正在提交…' : '打开浏览器'}</Button> : null}<DropdownMenu><DropdownMenuTrigger render={<Button size="sm" variant="ghost" />}>更多</DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem disabled={!canOperate || saving || Boolean(activeSession) || !readiness.data?.ready || qrLogin.isPending} onClick={() => qrLogin.mutate(crypto.randomUUID())}>扫码登录</DropdownMenuItem><DropdownMenuItem disabled={!canManage || rename.isPending} onClick={() => { rename.reset(); setRenameDraft(current.label); setRenameOpen(true) }}>重命名</DropdownMenuItem><DropdownMenuItem disabled={!canManage || accountOperation.isPending || current.status === 'closed'} onClick={() => accountOperation.mutate(current.paused || current.auth_required ? 'resume' : 'auth_required')}>{current.paused || current.auth_required ? '恢复账号' : '要求重新认证'}</DropdownMenuItem></DropdownMenuContent></DropdownMenu></div>
      <Dialog open={renameOpen && canManage} onOpenChange={(open) => { if (!rename.isPending) setRenameOpen(open) }}>
        <DialogContent showCloseButton={!rename.isPending}>
          <DialogHeader><DialogTitle>重命名账号</DialogTitle><DialogDescription>修改此处显示的账号备注名称。</DialogDescription></DialogHeader>
          <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); if (renameDraft.trim() && !rename.isPending) rename.mutate(renameDraft.trim()) }}>
            <label className="block space-y-2 text-sm"><span>账号名称</span><Input autoFocus value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} required maxLength={255} disabled={rename.isPending} /></label>
            {rename.error ? <p role="alert" className="text-sm text-destructive">{errorText(rename.error)}</p> : null}
            <div className="flex justify-end gap-2"><Button type="button" variant="outline" disabled={rename.isPending} onClick={() => setRenameOpen(false)}>取消</Button><Button type="submit" disabled={rename.isPending || !renameDraft.trim() || renameDraft.trim() === current.label}>{rename.isPending ? '正在保存…' : '保存名称'}</Button></div>
          </form>
        </DialogContent>
      </Dialog>
      {login.error ? <p role="alert" className="text-sm text-destructive">{errorText(login.error)}</p> : null}
      {accountOperation.error ? <p role="alert" className="text-sm text-destructive">{errorText(accountOperation.error)}</p> : null}
      {sessions.error ? <ErrorState message={errorText(sessions.error)} hint={BACKEND_HINT} /> : null}
      {sessionQuery.error ? <ErrorState message={errorText(sessionQuery.error)} hint={BACKEND_HINT} /> : null}
      {effectiveSession ? <SessionCard key={effectiveSession.id} workspaceId={workspaceId} account={current} session={effectiveSession} canOperate={canOperate} nativeAvailable={nativeSupport.data?.available === true} openRequest={portalRequest?.sessionId === effectiveSession.id ? portalRequest.id : null} onChanged={setSession} /> : null}
      {detailQuery.error ? <ErrorState message={errorText(detailQuery.error)} hint={BACKEND_HINT} /> : null}
    </section>
  )
}

export function BrowserAccountsPanel() {
  const searchParams = useSearchParams()
  const workspaces = useQuery({ queryKey: ['browser-account-workspaces'], queryFn: listBrowserAccountWorkspaces })
  const workspaceId = searchParams.get('workspace') ?? workspaces.data?.[0]?.id ?? null
  // Key the resolved workspace, including changes to the default workspace.
  // This discards selection, drafts and pending creation callbacks together.
  return <BrowserAccountsWorkspacePanel key={workspaceId ?? 'none'} />
}

function BrowserAccountsWorkspacePanel() {
  const { identity } = useAuth()
  const router = useRouter()
  const searchParams = useSearchParams()
  const workspaces = useQuery({ queryKey: ['browser-account-workspaces'], queryFn: listBrowserAccountWorkspaces })
  const workspaceId = searchParams.get('workspace') ?? workspaces.data?.[0]?.id ?? null
  const queryClient = useQueryClient()
  const membersQuery = useQuery({ queryKey: ['browser-workspace-members', workspaceId], queryFn: () => listBrowserWorkspaceMembers(workspaceId as string), enabled: Boolean(workspaceId), refetchInterval: 10_000 })
  const workspace = workspaces.data?.find((candidate) => candidate.id === workspaceId)
  const currentMember = membersQuery.data?.find((member) => member.subject === identity?.subject)
  const membershipKnown = membersQuery.isSuccess && Boolean(currentMember)
  const hasBrowserAccountReadPermission = Boolean(workspace?.active && membershipKnown && !currentMember?.disabled)
  const canManageBrowserAccounts = hasBrowserAccountReadPermission && (currentMember?.role === 'admin' || currentMember?.role === 'maintainer')
  const canOperateBrowserAccounts = hasBrowserAccountReadPermission && (currentMember?.role === 'admin' || currentMember?.role === 'maintainer' || currentMember?.role === 'operator')
  const accountsQuery = useInfiniteQuery({
    queryKey: ['browser-accounts', workspaceId, 'pages'],
    queryFn: ({ pageParam }) => listBrowserAccounts(workspaceId as string, { cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: hasBrowserAccountReadPermission,
    refetchInterval: 5_000,
  })
  const accounts = useMemo(() => accountsQuery.data?.pages.flatMap((page) => page.items) ?? [], [accountsQuery.data])
  const [createOpen, setCreateOpen] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createdAccount, setCreatedAccount] = useState<BrowserAccount | null>(null)
  const [loginRequest, setLoginRequest] = useState<{ accountId: string; id: string } | null>(null)
  const selected = selectedId ? accounts.find((account) => account.id === selectedId) ?? (createdAccount?.id === selectedId && createdAccount.workspace_id === workspaceId ? createdAccount : null) : null
  const availableWorkspaces = workspaces.data?.filter((item) => item.active) ?? []
  const showWorkspacePicker = availableWorkspaces.length > 1 || (availableWorkspaces.length > 0 && !hasBrowserAccountReadPermission)
  const workspacePicker = <label className="flex min-w-0 items-center gap-2 text-sm"><span className="shrink-0">工作区</span><select aria-label="工作区" className="h-9 min-w-0 max-w-full rounded-md border bg-background px-2" value={workspaceId ?? ''} disabled={workspaces.isLoading} onChange={(event) => { const params = new URLSearchParams(searchParams.toString()); params.set('workspace', event.target.value); router.replace(`/browser-accounts?${params.toString()}`) }}><option value="" disabled>选择工作区</option>{workspaces.data?.filter((item) => item.active).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
  if (!hasBrowserAccountReadPermission) {
    return (
      <Card className="overflow-hidden py-0">
        <CardHeader className="border-b bg-muted/20 py-4"><CardDescription>账号、登录会话和门户投影都必须在工作区读取权限确认后才能显示。</CardDescription>{showWorkspacePicker ? <CardAction>{workspacePicker}</CardAction> : null}</CardHeader>
        <CardContent>{workspaces.isLoading || membersQuery.isLoading ? <LoadingState /> : membersQuery.error ? <ErrorState message="无法读取工作区权限，请重试。" hint={BACKEND_HINT} /> : <EmptyState title={workspaceId ? '无权访问此工作区' : '尚无可用工作区'} description={workspaceId ? '请切换到有访问权限的工作区，或联系管理员。' : '创建或加入工作区后即可管理浏览器账号。'} />}</CardContent>
      </Card>
    )
  }
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['browser-accounts', workspaceId] }); if (selected) void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, selected.id] }) }
  return (
    <Card className="overflow-hidden py-0">
      {showWorkspacePicker ? <CardHeader className="flex justify-end border-b bg-muted/20 py-3"><CardAction>{workspacePicker}</CardAction></CardHeader> : null}
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><h3 className="font-medium">账号列表</h3><p className="mt-1 text-xs text-muted-foreground">已加载 {accounts.length} 个账号{accountsQuery.hasNextPage ? '，下方可加载更多' : ''} · 认证状态以最近验证结果为准</p></div>
          <div className="flex gap-2">
            <Button variant="outline" aria-label="刷新账号列表" onClick={refresh} disabled={accountsQuery.isFetching}><RefreshCw className={accountsQuery.isFetching ? 'size-4 animate-spin' : 'size-4'} />刷新</Button>
            <Dialog open={createOpen} onOpenChange={setCreateOpen}>
              <DialogTrigger render={<Button disabled={!canManageBrowserAccounts} />}><Plus className="size-4" />添加账号</DialogTrigger>
              <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg">
                <DialogHeader><DialogTitle>添加账号</DialogTitle><DialogDescription>选择平台并为账号命名。登录资源就绪后，按所选平台使用扫码或官方网页登录；其他认证方式会单独标注。</DialogDescription></DialogHeader>
                <CreateAccountForm key={workspaceId} workspaceId={workspaceId} canManage={canManageBrowserAccounts} onCreated={(account, startLogin) => { if (account.workspace_id !== workspaceId) return; setCreatedAccount(account); setLoginRequest(startLogin ? { accountId: account.id, id: crypto.randomUUID() } : null); setSelectedId(account.id); setCreateOpen(false); refresh() }} />
              </DialogContent>
            </Dialog>
          </div>
        </div>
        {!canManageBrowserAccounts ? <p className="text-xs text-muted-foreground">当前角色可查看账号{canOperateBrowserAccounts ? '并操作登录会话' : ''}；添加账号需要管理员或维护者权限。</p> : null}
        {accountsQuery.isLoading ? <LoadingState /> : accountsQuery.error ? <ErrorState message={errorText(accountsQuery.error)} hint={BACKEND_HINT} /> : accounts.length === 0 ? <EmptyState title="暂无账号" description="点击“添加账号”，选择平台并命名。" /> : <>
          <div aria-label="账号列表" className="overflow-x-auto">
            <table className="w-full min-w-[480px] table-fixed text-sm">
              <thead className="border-b text-left text-xs text-muted-foreground"><tr><th className="px-3 py-2 font-medium">账号</th><th className="hidden w-[25%] px-3 py-2 font-medium sm:table-cell">平台</th><th className="w-36 px-3 py-2 font-medium">状态</th><th className="w-32 px-3 py-2 text-right font-medium">操作</th></tr></thead>
              <tbody>
            {accounts.map((account) => {
              const presentation = presentBrowserAccount(account)
              const select = () => { setSelectedId(account.id); setLoginRequest(null) }
              const requestLogin = () => { setSelectedId(account.id); setLoginRequest(presentation.action !== 'view' ? { accountId: account.id, id: crypto.randomUUID() } : null) }
              return <tr key={account.id} className="border-b last:border-0"><td className="px-3 py-3 align-middle"><button type="button" className="max-w-full truncate text-left font-medium underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={select}>{account.label}</button><span className="mt-1 block truncate text-xs text-muted-foreground sm:hidden" title={account.site}>{account.site}</span></td><td className="hidden px-3 py-3 align-middle text-muted-foreground sm:table-cell"><span className="block truncate" title={account.site}>{account.site}</span></td><td className="px-3 py-3 align-middle"><AccountStatus account={account} /></td><td className="px-3 py-3 text-right align-middle"><Button size="sm" variant={presentation.canStartLogin ? 'default' : 'outline'} disabled={!canOperateBrowserAccounts || account.status === 'saving'} onClick={requestLogin}>{accountActionLabel(presentation.action)}</Button></td></tr>
            })}
              </tbody>
            </table>
          </div>
        </>}
        {accountsQuery.hasNextPage ? <Button variant="outline" size="sm" disabled={accountsQuery.isFetchingNextPage} onClick={() => void accountsQuery.fetchNextPage()}>{accountsQuery.isFetchingNextPage ? '加载中…' : '加载更多账号'}</Button> : null}
        {selected && workspaceId ? <section className="min-w-0 space-y-4 border-t pt-4" aria-label="账号详情"><div className="flex justify-end"><Button size="sm" variant="ghost" onClick={() => { setSelectedId(null); setLoginRequest(null) }}>收起详情</Button></div><AccountDetail key={`${workspaceId}:${selected.id}`} workspaceId={workspaceId} account={selected} loginRequest={loginRequest?.accountId === selected.id ? loginRequest.id : null} onLoginRequestHandled={() => setLoginRequest(null)} canManage={canManageBrowserAccounts} canOperate={canOperateBrowserAccounts} onRefresh={refresh} /></section> : null}
      </CardContent>
    </Card>
  )
}
