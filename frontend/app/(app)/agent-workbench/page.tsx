'use client'

import { useRouter, useSearchParams } from 'next/navigation'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bot, LoaderCircle, Plus, RefreshCcw, Send } from 'lucide-react'
import { toast } from 'sonner'

import {
  useCancelWorkbenchTurn,
  useConfirmWorkbenchProposal,
  useCreateWorkbenchThread,
  useCreateWorkbenchTurn,
  useGovernedWorkspaces,
  useWorkbenchEvents,
  useWorkbenchRepositories,
  useWorkbenchRuntimes,
  useWorkbenchThread,
  useWorkbenchThreads,
} from '@/lib/api/hooks'
import type { WorkbenchProposal, WorkbenchThread, WorkbenchTurn } from '@/lib/api/types'
import { useLiveWorkbenchEvents } from '@/lib/workbench/use-live-workbench-events'
import { formatRelative } from '@/lib/format'
import { EventTranscript } from '@/components/workbench/event-transcript'
import { ProposalDetails } from '@/components/workbench/proposal-details'
import { TURN_STATUS_LABEL, TurnList } from '@/components/workbench/turn-list'
import { PageContainer } from '@/components/shell/page-container'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

export default function WorkbenchPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const workspaces = useGovernedWorkspaces()
  const [workspaceId, setWorkspaceId] = useState<string | null>(null)
  const [repositoryId, setRepositoryId] = useState('')
  const [runtimeId, setRuntimeId] = useState('')
  const [requirement, setRequirement] = useState('')
  const repositories = useWorkbenchRepositories(workspaceId)
  const runtimes = useWorkbenchRuntimes(workspaceId)
  const threads = useWorkbenchThreads(workspaceId)
  const routedWorkspaceId = searchParams.get('workspace')
  const selectedThreadId = routedWorkspaceId === workspaceId ? searchParams.get('thread') : null
  const selectedThread = useWorkbenchThread(workspaceId, selectedThreadId)
  const thread = selectedThread.data ?? threads.data?.find((candidate) => candidate.id === selectedThreadId) ?? null
  const selectedTurnId = searchParams.get('turn')
  const currentTurn = useMemo(() => {
    if (!thread) return null
    return thread.turns.find((turn) => turn.id === selectedTurnId)
      ?? [...thread.turns].sort((left, right) => right.sequence - left.sequence)[0]
      ?? null
  }, [thread, selectedTurnId])
  const events = useWorkbenchEvents(workspaceId, thread?.id ?? null, currentTurn?.id ?? null)
  useLiveWorkbenchEvents(workspaceId, thread?.id ?? null, currentTurn?.id ?? null, events.data, events.isSuccess)
  const createThread = useCreateWorkbenchThread()
  const createTurn = useCreateWorkbenchTurn()
  const cancelTurn = useCancelWorkbenchTurn()
  const confirmProposal = useConfirmWorkbenchProposal()

  const changeWorkspace = useCallback((nextWorkspaceId: string | null) => {
    setWorkspaceId(nextWorkspaceId)
    setRepositoryId('')
    setRuntimeId('')
    const params = new URLSearchParams(searchParams.toString())
    params.delete('thread')
    params.delete('turn')
    if (nextWorkspaceId) params.set('workspace', nextWorkspaceId)
    else params.delete('workspace')
    router.replace(params.size ? `/agent-workbench?${params.toString()}` : '/agent-workbench')
  }, [router, searchParams])

  useEffect(() => {
    const available = workspaces.data ?? []
    if (routedWorkspaceId && available.some((workspace) => workspace.id === routedWorkspaceId)) {
      if (workspaceId !== routedWorkspaceId) {
        setWorkspaceId(routedWorkspaceId)
        setRepositoryId('')
        setRuntimeId('')
      }
      return
    }
    if (available.length) changeWorkspace(available[0].id)
  }, [changeWorkspace, routedWorkspaceId, workspaceId, workspaces.data])

  useEffect(() => {
    if (repositories.isLoading) return
    const available = repositories.data ?? []
    if (repositoryId && available.some((repository) => repository.id === repositoryId)) return
    setRepositoryId(available[0]?.id ?? '')
  }, [repositoryId, repositories.data, repositories.isLoading])

  useEffect(() => {
    if (runtimes.isLoading) return
    const available = (runtimes.data ?? []).filter((runtime) => runtime.readiness === 'ready')
    if (runtimeId && available.some((runtime) => runtime.id === runtimeId)) return
    setRuntimeId(available[0]?.id ?? '')
  }, [runtimeId, runtimes.data, runtimes.isLoading])

  useEffect(() => {
    if (!selectedThreadId && workspaceId && threads.data?.[0]) {
      router.replace(`/agent-workbench?workspace=${workspaceId}&thread=${threads.data[0].id}`)
    }
  }, [router, selectedThreadId, threads.data, workspaceId])

  function selectThread(nextThread: WorkbenchThread, nextTurn?: WorkbenchTurn) {
    const params = new URLSearchParams()
    if (workspaceId) params.set('workspace', workspaceId)
    params.set('thread', nextThread.id)
    const turn = nextTurn ?? [...nextThread.turns].sort((left, right) => right.sequence - left.sequence)[0]
    if (turn) params.set('turn', turn.id)
    router.replace(`/agent-workbench?${params.toString()}`)
  }

  async function submitRequirement() {
    if (!workspaceId || !repositoryId || !runtimeId || !requirement.trim()) return
    if (!repositories.data?.some((repository) => repository.id === repositoryId)) return
    if (!runtimes.data?.some((runtime) => runtime.id === runtimeId && runtime.readiness === 'ready')) return
    const requestId = crypto.randomUUID()
    try {
      if (thread) {
        const turn = await createTurn.mutateAsync({
          workspaceId,
          threadId: thread.id,
          data: { runtimeId, requirement: requirement.trim(), requestId },
        })
        setRequirement('')
        const refreshed = await selectedThread.refetch()
        if (refreshed.data) selectThread(refreshed.data, turn)
        toast.success('已提交新的 Workbench 回合')
        return
      }
      const created = await createThread.mutateAsync({
        workspaceId,
        data: {
          repositoryId,
          runtimeId,
          requirement: requirement.trim(),
          requestId,
          title: requirement.trim().slice(0, 80),
        },
      })
      setRequirement('')
      selectThread(created)
      toast.success('已创建 Workbench 会话')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '提交 Workbench 回合失败')
    }
  }

  async function confirm(proposal: WorkbenchProposal) {
    if (!workspaceId || !thread) return
    try {
      await confirmProposal.mutateAsync({ workspaceId, threadId: thread.id, proposalId: proposal.id })
      await Promise.all([selectedThread.refetch(), threads.refetch()])
      toast.success('检查点已应用，目标分支保持干净')
    } catch (error) {
      const status = (error as Error & { status?: number }).status
      if (status === 409) {
        await Promise.all([selectedThread.refetch(), threads.refetch()])
        toast.error('目标分支、基础 SHA 或工作区已变化；已刷新持久化失败证据')
        return
      }
      toast.error(error instanceof Error ? error.message : '确认提案失败')
    }
  }

  async function cancel(turn: WorkbenchTurn) {
    if (!workspaceId || !thread) return
    try {
      await cancelTurn.mutateAsync({ workspaceId, threadId: thread.id, turnId: turn.id })
      await Promise.all([selectedThread.refetch(), threads.refetch()])
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '取消失败')
    }
  }

  const busy = createThread.isPending || createTurn.isPending
  const selectedRepository = repositories.data?.find((repository) => repository.id === repositoryId)
  const selectedRuntime = runtimes.data?.find((runtime) => runtime.id === runtimeId)

  return (
    <PageContainer
      eyebrow="Coding Workbench"
      title="受治理的代码变更"
      description="每个回合固定已发布的运行时版本，在控制器工作树中生成可审阅检查点；只有明确确认才会快进配置的目标分支。"
      actions={(
        <Button
          variant="outline"
          onClick={() => void Promise.all([repositories.refetch(), runtimes.refetch(), threads.refetch(), selectedThread.refetch()])}
        >
          <RefreshCcw className="size-4" />
          刷新
        </Button>
      )}
    >
      <section className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <Card>
          <CardHeader><CardTitle className="text-base">工作区与会话</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <label className="block space-y-1.5 text-sm">
              工作区
              <Select value={workspaceId ?? ''} onValueChange={(value) => changeWorkspace(value || null)}>
                <SelectTrigger className="w-full min-w-0"><SelectValue placeholder="选择工作区">
                  {(value: string | null) => workspaces.data?.find((workspace) => workspace.id === value)?.name ?? '选择工作区'}
                </SelectValue></SelectTrigger>
                <SelectContent>
                  {workspaces.data?.map((workspace) => (
                    <SelectItem key={workspace.id} value={workspace.id}>{workspace.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            {workspaces.isError ? (
              <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                无法读取可用工作区。检查后端连接或登录状态后刷新。
              </p>
            ) : null}
            <div className="space-y-2" aria-label="持久会话列表">
              {threads.isError ? (
                <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                  无法恢复 Workbench 会话。检查后端连接后刷新。
                </p>
              ) : null}
              {threads.isLoading ? <p className="text-sm text-muted-foreground">正在恢复会话…</p> : null}
              {!threads.isLoading && threads.data?.length ? threads.data.map((candidate) => {
                const latest = [...candidate.turns].sort((left, right) => right.sequence - left.sequence)[0]
                return (
                  <button
                    key={candidate.id}
                    type="button"
                    onClick={() => selectThread(candidate)}
                    className={`w-full rounded-md border p-3 text-left transition-colors ${candidate.id === thread?.id ? 'border-primary bg-primary/5' : 'hover:bg-muted/50'}`}
                  >
                    <p className="line-clamp-2 text-sm font-medium">{candidate.title || latest?.requirement || '未命名会话'}</p>
                    <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>{latest ? TURN_STATUS_LABEL[latest.status] : '没有回合'}</span>
                      <span>{formatRelative(candidate.updatedAt)}</span>
                    </div>
                  </button>
                )
              }) : null}
              {!threads.isLoading && !threads.isError && !threads.data?.length ? (
                <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                  还没有持久会话。选择仓库与运行时后提交第一条需求。
                </p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base"><Bot className="size-4" />{thread?.title || '新建 Workbench 会话'}</CardTitle>
              <CardDescription>{thread ? '继续这个持久会话；刷新和重新连接不会丢失已落库的事件。' : '先选择控制器已配置的仓库与可用运行时。'}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="block space-y-1.5 text-sm">
                  仓库
                  <Select value={repositoryId} onValueChange={(value) => setRepositoryId(value ?? '')} disabled={Boolean(thread) || repositories.isLoading || !repositories.data?.length}>
                    <SelectTrigger className="w-full min-w-0"><SelectValue placeholder="选择仓库">
                      {(value: string | null) => {
                        const repository = repositories.data?.find((candidate) => candidate.id === value)
                        return repository ? `${repository.name} · ${repository.defaultRef}` : '选择仓库'
                      }}
                    </SelectValue></SelectTrigger>
                    <SelectContent>{repositories.data?.map((repository) => <SelectItem key={repository.id} value={repository.id}>{repository.name} · {repository.defaultRef}</SelectItem>)}</SelectContent>
                  </Select>
                </label>
                <label className="block space-y-1.5 text-sm">
                  运行时
                  <Select value={runtimeId} onValueChange={(value) => setRuntimeId(value ?? '')} disabled={runtimes.isLoading || !runtimes.data?.length}>
                    <SelectTrigger className="w-full min-w-0"><SelectValue placeholder="选择运行时">
                      {(value: string | null) => {
                        const runtime = runtimes.data?.find((candidate) => candidate.id === value)
                        return runtime
                          ? `${runtime.name} · v${runtime.publishedVersion}${runtime.readiness === 'blocked' ? ' · 不可用' : ''}`
                          : '选择运行时'
                      }}
                    </SelectValue></SelectTrigger>
                    <SelectContent>{runtimes.data?.map((runtime) => <SelectItem key={runtime.id} value={runtime.id} disabled={runtime.readiness !== 'ready'}>{runtime.name} · v{runtime.publishedVersion}{runtime.readiness === 'blocked' ? ' · 不可用' : ''}</SelectItem>)}</SelectContent>
                  </Select>
                </label>
              </div>
              {repositories.isError ? <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">无法读取服务器配置的仓库。检查后端连接后刷新。</p> : null}
              {!repositories.isLoading && !repositories.isError && !repositories.data?.length ? <p role="status" className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">没有服务器配置的仓库。请让部署管理员设置 <code>WORKBENCH_REPOSITORIES</code>；不会在浏览器中输入或保存路径。</p> : null}
              {runtimes.isError ? <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">无法读取编码运行时。检查后端连接后刷新。</p> : null}
              {!runtimes.isLoading && !runtimes.isError && !runtimes.data?.length ? <p role="status" className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">没有可运行的编码 Agent。请让部署管理员发布 suggest_changes 版本，并为它配置与仓库映射相同的执行节点 URL 和共享文件系统 ID。</p> : null}
              {selectedRuntime?.readiness === 'blocked' ? <p role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700">运行时暂不可用：{selectedRuntime.reason ?? selectedRuntime.reasonCode ?? '节点未就绪'}</p> : null}
              <Textarea value={requirement} onChange={(event) => setRequirement(event.target.value)} placeholder="描述要实现、修复或审阅的代码需求。运行时只会在控制器拥有的隔离工作树中提出变更。" className="min-h-28 resize-y" />
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">{selectedRepository && selectedRuntime ? `${selectedRepository.name} · ${selectedRuntime.name} v${selectedRuntime.publishedVersion}` : '选择仓库和运行时以继续'}</p>
                <Button onClick={() => void submitRequirement()} disabled={busy || !workspaceId || !repositoryId || !runtimeId || selectedRuntime?.readiness !== 'ready' || !requirement.trim()}>
                  {busy ? <LoaderCircle className="size-4 animate-spin" /> : thread ? <Send className="size-4" /> : <Plus className="size-4" />}
                  {thread ? '添加回合' : '开始会话'}
                </Button>
              </div>
            </CardContent>
          </Card>

          {thread ? <TurnList thread={thread} currentTurnId={currentTurn?.id ?? null} cancelling={cancelTurn.isPending} onSelect={(turn) => selectThread(thread, turn)} onCancel={(turn) => void cancel(turn)} /> : null}
          {currentTurn ? <EventTranscript events={events.data} loading={events.isLoading} /> : null}
          {currentTurn?.output?.proposal ? <ProposalDetails proposal={currentTurn.output.proposal} onConfirm={() => void confirm(currentTurn.output!.proposal!)} confirming={confirmProposal.isPending} /> : null}
        </div>
      </section>
    </PageContainer>
  )
}
