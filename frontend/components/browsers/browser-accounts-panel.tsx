'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter, useSearchParams } from 'next/navigation'
import { CheckCircle2, ExternalLink, Loader2, Plus, RefreshCw, ShieldAlert } from 'lucide-react'
import { toast } from 'sonner'

import { useAuth } from '@/components/auth/auth-provider'
import {
  createBrowserAccount,
  createBrowserLoginSession,
  getBrowserAccount,
  getBrowserLoginSession,
  listBrowserAccounts,
  listBrowserAccountWorkspaces,
  listBrowserWorkspaceMembers,
  operateBrowserAccount,
  performBrowserSessionAction,
  type BrowserAccount,
  type BrowserAccountOperationRequest,
  type BrowserAccountStatus,
  type BrowserLoginSession,
} from '@/lib/api/browser-accounts'
import { BrowserAccountPortal } from '@/components/browsers/browser-account-portal'
import { sessionTarget } from '@/lib/browser-accounts/portal-protocol'
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


function SessionCard({ workspaceId, account, session, canOperate, onChanged }: { workspaceId: string; account: BrowserAccount; session: BrowserLoginSession; canOperate: boolean; onChanged: (session: BrowserLoginSession) => void }) {
  const [pending, setPending] = useState<'view' | 'takeover' | 'confirm' | 'close' | null>(null)
  const [portalSession, setPortalSession] = useState<BrowserLoginSession | null>(null)
  const autoOpenedSession = useRef<string | null>(null)
  useEffect(() => {
    if (canOperate && sessionTarget(session) && !['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status) && autoOpenedSession.current !== session.id) {
      autoOpenedSession.current = session.id
      setPortalSession(session)
    }
  }, [canOperate, session])
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
      {portalSession && !['closed', 'expired', 'error', 'saving', 'saved', 'dormant'].includes(session.status) ? <BrowserAccountPortal workspaceId={workspaceId} accountId={account.id} session={session.id === portalSession.id ? session : portalSession} onClose={() => setPortalSession(null)} /> : null}
    </div>
  )
}

