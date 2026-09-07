'use client'

import { useState } from 'react'
import { Loader2, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import {
  useBrowserBindings,
  useChromePool,
  useCreateBrowserBinding,
  useDeleteBrowserBinding,
} from '@/lib/api/hooks'
import type { BrowserBinding } from '@/lib/api/types'
import { formatRelative } from '@/lib/format'
import { BACKEND_HINT, EmptyState, ErrorState, LoadingState } from '@/components/shell/data-states'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'

const EMPTY_FORM = { browser_endpoint: '', site: '', notes: '' }

function AddBindingDialog() {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const chromePool = useChromePool()
  const createMutation = useCreateBrowserBinding()
  const knownEndpoints = chromePool.data?.endpoints ?? []

  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) setForm(EMPTY_FORM)
  }

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const site = form.site.trim()
    const browserEndpoint = form.browser_endpoint.trim()
    if (!site || !browserEndpoint) {
      toast.error('请填写站点与浏览器地址')
      return
    }
    createMutation.mutate(
      { site, browser_endpoint: browserEndpoint, notes: form.notes.trim() || undefined },
      {
        onSuccess: () => {
          toast.success('已添加站点绑定')
          setOpen(false)
        },
        onError: (error: Error) => toast.error(error.message),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger render={<Button size="sm" />}>
        <Plus className="size-4" />
        添加绑定
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleSubmit} className="flex flex-col gap-5">
          <DialogHeader>
            <DialogTitle>添加站点绑定</DialogTitle>
            <DialogDescription>
              将一个站点固定路由到指定的浏览器实例（本机 Chrome 或远程 Agent 端点）。
            </DialogDescription>
          </DialogHeader>

          <FieldGroup className="gap-4">
            <Field>
              <FieldLabel htmlFor="binding-site">站点</FieldLabel>
              <Input
                id="binding-site"
                placeholder="xiaohongshu.com"
                value={form.site}
                onChange={(event) => setForm((current) => ({ ...current, site: event.target.value }))}
                autoFocus
              />
              <FieldDescription>站点域名或标识；已被绑定的站点不能重复添加。</FieldDescription>
            </Field>

            <Field>
              <FieldLabel htmlFor="binding-endpoint">浏览器地址</FieldLabel>
              <Input
                id="binding-endpoint"
                placeholder="http://agent-2:19222"
                value={form.browser_endpoint}
                onChange={(event) =>
                  setForm((current) => ({ ...current, browser_endpoint: event.target.value }))
                }
                list="browser-binding-known-endpoints"
              />
              {knownEndpoints.length > 0 ? (
                <datalist id="browser-binding-known-endpoints">
                  {knownEndpoints.map((endpoint) => (
                    <option key={endpoint.url} value={endpoint.url} />
                  ))}
                </datalist>
              ) : null}
              <FieldDescription>可从上方 Chrome 实例列表中选一个地址，也可以手动填写。</FieldDescription>
            </Field>

            <Field>
              <FieldLabel htmlFor="binding-notes">备注（可选）</FieldLabel>
              <Textarea
                id="binding-notes"
                rows={2}
                placeholder="路由原因、负责人等"
                value={form.notes}
                onChange={(event) => setForm((current) => ({ ...current, notes: event.target.value }))}
              />
            </Field>
          </FieldGroup>

          <DialogFooter>
            <Button type="submit" disabled={createMutation.isPending} className="min-w-24">
              {createMutation.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              添加
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function LegacyBrowserBindingsPanel() {
  const { data, isLoading, isError, error } = useBrowserBindings()
  const bindings = data ?? []
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const deleteMutation = useDeleteBrowserBinding()

  const handleDelete = (binding: BrowserBinding) => {
    if (confirmDeleteId !== binding.id) {
      setConfirmDeleteId(binding.id)
      return
    }
    deleteMutation.mutate(binding.id, {
      onSuccess: () => {
        toast.success('已删除站点绑定')
        setConfirmDeleteId(null)
      },
      onError: (cause: Error) => toast.error(cause.message),
    })
  }

  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="border-b bg-muted/20 py-4">
        <CardTitle className="text-base">站点绑定</CardTitle>
        <CardDescription>按站点把采集固定路由到某个浏览器实例，覆盖默认的池内自动分配。</CardDescription>
        <CardAction>
          <AddBindingDialog />
        </CardAction>
      </CardHeader>
      <CardContent className="p-4">
        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState message={(error as Error)?.message} hint={BACKEND_HINT} />
        ) : bindings.length === 0 ? (
          <EmptyState title="暂无站点绑定" description="默认按池内可用实例自动分配，无需手动绑定。" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>站点</TableHead>
                <TableHead>浏览器地址</TableHead>
                <TableHead>备注</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {bindings.map((binding) => {
                const confirming = confirmDeleteId === binding.id
                return (
                  <TableRow key={binding.id}>
                    <TableCell className="font-medium">{binding.site}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {binding.browser_endpoint}
                    </TableCell>
                    <TableCell className="max-w-64 truncate text-muted-foreground">
                      {binding.notes || '—'}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{formatRelative(binding.created_at)}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="xs"
                        variant={confirming ? 'destructive' : 'ghost'}
                        disabled={deleteMutation.isPending}
                        onClick={() => handleDelete(binding)}
                        className="gap-1"
                      >
                        <Trash2 className="size-3" />
                        {confirming ? '确认删除' : '删除'}
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * The old site → endpoint binding editor is intentionally no longer mounted.
 * Account ownership and verified sessions are managed from the account route;
 * this compatibility surface keeps a clear migration path for old imports.
 */
export function BrowserBindingsPanel() {
  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="border-b bg-muted/20 py-4">
        <CardTitle className="text-base">Browser account bindings</CardTitle>
        <CardDescription>
          Legacy site bindings no longer assign browser identities. Select a workspace account and its pinned session from the account manager.
        </CardDescription>
        <CardAction>
          <Button render={<a href="/browser-accounts" />} variant="outline" size="sm">Open account manager</Button>
        </CardAction>
      </CardHeader>
    </Card>
  )
}
