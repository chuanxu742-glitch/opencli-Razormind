'use client'

import Link from 'next/link'
import { useState } from 'react'
import { toast } from 'sonner'

import { useAuth } from '@/components/auth/auth-provider'
import { BACKEND_HINT, ErrorState, LoadingState } from '@/components/shell/data-states'
import { PageContainer } from '@/components/shell/page-container'
import { RestartApiCard } from '@/components/system/restart-api-card'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { type SystemConfig, type SystemConfigPatch, useSystemConfig, useUpdateSystemConfig } from '@/lib/api/hooks'

type StatusBadge = {
  label: string
  critical?: boolean
}

function ConfigStatusRow({
  label,
  value,
  badge,
}: {
  label: string
  value: React.ReactNode
  badge?: StatusBadge
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border px-3 py-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="flex min-w-0 flex-wrap items-center gap-2 text-sm font-medium sm:justify-end sm:text-right">
        <span className="min-w-0 break-all">{value}</span>
        {badge ? (
          <span className={badge.critical ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
            {badge.label}
          </span>
        ) : null}
      </span>
    </div>
  )
}

function configuredBadge(configured: boolean): StatusBadge {
  return { label: configured ? '已配置' : '未配置' }
}

function SystemConfigOverview({ config }: { config: SystemConfig }) {
  const configuredIntegrations = [
    config.api_auth_configured,
    config.credential_encryption_configured,
    config.oidc_configured,
    config.smtp_configured,
  ].filter(Boolean).length

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader>
          <CardTitle><h2>部署概览</h2></CardTitle>
          <CardDescription>来自当前服务端配置；这里不把配置值描述为运行健康状态。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-lg border p-3">
            <p className="text-xs text-muted-foreground">应用环境</p>
            <p className="mt-1 font-medium">{config.app_env}</p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-xs text-muted-foreground">任务执行器</p>
            <p className="mt-1 font-medium">{config.task_executor}</p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-xs text-muted-foreground">数据库</p>
            <p className="mt-1 font-medium">{config.database_kind}</p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-xs text-muted-foreground">镜像版本</p>
            <p className="mt-1 break-all font-mono text-xs">{config.image_tag}</p>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle><h2>当前执行策略</h2></CardTitle>
            <CardDescription>展示服务端正在使用的配置值。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ConfigStatusRow
              label="采集执行位置"
              value={config.collection_mode === 'local' ? '本机执行' : '远程 Agent 执行'}
            />
            <ConfigStatusRow label="调度编排器" value={config.collection_orchestrator} />
            <ConfigStatusRow
              label="最大并发任务"
              value={`${config.local_max_concurrent_pipelines} 个`}
            />
            <ConfigStatusRow label="OpenCLI 超时" value={`${config.opencli_timeout} 秒`} />
            <ConfigStatusRow label="默认时区" value={config.default_timezone} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle><h2>浏览器与 Agent 配置</h2></CardTitle>
            <CardDescription>仅展示地址和组网配置，不代表端点在线或可连接。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ConfigStatusRow label="默认 CDP" value={config.opencli_cdp_endpoint} />
            <ConfigStatusRow
              label="已配置的 Agent Pool"
              value={
                config.agent_pool_endpoints.length ? (
                  <span className="flex flex-col gap-1 font-mono text-xs">
                    {config.agent_pool_endpoints.map((endpoint) => (
                      <span key={endpoint}>{endpoint}</span>
                    ))}
                  </span>
                ) : (
                  '未配置，将使用默认 CDP'
                )
              }
              badge={{ label: `${config.agent_pool_endpoints.length} 个地址` }}
            />
            <ConfigStatusRow
              label="当前有效 CDP 地址"
              value={
                <span className="flex flex-col gap-1 font-mono text-xs">
                  {config.effective_cdp_endpoints.map((endpoint) => (
                    <span key={endpoint}>{endpoint}</span>
                  ))}
                </span>
              }
            />
            <ConfigStatusRow label="Fleet 网络" value={config.fleet_network_provider} />
            <ConfigStatusRow label="NetBird 模式" value={config.netbird_mode} />
            <ConfigStatusRow
              label="对外访问地址"
              value={config.public_url || '未配置'}
              badge={configuredBadge(Boolean(config.public_url))}
            />
            <Button variant="ghost" nativeButton={false} render={<Link href="/nodes" />}>
              查看节点
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle><h2>AI 与自动执行</h2></CardTitle>
            <CardDescription>当前模型请求边界和全局控制策略。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ConfigStatusRow
              label="模型请求超时"
              value={`${config.llm_request_timeout_seconds} 秒`}
            />
            <ConfigStatusRow label="模型并发数" value={String(config.llm_max_concurrency)} />
            <ConfigStatusRow
              label="控制策略"
              value={config.control_mode === 'advisory' ? '建议模式' : '自动模式'}
            />
            <ConfigStatusRow
              label="全局暂停自动执行"
              value={config.control_kill_switch ? '已开启' : '已关闭'}
              badge={
                config.control_kill_switch
                  ? { label: '拦截自动动作', critical: true }
                  : { label: '未拦截' }
              }
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle><h2>集成与密钥状态</h2></CardTitle>
            <CardDescription>
              {configuredIntegrations}/4 项已配置；只显示是否存在，不在浏览器中暴露密钥。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ConfigStatusRow
              label="API / Fleet 访问令牌"
              value={config.api_auth_configured ? '服务端已保存' : '服务端未保存'}
              badge={configuredBadge(config.api_auth_configured)}
            />
            <ConfigStatusRow
              label="凭证加密密钥"
              value={config.credential_encryption_configured ? '服务端已保存' : '服务端未保存'}
              badge={configuredBadge(config.credential_encryption_configured)}
            />
            <ConfigStatusRow
              label="OIDC 组织登录"
              value={config.oidc_configured ? '环境变量完整' : '环境变量不完整'}
              badge={configuredBadge(config.oidc_configured)}
            />
            <ConfigStatusRow
              label="SMTP 通知"
              value={config.smtp_configured ? '主机与发件人已配置' : '主机或发件人未配置'}
              badge={configuredBadge(config.smtp_configured)}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
type RuntimeSettingsValues = {
  collectionMode: SystemConfig['collection_mode']
  collectionOrchestrator: SystemConfig['collection_orchestrator']
  localMaxConcurrentPipelines: string
  opencliTimeout: string
  defaultTimezone: string
  publicUrl: string
  fleetNetworkProvider: SystemConfig['fleet_network_provider']
  netbirdMode: SystemConfig['netbird_mode']
  opencliCdpEndpoint: string
  agentPoolEndpoints: string
  llmRequestTimeoutSeconds: string
  llmMaxConcurrency: string
  controlMode: SystemConfig['control_mode']
  controlKillSwitch: boolean
}

type RuntimeSettingsChange = <Key extends keyof RuntimeSettingsValues>(
  key: Key,
  value: RuntimeSettingsValues[Key],
) => void

type SettingsSectionProps = {
  values: RuntimeSettingsValues
  onChange: RuntimeSettingsChange
}

function ExecutionSettingsSection({ values, onChange }: SettingsSectionProps) {
  return (
    <section className="space-y-4" aria-labelledby="execution-settings-title">
      <div>
        <h3 id="execution-settings-title" className="text-sm font-medium">
          任务与执行
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          决定采集任务在哪里运行、由谁编排，以及本机执行的资源边界。
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="collection-mode">采集执行位置</FieldLabel>
          <Select
            value={values.collectionMode}
            onValueChange={(value) =>
              onChange('collectionMode', value as SystemConfig['collection_mode'])
            }
          >
            <SelectTrigger id="collection-mode" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="local">本机执行</SelectItem>
              <SelectItem value="agent">远程 Agent 执行</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel htmlFor="collection-orchestrator">调度编排器</FieldLabel>
          <Select
            value={values.collectionOrchestrator}
            onValueChange={(value) =>
              onChange(
                'collectionOrchestrator',
                value as SystemConfig['collection_orchestrator'],
              )
            }
          >
            <SelectTrigger id="collection-orchestrator" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="admin">Admin</SelectItem>
              <SelectItem value="iii">III</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel htmlFor="max-concurrent-pipelines">本机最大并发任务</FieldLabel>
          <Input
            id="max-concurrent-pipelines"
            type="number"
            min={1}
            max={64}
            value={values.localMaxConcurrentPipelines}
            onChange={(event) => onChange('localMaxConcurrentPipelines', event.target.value)}
            aria-describedby="max-concurrent-pipelines-description"
            required
          />
          <FieldDescription id="max-concurrent-pipelines-description">允许范围 1–64；只约束本机执行管线。</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="opencli-timeout">OpenCLI 超时（秒）</FieldLabel>
          <Input
            id="opencli-timeout"
            type="number"
            min={1}
            max={3600}
            value={values.opencliTimeout}
            onChange={(event) => onChange('opencliTimeout', event.target.value)}
            aria-describedby="opencli-timeout-description"
            required
          />
          <FieldDescription id="opencli-timeout-description">允许范围 1–3600 秒。</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="default-timezone">默认时区</FieldLabel>
          <Input
            id="default-timezone"
            value={values.defaultTimezone}
            onChange={(event) => onChange('defaultTimezone', event.target.value)}
            placeholder="Asia/Shanghai"
            maxLength={64}
            pattern=".*\S.*"
            aria-describedby="default-timezone-description"
            required
          />
          <FieldDescription id="default-timezone-description">填写非空的 IANA 时区名称，例如 Asia/Shanghai。</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="public-url">对外访问地址</FieldLabel>
          <Input
            id="public-url"
            value={values.publicUrl}
            onChange={(event) => onChange('publicUrl', event.target.value)}
            placeholder="未配置"
            maxLength={2048}
          />
          <FieldDescription>留空表示未配置；不会在此处探测连通性。</FieldDescription>
        </Field>
      </div>
    </section>
  )
}

function BrowserSettingsSection({ values, onChange }: SettingsSectionProps) {
  return (
    <section className="space-y-4 border-t pt-6" aria-labelledby="browser-settings-title">
      <div>
        <h3 id="browser-settings-title" className="text-sm font-medium">
          浏览器与 Agent 网络
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          这些是执行地址和组网方式的配置值，不是实时健康检查。
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="fleet-network-provider">Fleet 网络</FieldLabel>
          <Select
            value={values.fleetNetworkProvider}
            onValueChange={(value) =>
              onChange(
                'fleetNetworkProvider',
                value as SystemConfig['fleet_network_provider'],
              )
            }
          >
            <SelectTrigger id="fleet-network-provider" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="lan">LAN</SelectItem>
              <SelectItem value="netbird">NetBird</SelectItem>
              <SelectItem value="wireguard">WireGuard</SelectItem>
              <SelectItem value="ssh">SSH</SelectItem>
              <SelectItem value="custom">自定义</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel htmlFor="netbird-mode">NetBird 模式</FieldLabel>
          <Select
            value={values.netbirdMode}
            onValueChange={(value) =>
              onChange('netbirdMode', value as SystemConfig['netbird_mode'])
            }
          >
            <SelectTrigger id="netbird-mode" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="off">关闭</SelectItem>
              <SelectItem value="host">宿主机</SelectItem>
              <SelectItem value="docker">Docker</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field className="sm:col-span-2">
          <FieldLabel htmlFor="opencli-cdp-endpoint">默认 CDP 地址</FieldLabel>
          <Input
            id="opencli-cdp-endpoint"
            value={values.opencliCdpEndpoint}
            onChange={(event) => onChange('opencliCdpEndpoint', event.target.value)}
            maxLength={2048}
            pattern=".*\S.*"
            aria-describedby="opencli-cdp-endpoint-description"
            required
          />
          <FieldDescription id="opencli-cdp-endpoint-description">不能为空；Agent Pool 未配置时使用此地址。</FieldDescription>
        </Field>

        <Field className="sm:col-span-2">
          <FieldLabel htmlFor="agent-pool-endpoints">Agent Pool 地址</FieldLabel>
          <Input
            id="agent-pool-endpoints"
            value={values.agentPoolEndpoints}
            onChange={(event) => onChange('agentPoolEndpoints', event.target.value)}
            placeholder="使用逗号分隔多个地址"
            maxLength={8192}
            className="font-mono text-xs"
            aria-describedby="agent-pool-endpoints-description"
          />
          <FieldDescription id="agent-pool-endpoints-description">多个地址使用逗号分隔；留空表示不配置 Agent Pool，届时使用默认 CDP。</FieldDescription>
        </Field>
      </div>
    </section>
  )
}

function AutomationSettingsSection({ values, onChange }: SettingsSectionProps) {
  return (
    <section className="space-y-4 border-t pt-6" aria-labelledby="automation-settings-title">
      <div>
        <h3 id="automation-settings-title" className="text-sm font-medium">
          AI 与自动执行
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          设置模型请求资源上限和全局控制策略。
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="llm-request-timeout">模型请求超时（秒）</FieldLabel>
          <Input
            id="llm-request-timeout"
            type="number"
            min={1}
            max={3600}
            value={values.llmRequestTimeoutSeconds}
            onChange={(event) => onChange('llmRequestTimeoutSeconds', event.target.value)}
            aria-describedby="llm-request-timeout-description"
            required
          />
          <FieldDescription id="llm-request-timeout-description">允许范围 1–3600 秒。</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="llm-max-concurrency">模型最大并发数</FieldLabel>
          <Input
            id="llm-max-concurrency"
            type="number"
            min={1}
            max={64}
            value={values.llmMaxConcurrency}
            onChange={(event) => onChange('llmMaxConcurrency', event.target.value)}
            aria-describedby="llm-max-concurrency-description"
            required
          />
          <FieldDescription id="llm-max-concurrency-description">允许范围 1–64。</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="control-mode">控制策略</FieldLabel>
          <Select
            value={values.controlMode}
            onValueChange={(value) =>
              onChange('controlMode', value as SystemConfig['control_mode'])
            }
          >
            <SelectTrigger id="control-mode" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="advisory">建议模式</SelectItem>
              <SelectItem value="automatic">自动模式</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field orientation="horizontal" className="items-center justify-between rounded-lg border p-3">
          <div className="space-y-1">
            <FieldLabel htmlFor="control-kill-switch">暂停自动执行</FieldLabel>
            <FieldDescription id="control-kill-switch-description">开启后，全局阻止自动控制动作；建议模式不受影响。</FieldDescription>
          </div>
          <Switch
            id="control-kill-switch"
            checked={values.controlKillSwitch}
            onCheckedChange={(checked) => onChange('controlKillSwitch', checked)}
            aria-describedby="control-kill-switch-description"
          />
        </Field>
      </div>
    </section>
  )
}

type RuntimeSettingsFormProps = {
  config: SystemConfig
}



function valuesFromConfig(config: SystemConfig): RuntimeSettingsValues {
  return {
    collectionMode: config.collection_mode,
    collectionOrchestrator: config.collection_orchestrator,
    localMaxConcurrentPipelines: String(config.local_max_concurrent_pipelines),
    opencliTimeout: String(config.opencli_timeout),
    defaultTimezone: config.default_timezone,
    publicUrl: config.public_url,
    fleetNetworkProvider: config.fleet_network_provider,
    netbirdMode: config.netbird_mode,
    opencliCdpEndpoint: config.opencli_cdp_endpoint,
    agentPoolEndpoints: config.agent_pool_endpoints.join('\n'),
    llmRequestTimeoutSeconds: String(config.llm_request_timeout_seconds),
    llmMaxConcurrency: String(config.llm_max_concurrency),
    controlMode: config.control_mode,
    controlKillSwitch: config.control_kill_switch,
  }
}

function normalizedEndpoints(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((endpoint) => endpoint.trim())
    .filter(Boolean)
}

function patchFromValues(
  values: RuntimeSettingsValues,
  config: SystemConfig,
): SystemConfigPatch {
  const patch: SystemConfigPatch = {}
  const localMaxConcurrentPipelines = Number(values.localMaxConcurrentPipelines)
  const opencliTimeout = Number(values.opencliTimeout)
  const llmRequestTimeoutSeconds = Number(values.llmRequestTimeoutSeconds)
  const llmMaxConcurrency = Number(values.llmMaxConcurrency)
  const defaultTimezone = values.defaultTimezone.trim()
  const publicUrl = values.publicUrl.trim()
  const opencliCdpEndpoint = values.opencliCdpEndpoint.trim()
  const agentPoolEndpoints = normalizedEndpoints(values.agentPoolEndpoints)

  if (values.collectionMode !== config.collection_mode) {
    patch.collection_mode = values.collectionMode
  }
  if (values.collectionOrchestrator !== config.collection_orchestrator) {
    patch.collection_orchestrator = values.collectionOrchestrator
  }
  if (localMaxConcurrentPipelines !== config.local_max_concurrent_pipelines) {
    patch.local_max_concurrent_pipelines = localMaxConcurrentPipelines
  }
  if (opencliTimeout !== config.opencli_timeout) patch.opencli_timeout = opencliTimeout
  if (defaultTimezone !== config.default_timezone) patch.default_timezone = defaultTimezone
  if (publicUrl !== config.public_url) patch.public_url = publicUrl
  if (values.fleetNetworkProvider !== config.fleet_network_provider) {
    patch.fleet_network_provider = values.fleetNetworkProvider
  }
  if (values.netbirdMode !== config.netbird_mode) patch.netbird_mode = values.netbirdMode
  if (opencliCdpEndpoint !== config.opencli_cdp_endpoint) {
    patch.opencli_cdp_endpoint = opencliCdpEndpoint
  }
  if (agentPoolEndpoints.join(',') !== config.agent_pool_endpoints.join(',')) {
    patch.agent_pool_endpoints = agentPoolEndpoints.join(',')
  }
  if (llmRequestTimeoutSeconds !== config.llm_request_timeout_seconds) {
    patch.llm_request_timeout_seconds = llmRequestTimeoutSeconds
  }
  if (llmMaxConcurrency !== config.llm_max_concurrency) {
    patch.llm_max_concurrency = llmMaxConcurrency
  }
  if (values.controlMode !== config.control_mode) patch.control_mode = values.controlMode
  if (values.controlKillSwitch !== config.control_kill_switch) {
    patch.control_kill_switch = values.controlKillSwitch
  }

  return patch
}

function RuntimeSettingsForm({ config }: RuntimeSettingsFormProps) {
  const [values, setValues] = useState(() => valuesFromConfig(config))
  const updateConfig = useUpdateSystemConfig()
  const patch = patchFromValues(values, config)
  const hasChanges = Object.keys(patch).length > 0

  function setValue<Key extends keyof RuntimeSettingsValues>(
    key: Key,
    value: RuntimeSettingsValues[Key],
  ) {
    setValues((current) => ({ ...current, [key]: value }))
  }

  async function saveSettings(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!hasChanges) return
    if (!values.defaultTimezone.trim() || !values.opencliCdpEndpoint.trim()) {
      toast.error('默认时区和默认 CDP 地址不能为空')
      return
    }

    try {
      const updated = await updateConfig.mutateAsync(patch)
      setValues(valuesFromConfig(updated))
      toast.success('运行配置已保存', {
        description: '当前 API 进程已刷新配置；独立 Worker 或 Agent 可能需要按部署方式重启。',
      })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '运行配置保存失败')
    }
  }

  return (
    <form onSubmit={saveSettings}>
      <Card>
        <CardHeader>
          <CardTitle><h2 id="runtime-settings-title">运行配置</h2></CardTitle>
          <CardDescription>
            这里修改服务端已开放的安全配置字段。保存会写入部署环境文件并刷新当前 API 进程，不代表外部服务已连通。
          </CardDescription>
        </CardHeader>

        <CardContent>
          <fieldset
            className="space-y-7 border-0 p-0"
            disabled={updateConfig.isPending}
            aria-labelledby="runtime-settings-title"
          >
            <legend className="sr-only">运行配置字段</legend>
            <ExecutionSettingsSection values={values} onChange={setValue} />
            <BrowserSettingsSection values={values} onChange={setValue} />
            <AutomationSettingsSection values={values} onChange={setValue} />
          </fieldset>
        </CardContent>

        <CardFooter className="flex flex-wrap justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {hasChanges ? '有尚未保存的更改。' : '当前表单与服务端配置一致。'}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={!hasChanges || updateConfig.isPending}
              onClick={() => setValues(valuesFromConfig(config))}
            >
              放弃更改
            </Button>
            <Button type="submit" disabled={!hasChanges || updateConfig.isPending}>
              {updateConfig.isPending ? '保存中…' : '保存运行配置'}
            </Button>
          </div>
        </CardFooter>
      </Card>
    </form>
  )
}
function AccountSecurityCard() {
  const { changePassword } = useAuth()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [savingPassword, setSavingPassword] = useState(false)

  async function savePassword(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (newPassword !== confirmPassword) {
      toast.error('两次输入的新密码不一致')
      return
    }

    setSavingPassword(true)
    try {
      await changePassword(currentPassword, newPassword)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      toast.success('密码已更新')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '密码更新失败')
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle><h2>管理员账户</h2></CardTitle>
        <CardDescription>
          修改当前本地管理员密码。此操作不依赖运行配置是否成功加载。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="max-w-xl space-y-5" onSubmit={savePassword}>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="current-password">当前密码</FieldLabel>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                required
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="new-password">新密码</FieldLabel>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                minLength={6}
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                required
                aria-describedby="new-password-description"
              />
              <FieldDescription id="new-password-description">至少 6 个字符。</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="confirm-password">确认新密码</FieldLabel>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                minLength={6}
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                required
              />
            </Field>
          </FieldGroup>
          <Button type="submit" disabled={savingPassword}>
            {savingPassword ? '保存中…' : '保存密码'}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

export default function SystemSettingsPage() {
  const config = useSystemConfig()

  return (
    <PageContainer
      eyebrow="SYSTEM"
      title="系统设置"
      description="查看真实部署配置，调整服务端开放的运行参数，并管理管理员账户。"
    >
      {config.isLoading ? (
        <Card>
          <CardHeader>
            <CardTitle><h2>部署与运行配置</h2></CardTitle>
            <CardDescription>正在从服务端读取当前配置。</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingState rows={3} />
          </CardContent>
        </Card>
      ) : config.isError ? (
        <ErrorState message={(config.error as Error)?.message} hint={BACKEND_HINT} />
      ) : config.data ? (
        <div className="space-y-5">
          <SystemConfigOverview config={config.data} />
          <RuntimeSettingsForm config={config.data} />
          <AccountSecurityCard />
          <RestartApiCard />
        </div>
      ) : null}
    </PageContainer>
  )
}