function AccountDetail({ workspaceId, account, canManage, canOperate, autoLogin = false, onAutoLoginStarted, onRefresh }: { workspaceId: string; account: BrowserAccount; canManage: boolean; canOperate: boolean; autoLogin?: boolean; onAutoLoginStarted: () => void; onRefresh: () => void }) {
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
  const autoLoginStarted = useRef(false)
  const startLogin = login.mutate
  useEffect(() => {
    if (autoLogin && canOperate && !autoLoginStarted.current) {
      autoLoginStarted.current = true
      onAutoLoginStarted()
      startLogin()
    }
  }, [autoLogin, canOperate, onAutoLoginStarted, startLogin])
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
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createdAccount, setCreatedAccount] = useState<BrowserAccount | null>(null)
  const [autoLoginId, setAutoLoginId] = useState<string | null>(null)
  const selected = selectedId ? accounts.find((account) => account.id === selectedId) ?? (createdAccount?.id === selectedId && createdAccount.workspace_id === workspaceId ? createdAccount : null) : null
  useEffect(() => { setSelectedId(null) }, [workspaceId])
  useEffect(() => { if (!selectedId && accounts[0]) setSelectedId(accounts[0].id); if (selectedId && selectedId !== createdAccount?.id && !accounts.some((account) => account.id === selectedId)) setSelectedId(accounts[0]?.id ?? null) }, [accounts, createdAccount?.id, selectedId])
  const workspacePicker = <label className="flex items-center gap-2 text-sm"><span>工作区</span><select aria-label="工作区" className="h-9 rounded-md border bg-background px-2" value={workspaceId ?? ''} disabled={workspaces.isLoading} onChange={(event) => { const params = new URLSearchParams(searchParams.toString()); params.set('workspace', event.target.value); router.replace(`/browser-accounts?${params.toString()}`) }}><option value="" disabled>选择工作区</option>{workspaces.data?.filter((item) => item.active).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
  if (!hasBrowserAccountReadPermission) {
    return (
      <Card className="overflow-hidden py-0">
        <CardHeader className="border-b bg-muted/20 py-4"><CardTitle className="text-base">浏览器账号</CardTitle><CardDescription>账号、登录会话和门户投影都必须在工作区读取权限确认后才能显示。</CardDescription><CardAction>{workspacePicker}</CardAction></CardHeader>
        <CardContent>{workspaces.isLoading || membersQuery.isLoading ? <LoadingState /> : membersQuery.error ? <ErrorState message="无法读取工作区权限，请重试。" hint={BACKEND_HINT} /> : <EmptyState title={workspaceId ? '无权访问此工作区' : '尚无可用工作区'} description={workspaceId ? '请切换到有访问权限的工作区，或联系管理员。' : '创建或加入工作区后即可管理浏览器账号。'} />}</CardContent>
      </Card>
    )
  }
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['browser-accounts', workspaceId] }); if (selected) void queryClient.invalidateQueries({ queryKey: ['browser-account', workspaceId, selected.id] }) }
  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="border-b bg-muted/20 py-4"><CardTitle className="text-base">工作区浏览器账号</CardTitle><CardDescription>每个账号拥有独立的认证资料和登录会话；账号认证状态与资源分配分别展示；资源已分配不代表节点健康。</CardDescription><CardAction>{workspacePicker}</CardAction></CardHeader>
      <CardContent className="grid gap-6 p-4 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.2fr)]">
        <section className="space-y-4" aria-labelledby="browser-account-create-title"><div><h3 id="browser-account-create-title" className="font-medium">添加账号</h3><p className="mt-1 text-xs text-muted-foreground">普通登录会开启一个真实会话；二维码刷新和原生表单只会在已批准的会话中展示。</p></div><CreateAccountForm workspaceId={workspaceId} canManage={canManageBrowserAccounts} onCreated={(account) => { setCreatedAccount(account); setAutoLoginId(account.id); setSelectedId(account.id); refresh() }} /><div className="border-t pt-4"><div className="flex items-center justify-between gap-2"><h3 className="font-medium">账号</h3><Button size="xs" variant="ghost" onClick={refresh} disabled={accountsQuery.isFetching}><RefreshCw className={accountsQuery.isFetching ? 'size-3 animate-spin' : 'size-3'} /></Button></div>{accountsQuery.isLoading ? <LoadingState /> : accountsQuery.error ? <ErrorState message={errorText(accountsQuery.error)} hint={BACKEND_HINT} /> : accounts.length === 0 ? <EmptyState title="暂无浏览器账号" description="创建工作区账号后即可开始已验证的登录会话。" /> : <div className="mt-2 space-y-2">{accounts.map((account) => <button key={account.id} type="button" onClick={() => setSelectedId(account.id)} className={`w-full rounded-md border p-3 text-left transition-colors ${selected?.id === account.id ? 'border-primary bg-primary/5' : 'hover:bg-muted/40'}`}><div className="flex items-center justify-between gap-2"><span className="truncate font-medium">{account.label}</span><AccountStatus status={account.status} /></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.site} · {account.id}</p><p className="mt-1 text-xs text-muted-foreground">{account.evidence_source ?? '认证凭据未知'} · revision {account.revision}</p></button>)}</div>}{accountsQuery.hasNextPage ? <Button variant="outline" size="sm" disabled={accountsQuery.isFetchingNextPage} onClick={() => void accountsQuery.fetchNextPage()}>{accountsQuery.isFetchingNextPage ? "加载中…" : "加载更多账号"}</Button> : null}</div></section>
        <section className="min-w-0" aria-labelledby="browser-account-detail-title"><h3 id="browser-account-detail-title" className="sr-only">所选账号详情</h3>{!workspaceId ? <EmptyState title="选择工作区" description="账号操作需要明确的工作区范围。" /> : !selected ? <EmptyState title="选择账号" description="创建或选择工作区账号以查看认证凭据和登录会话。" /> : <AccountDetail key={`${workspaceId}:${selected.id}`} workspaceId={workspaceId} account={selected} autoLogin={autoLoginId === selected.id} onAutoLoginStarted={() => setAutoLoginId(null)} canManage={canManageBrowserAccounts} canOperate={canOperateBrowserAccounts} onRefresh={refresh} />}</section>
      </CardContent>
    </Card>
  )
}
