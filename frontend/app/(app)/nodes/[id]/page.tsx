'use client'

import { use, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowLeft, Download, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { getInstallScriptUrl } from '@/lib/api/endpoints'
import { useDeleteNode, useNodeEvents, useNodeStats, useNodes } from '@/lib/api/hooks'
import { formatDateTime, formatNumber, formatRelative } from '@/lib/format'
import { BACKEND_HINT, EmptyState, ErrorState, LoadingState } from '@/components/shell/data-states'
import { PageContainer } from '@/components/shell/page-container'
import { StatusBadge } from '@/components/shell/status-badge'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

export default function NodeDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const router = useRouter()
  const nodes = useNodes()
  const events = useNodeEvents(id)
  const stats = useNodeStats(id)
  const deleteNode = useDeleteNode()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const node = nodes.data?.data.find((candidate) => candidate.id === id)

  async function handleDelete() {
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    try {
      await deleteNode.mutateAsync(id)
      toast.success('节点已删除')
      router.push('/nodes')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '删除节点失败')
    }
  }

  if (nodes.isLoading) return <LoadingState rows={4} />
  if (nodes.isError) {
    return <ErrorState message={(nodes.error as Error)?.message} hint={BACKEND_HINT} />
  }
  if (!node) {
    return <EmptyState title="找不到节点" description="该节点可能已被删除，返回节点列表查看最新资源。" />
  }

  const installUrl = typeof window === 'undefined' ? '/api/v1/nodes/install/agent.sh' : getInstallScriptUrl(window.location.origin)
  const nodeStats = stats.data
  const nodeEvents = events.data?.data ?? []

  return (
    <PageContainer
      eyebrow="EXECUTION RESOURCE"
      title={node.label}
      description="查看节点身份、运行统计、在线事件与安装入口。删除是受保护的运维操作。"
      actions={
        <Link href="/nodes" className={cn(buttonVariants({ variant: 'outline', size: 'sm' }))}>
          <ArrowLeft className="size-4" />
          返回节点列表
        </Link>
      }
    >
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <div>
              <CardTitle className="text-base">节点状态</CardTitle>
              <CardDescription>配置来自 Edge Node 注册记录，状态不由前端推测。</CardDescription>
            </div>
            <StatusBadge status={node.status} />
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-2">
            <InfoRow label="Node ID" value={node.id} mono />
            <InfoRow label="地址" value={node.url} mono />
            <InfoRow label="类型" value={node.node_type === 'docker' ? 'Docker' : 'Shell'} />
            <InfoRow label="协议" value={node.protocol.toUpperCase()} />
            <InfoRow label="模式" value={node.mode.toUpperCase()} />
            <InfoRow label="最近在线" value={formatRelative(node.last_seen_at)} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">节点操作</CardTitle>
            <CardDescription>安装脚本不携带凭证；删除前需要二次确认。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <Button variant="outline" nativeButton={false} render={<a href={installUrl} download="opencli-agent.sh" />}>
              <Download className="size-4" />
              下载 Agent 安装脚本
            </Button>
            <Button
              variant={confirmDelete ? 'destructive' : 'ghost'}
              onClick={() => void handleDelete()}
              disabled={deleteNode.isPending}
            >
              <Trash2 className="size-4" />
              {deleteNode.isPending ? '删除中…' : confirmDelete ? '确认删除节点' : '删除节点'}
            </Button>
            {confirmDelete ? (
              <p className="text-xs text-destructive">
                删除后节点注册记录和事件将不再出现在此处，确认要继续吗？
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">采集统计</CardTitle>
          <CardDescription>当前节点关联的任务运行与成果计数。</CardDescription>
        </CardHeader>
        <CardContent>
          {stats.isLoading ? (
            <LoadingState rows={2} />
          ) : stats.isError ? (
            <ErrorState message={(stats.error as Error)?.message} hint={BACKEND_HINT} />
          ) : nodeStats ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <Metric label="总运行" value={nodeStats.total} />
              <Metric label="成功" value={nodeStats.success} />
              <Metric label="失败" value={nodeStats.failed} />
              <Metric label="成功率" value={`${(nodeStats.success_rate * 100).toFixed(1)}%`} />
              <Metric label="采集记录" value={nodeStats.records_collected} />
            </div>
          ) : (
            <EmptyState title="暂无统计" description="该节点尚未产生可统计的运行数据。" />
          )}
        </CardContent>
      </Card>

      <Card className="overflow-hidden py-0">
        <CardHeader className="border-b py-4">
          <CardTitle className="text-base">节点事件</CardTitle>
        </CardHeader>
        {events.isLoading ? (
          <div className="p-4"><LoadingState rows={3} /></div>
        ) : events.isError ? (
          <div className="p-4"><ErrorState message={(events.error as Error)?.message} hint={BACKEND_HINT} /></div>
        ) : nodeEvents.length === 0 ? (
          <div className="p-4"><EmptyState title="暂无事件" description="节点注册、上线和离线事件会显示在这里。" /></div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>事件</TableHead>
                <TableHead>IP</TableHead>
                <TableHead>时间</TableHead>
                <TableHead>详情</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {nodeEvents.map((event) => (
                <TableRow key={event.id}>
                  <TableCell><Badge variant="outline">{event.event}</Badge></TableCell>
                  <TableCell className="font-mono text-xs">{event.ip ?? '—'}</TableCell>
                  <TableCell className="text-muted-foreground">{formatDateTime(event.created_at)}</TableCell>
                  <TableCell className="max-w-64 truncate font-mono text-xs text-muted-foreground">
                    {event.event_meta ? JSON.stringify(event.event_meta) : '—'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
    </PageContainer>
  )
}

function InfoRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-md border p-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn('truncate text-sm', mono && 'font-mono text-xs')} title={value}>{value}</span>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md border p-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="mt-1 font-mono text-lg font-semibold tabular-nums">
        {typeof value === 'number' ? formatNumber(value) : value}
      </div>
    </div>
  )
}
