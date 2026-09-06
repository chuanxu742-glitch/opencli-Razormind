'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import { CheckCircle2, ExternalLink, Loader2, Plus, RefreshCw, ShieldAlert } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { useAuth } from '@/components/auth/auth-provider'
import { useMyWorkspaces } from '@/lib/api/hooks'
import {
  createBrowserAccount,
  createBrowserLoginSession,
  getBrowserAccount,
  getBrowserLoginSession,
  listBrowserAccounts,
  operateBrowserAccount,
  performBrowserSessionAction,
  type BrowserAccount,
  type BrowserAccountOperationRequest,
  type BrowserAccountStatus,
  type BrowserLoginSession,
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

function SessionCard({ workspaceId, account, session, onChanged }: { workspaceId: string; account: BrowserAccount; session: BrowserLoginSession; onChanged: (session: BrowserLoginSession) => void }) {
  const [pending, setPending] = useState<'takeover' | 'confirm' | 'close' | null>(null)
  const run = async (action: 'takeover' | 'confirm' | 'close') => {
    setPending(action)
    try {
      const next = await performBrowserSessionAction(workspaceId, account.id, session.id, action, session.revision, `browser-session-${action}-${session.id}-${session.revision}`)
      onChanged(next)
      toast.success(`Session ${action} accepted`)
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
      <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Target tab / frame</p><p className="mt-1 font-mono text-xs">{session.tab_id ?? 'pending'} / {session.frame_id ?? 'pending'}</p></div><div><p className="text-xs text-muted-foreground">View generation</p><p className="mt-1 font-mono text-xs">{session.view_generation}</p></div><div><p className="text-xs text-muted-foreground">Origin</p><p className="mt-1 truncate font-mono text-xs">{session.origin ?? 'not established'}</p></div></div>
      {challenge ? <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200"><ShieldAlert className="mt-0.5 size-4 shrink-0" /><p>Challenge or unknown state stays in this same session. Take over the approved portal, then confirm the observed identity; no new session or account is guessed.</p></div> : null}
      <div className="flex flex-wrap gap-2">{challenge ? <Button size="sm" variant="outline" disabled={pending !== null} onClick={() => void run('takeover')}>{pending === 'takeover' ? <Loader2 className="size-4 animate-spin" /> : null}Take over session</Button> : null}<Button size="sm" variant="outline" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('confirm')}>{pending === 'confirm' ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}Confirm observed identity</Button><Button size="sm" variant="ghost" disabled={pending !== null || session.status === 'closed'} onClick={() => void run('close')}>Close session</Button></div>
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
      <div className="flex flex-wrap items-end gap-2 rounded-md border p-3"><label className="min-w-64 flex-1 space-y-1 text-sm"><span>Source Binding Revision <span className="text-muted-foreground">(optional)</span></span><Input value={sourceBindingRevisionId} onChange={(event) => setSourceBindingRevisionId(event.target.value)} placeholder="Pinned revision id" disabled={login.isPending || current.status === 'closed'} /></label><Button disabled={login.isPending || current.status === 'closed'} onClick={() => login.mutate()}>{login.isPending ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}{login.isPending ? 'Opening...' : 'Open login session'}</Button><Button variant="outline" disabled={accountOperation.isPending} onClick={() => accountOperation.mutate('auth_required')}>{current.auth_required ? 'Clear auth-required' : 'Mark auth-required'}</Button></div>
      <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">Lifecycle is server-owned: opening -&gt; presenting -&gt; refreshing/verifying -&gt; saving -&gt; saved/dormant. A failed save remains visible as saving/error and never reports success or retries transient input.</div>
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
        <section className="space-y-4" aria-labelledby="browser-account-create-title"><div><h3 id="browser-account-create-title" className="font-medium">Add account</h3><p className="mt-1 text-xs text-muted-foreground">A normal login starts one real session. QR refresh and native forms are presented only inside that approved session.</p></div><CreateAccountForm workspaceId={workspaceId} onCreated={(account) => { setSelectedId(account.id); refresh() }} /><div className="border-t pt-4"><div className="flex items-center justify-between gap-2"><h3 className="font-medium">Accounts</h3><Button size="xs" variant="ghost" onClick={refresh} disabled={accountsQuery.isFetching}><RefreshCw className={accountsQuery.isFetching ? 'size-3 animate-spin' : 'size-3'} /></Button></div>{workspaces.isLoading || accountsQuery.isLoading ? <LoadingState /> : accountsQuery.error ? <ErrorState message={errorText(accountsQuery.error)} hint={BACKEND_HINT} /> : accounts.length === 0 ? <EmptyState title="No browser accounts" description="Add a workspace-scoped account to begin a verified login session." /> : <div className="mt-2 space-y-2">{accounts.map((account) => <button key={account.id} type="button" className={`w-full rounded-md border p-3 text-left transition-colors hover:bg-muted/40 ${account.id === selected?.id ? 'border-primary bg-muted/30' : ''}`} onClick={() => setSelectedId(account.id)}><div className="flex items-center justify-between gap-2"><span className="truncate font-medium">{account.label}</span><AccountStatus status={account.status} /></div><div className="mt-1 truncate font-mono text-xs text-muted-foreground">{account.site} · {account.id}</div><div className="mt-2 flex flex-wrap gap-1"><Badge variant="secondary">auth {typeof account.auth_evidence === 'string' ? account.auth_evidence : account.auth_evidence?.kind ?? 'unknown'}</Badge>{account.paused ? <Badge variant="outline">paused</Badge> : null}</div></button>)}</div>}</div><Link href="/browsers" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"><ExternalLink className="size-3" />View active browser resources</Link></section>
        <section className="min-w-0" aria-labelledby="browser-account-detail-title"><h3 id="browser-account-detail-title" className="sr-only">Selected account detail</h3>{!workspaceId ? <EmptyState title="Select a workspace" description="Account operations require an explicit workspace scope." /> : !selected ? <EmptyState title="Select an account" description="Create or select a workspace account to inspect its evidence and session." /> : <AccountDetail workspaceId={workspaceId} account={selected} onRefresh={refresh} />}</section>
      </CardContent>
    </Card>
  )
}
