import type {
  AgentPermissions,
  AdapterBinding,
  WorkflowCapability,
  WorkflowNodeKind,
  WorkflowProfile,
  WorkflowProject,
  WorkflowProjectNode,
} from "./schema"
import { adapterBindingSchema, parseWorkflowProject } from "./schema"
import { getNodeInternals } from "./node-internals"
import {
  backendParameterDefault,
  createBackendParameterInterface,
  createDataOperatorParameterInterface,
  createParameterInterfaceFromInternals,
} from "./parameter-interface"
import {
  catalogRuntimeCapability,
  projectedCatalogRuntimeCapability,
  runtimeContractForCapability,
  type WorkflowCapabilitiesResponse,
  type WorkflowRuntimeIOContract,
  type WorkflowRuntimeCapability,
} from "./capabilities"
import type { WorkflowToolCapability } from "./backend-tool-capabilities"

export type WorkflowNodeCatalogCategory =
  | "trigger"
  | "source"
  | "processing"
  | "flow"
  | "decision"
  | "control"
  | "sink"
  | "output"
  | "media"
  | "package"

export type WorkflowNodeCatalogItem = {
  id: string
  idPrefix: string
  label: string
  description: string
  category: WorkflowNodeCatalogCategory
  profile: WorkflowProfile
  kind: WorkflowNodeKind
  capability: WorkflowCapability
  icon: string
  color: string
  adapter?: string
  requiredAdapters?: AdapterBinding[]
  params: Record<string, unknown>
  topicCollapse?: WorkflowProjectNode["topicCollapse"]
  internals?: WorkflowProjectNode["internals"]
  runtimeCapability?: WorkflowRuntimeCapability
  runtimeContract?: WorkflowRuntimeIOContract
  proposalState?: WorkflowProjectNode["proposalState"]
  agentPermissionPatch?: Partial<AgentPermissions>
  keywords: string[]
}

export const COLLECTION_NEED_CATALOG_ID = "intelligence.input.collection-need"
export const TURBOPUSH_PUBLISH_CATALOG_ID = "intelligence.output.turbopush-publish"
export const RECORD_HYGIENE_PACKAGE_CATALOG_ID = "package.processing.record-hygiene"
export const COLLECTOR_NODE_CATALOG_IDS = [
  "collection.source.web",
  "collection.source.api",
  "collection.source.rss",
  "collection.source.cli",
] as const

export type CollectorSourceKind = "web" | "api" | "rss" | "cli"

type CollectorSourceBase = {
  sourceId: string
  kind: CollectorSourceKind
  name?: string
  enabled?: boolean
}

export type WebCollectorSource = CollectorSourceBase & {
  kind: "web"
  url: string
  fetchMode: "auto" | "http" | "browser"
  selector?: string
  extraction?: Record<string, unknown>
  pagination?: Record<string, unknown>
  timeWindow?: Record<string, unknown>
}

export type ApiCollectorSource = CollectorSourceBase & {
  kind: "api"
  url: string
  method: "GET" | "HEAD"
  credentialRef?: string
  credentialScheme?: "bearer" | "api_key" | "basic"
  query?: Record<string, unknown>
  headers?: Record<string, unknown>
  body?: unknown
  pagination?: Record<string, unknown>
  responseMapping?: Record<string, unknown>
}

export type RssCollectorSource = CollectorSourceBase & {
  kind: "rss"
  feedUrl: string
  timeWindow?: Record<string, unknown>
  itemLimit: number
}

export type CliCollectorSource = CollectorSourceBase & {
  kind: "cli"
  adapterNodeId: string
  args: Record<string, unknown>
}

export type CollectorSourceDefinition =
  | WebCollectorSource
  | ApiCollectorSource
  | RssCollectorSource
  | CliCollectorSource

export type CollectorNodeParams = {
  version: 1
  execution: {
    concurrency?: number
    timeoutMs?: number
    retry?: { maxAttempts: number; backoffMs?: number }
  }
  sources: CollectorSourceDefinition[]
}

export type CollectedItemV1 = {
  itemId: string
  sourceId: string
  sourceType: CollectorSourceKind
  title?: string | null
  url?: string | null
  content?: string | null
  data?: unknown
  publishedAt: string | null
  fetchedAt: string
  lineage: Record<string, unknown>
}

export type CollectorSourceExecutionResult = {
  sourceId: string
  status: "completed" | "failed" | "skipped"
  itemCount: number
  attempts: number
  startedAt: string
  finishedAt: string
  error?: { code: string; message: string; retryable: boolean }
}

export type CollectorOutputV1 = {
  items: CollectedItemV1[]
  sourceResults: CollectorSourceExecutionResult[]
}

export function collectorKindForCatalogId(
  catalogId: unknown,
): CollectorSourceKind | undefined {
  if (typeof catalogId !== "string" || !catalogId.startsWith("collection.source.")) {
    return undefined
  }
  const kind = catalogId.slice("collection.source.".length)
  return COLLECTOR_NODE_CATALOG_IDS.some((id) => id.endsWith(`.${kind}`))
    ? kind as CollectorSourceKind
    : undefined
}

export function isCollectorNodeParams(
  value: unknown,
  expectedKind?: CollectorSourceKind,
): value is CollectorNodeParams {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const params = value as Record<string, unknown>
  if (
    params.version !== 1 ||
    !params.execution ||
    typeof params.execution !== "object" ||
    Array.isArray(params.execution) ||
    !Array.isArray(params.sources)
  ) {
    return false
  }
  return params.sources.every((source) => {
    if (!source || typeof source !== "object" || Array.isArray(source)) return false
    const candidate = source as Record<string, unknown>
    const kind = candidate.kind
    return (
      (kind === "web" || kind === "api" || kind === "rss" || kind === "cli") &&
      (!expectedKind || kind === expectedKind) &&
      typeof candidate.sourceId === "string" &&
      candidate.sourceId.trim().length > 0 &&
      typeof candidate.name === "string" &&
      typeof candidate.enabled === "boolean"
    )
  })
}

export function createCollectorSource(
  kind: CollectorSourceKind,
  sourceId = `source-${Date.now()}`,
): CollectorSourceDefinition {
  const base = {
    sourceId,
    kind,
    name: `新${collectorKindLabel(kind)}来源`,
    enabled: true,
  }
  if (kind === "web") {
    return { ...base, kind, url: "https://example.com", fetchMode: "auto" }
  }
  if (kind === "api") {
    return {
      ...base,
      kind,
      url: "https://api.example.com",
      method: "GET",
      query: {},
      headers: {},
    }
  }
  if (kind === "rss") {
    return {
      ...base,
      kind,
      feedUrl: "https://example.com/feed.xml",
      itemLimit: 20,
    }
  }
  return { ...base, kind, adapterNodeId: "opencli.adapter.example.list", args: {} }
}

function collectorKindLabel(kind: CollectorSourceKind): string {
  if (kind === "web") return "网页"
  if (kind === "api") return "API"
  if (kind === "rss") return "RSS"
  return "CLI"
}

const RECORD_HYGIENE_INTERNALS: NonNullable<WorkflowProjectNode["internals"]> = {
  locked: true,
  nodes: [
    {
      id: "normalize",
      kind: "agent",
      capability: "normalize",
      params: { language: "zh-CN", preserveSourceRefs: true },
      ui: {
        label: "Normalize Items",
        description: "统一字段，记录语言标注并保留来源引用（不翻译内容）",
        icon: "ArrowRightLeft",
        color: "var(--chart-2)",
        catalogId: "intelligence.processing.normalize",
        position: { x: 80, y: 120 },
      },
    },
    {
      id: "dedupe",
      kind: "agent",
      capability: "dedupe",
      params: { key: "title+source+publishedAt", window: "24h" },
      ui: {
        label: "Dedupe Items",
        description: "按稳定业务键和时间窗口去重",
        icon: "Filter",
        color: "var(--chart-2)",
        catalogId: "intelligence.processing.dedupe",
        position: { x: 400, y: 120 },
      },
    },
    {
      id: "record-acceptance",
      kind: "control",
      capability: "accept",
      params: {
        mode: "automatic_with_review",
        schema: "record.v1",
        dedupe: "required",
        lineageRequired: true,
        minQuality: 0,
      },
      ui: {
        label: "Record Acceptance Gate",
        description: "按 schema、质量和 lineage 接收 Record",
        icon: "BadgeCheck",
        color: "var(--chart-3)",
        catalogId: "intelligence.control.record-acceptance",
        position: { x: 720, y: 120 },
      },
    },
  ],
  edges: [
    {
      id: "normalize-dedupe",
      source: "normalize",
      target: "dedupe",
      sourcePort: "out",
      targetPort: "in",
    },
    {
      id: "dedupe-record-acceptance",
      source: "dedupe",
      target: "record-acceptance",
      sourcePort: "out",
      targetPort: "candidates",
    },
  ],
}

const DIFY_COMPATIBLE_WORKFLOW_BLOCKS: WorkflowNodeCatalogItem[] = [
  {
    id: "workflow.start.webhook",
    idPrefix: "webhook-trigger",
    label: "Webhook Trigger",
    description: "通过外部 HTTP 事件启动工作流",
    category: "trigger",
    profile: "intelligence",
    kind: "schedule",
    capability: "trigger",
    icon: "Webhook",
    color: "var(--chart-1)",
    params: { componentType: "webhook-trigger", compatibility: "dify", method: "POST", path: "" },
    keywords: ["webhook", "trigger", "start", "事件", "触发"],
  },
  {
    id: "workflow.block.agent",
    idPrefix: "agent",
    label: "Agent",
    description: "调用具备工具使用能力的 Agent 完成一个任务",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "summarize",
    icon: "Bot",
    color: "var(--chart-2)",
    params: { componentType: "agent", compatibility: "dify", strategy: "function-calling" },
    keywords: ["agent", "tool", "reasoning", "智能体", "工具调用"],
  },
  {
    id: "workflow.block.agent-v2",
    idPrefix: "agent-v2",
    label: "Agent V2",
    description: "使用可扩展策略与工具集运行 Agent",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "summarize",
    icon: "Bot",
    color: "var(--chart-2)",
    params: { componentType: "agent-v2", compatibility: "dify", strategy: "provider" },
    keywords: ["agent", "provider", "strategy", "智能体"],
  },
  {
    id: "workflow.block.llm",
    idPrefix: "llm",
    label: "LLM",
    description: "调用模型完成生成、理解、分类或结构化输出",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "summarize",
    icon: "Sparkles",
    color: "var(--chart-2)",
    params: { componentType: "llm", compatibility: "dify", model: "", prompt: "" },
    keywords: ["llm", "model", "prompt", "模型", "生成"],
  },
  {
    id: "workflow.block.knowledge-retrieval",
    idPrefix: "knowledge-retrieval",
    label: "Knowledge Retrieval",
    description: "从知识库检索与当前问题相关的上下文",
    category: "processing",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "BookOpen",
    color: "var(--chart-4)",
    params: { componentType: "knowledge-retrieval", compatibility: "dify", datasetIds: [] },
    keywords: ["knowledge", "retrieval", "rag", "知识库", "检索"],
  },
  {
    id: "workflow.block.end",
    idPrefix: "end",
    label: "End",
    description: "结束工作流并声明最终输出变量",
    category: "output",
    profile: "intelligence",
    kind: "sink",
    capability: "store",
    icon: "CircleStop",
    color: "var(--chart-4)",
    params: { componentType: "end", compatibility: "dify", outputs: [] },
    keywords: ["end", "output", "finish", "结束", "输出"],
  },
  {
    id: "workflow.block.direct-answer",
    idPrefix: "direct-answer",
    label: "Direct Answer",
    description: "在对话流程中直接返回文本或变量",
    category: "output",
    profile: "intelligence",
    kind: "notify",
    capability: "send",
    icon: "MessageSquareReply",
    color: "var(--chart-1)",
    params: { componentType: "answer", compatibility: "dify", answer: "" },
    keywords: ["answer", "response", "chat", "直接回复", "回答"],
  },
  {
    id: "workflow.block.question-classifier",
    idPrefix: "question-classifier",
    label: "Question Classifier",
    description: "使用模型把输入问题分配到不同分支",
    category: "decision",
    profile: "intelligence",
    kind: "router",
    capability: "route",
    icon: "ListTree",
    color: "var(--chart-5)",
    params: { componentType: "question-classifier", compatibility: "dify", classes: [] },
    keywords: ["question", "classifier", "route", "问题分类", "分支"],
  },
  {
    id: "workflow.block.if-else",
    idPrefix: "if-else",
    label: "IF / ELSE",
    description: "根据条件表达式把数据路由到不同分支",
    category: "decision",
    profile: "intelligence",
    kind: "router",
    capability: "route",
    icon: "GitBranch",
    color: "var(--chart-5)",
    params: { componentType: "if-else", compatibility: "dify", conditions: [] },
    keywords: ["if", "else", "condition", "条件", "分支"],
  },
  {
    id: "workflow.block.exit-loop",
    idPrefix: "exit-loop",
    label: "Exit Loop",
    description: "满足条件时退出当前循环",
    category: "control",
    profile: "intelligence",
    kind: "control",
    capability: "accept",
    icon: "LogOut",
    color: "var(--chart-5)",
    params: { componentType: "exit-loop", compatibility: "dify" },
    keywords: ["exit", "break", "loop", "退出循环"],
  },
  {
    id: "workflow.block.iteration",
    idPrefix: "iteration",
    label: "Iteration",
    description: "对数组中的每个元素执行同一个子流程",
    category: "flow",
    profile: "intelligence",
    kind: "flow",
    capability: "merge",
    icon: "ListRestart",
    color: "var(--chart-5)",
    params: { componentType: "iteration", compatibility: "dify", parallel: false },
    keywords: ["iteration", "foreach", "array", "迭代", "遍历"],
  },
  {
    id: "workflow.block.loop",
    idPrefix: "loop",
    label: "Loop",
    description: "按终止条件重复执行一个子流程",
    category: "flow",
    profile: "intelligence",
    kind: "flow",
    capability: "merge",
    icon: "Repeat2",
    color: "var(--chart-5)",
    params: { componentType: "loop", compatibility: "dify", maxIterations: 10 },
    keywords: ["loop", "repeat", "while", "循环", "重复"],
  },
  {
    id: "workflow.block.code",
    idPrefix: "code",
    label: "Code",
    description: "运行受控代码处理输入并返回结构化结果",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Code2",
    color: "var(--chart-2)",
    params: { componentType: "code", compatibility: "dify", language: "javascript", code: "" },
    keywords: ["code", "javascript", "python", "代码", "脚本"],
  },
  {
    id: "workflow.block.template-transform",
    idPrefix: "template-transform",
    label: "Template Transform",
    description: "使用模板把上游变量转换为文本",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Braces",
    color: "var(--chart-2)",
    params: { componentType: "template-transform", compatibility: "dify", template: "" },
    keywords: ["template", "jinja", "transform", "模板", "转换"],
  },
  {
    id: "workflow.block.variable-aggregator",
    idPrefix: "variable-aggregator",
    label: "Variable Aggregator",
    description: "把多个分支中的变量聚合为统一输出",
    category: "flow",
    profile: "intelligence",
    kind: "flow",
    capability: "merge",
    icon: "GitMerge",
    color: "var(--chart-5)",
    params: { componentType: "variable-aggregator", compatibility: "dify", variables: [] },
    keywords: ["variable", "aggregate", "merge", "变量聚合", "合并"],
  },
  {
    id: "workflow.block.document-extractor",
    idPrefix: "document-extractor",
    label: "Document Extractor",
    description: "从上传的文档中提取可供下游处理的文本",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "FileText",
    color: "var(--chart-2)",
    params: { componentType: "document-extractor", compatibility: "dify", files: [] },
    keywords: ["document", "extract", "file", "文档", "提取"],
  },
  {
    id: "workflow.block.variable-assigner",
    idPrefix: "variable-assigner",
    label: "Variable Assigner",
    description: "在流程上下文中写入或更新变量",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Variable",
    color: "var(--chart-2)",
    params: { componentType: "assigner", compatibility: "dify", assignments: [] },
    keywords: ["variable", "assign", "state", "变量", "赋值"],
  },
  {
    id: "workflow.block.parameter-extractor",
    idPrefix: "parameter-extractor",
    label: "Parameter Extractor",
    description: "使用模型从自然语言中抽取结构化参数",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "ListFilter",
    color: "var(--chart-2)",
    params: { componentType: "parameter-extractor", compatibility: "dify", parameters: [] },
    keywords: ["parameter", "extract", "structured", "参数", "抽取"],
  },
  {
    id: "workflow.block.http-request",
    idPrefix: "http-request",
    label: "HTTP Request",
    description: "在流程中发起可配置的 HTTP 请求",
    category: "processing",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    params: { componentType: "http-request", compatibility: "dify", method: "GET", url: "" },
    keywords: ["http", "request", "api", "网络请求", "接口"],
  },
  {
    id: "workflow.block.list-filter",
    idPrefix: "list-filter",
    label: "List Filter",
    description: "筛选、排序或截断数组数据",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "ListFilter",
    color: "var(--chart-2)",
    params: { componentType: "list-filter", compatibility: "dify", conditions: [], limit: null },
    keywords: ["list", "filter", "sort", "数组", "筛选"],
  },
]
export const IMAGE_GENERATION_CATALOG_ID = "media.image-generation"
export const IMAGE_ASSET_CATALOG_ID = "media.image-asset"

const JIN10_ADAPTER: AdapterBinding = {
  id: "jin10-kuaixun",
  type: "source",
  provider: "jin10",
  mode: "fixture",
  config: { feed: "kuaixun" },
}

const RSS_ADAPTER: AdapterBinding = {
  id: "rss-feed",
  type: "source",
  provider: "rss",
  mode: "live",
  config: { channel: "rss" },
}

const HTTP_ADAPTER: AdapterBinding = {
  id: "http-api",
  type: "source",
  provider: "http",
  mode: "live",
  config: { channel: "http", method: "GET" },
}

const FEISHU_TABLE_ADAPTER: AdapterBinding = {
  id: "feishu-table-source",
  type: "source",
  provider: "feishu",
  mode: "live",
  config: { channel: "feishu_table", channelType: "feishu_table" },
}

const DOUBAO_RESEARCH_ADAPTER: AdapterBinding = {
  id: "doubao-research-source",
  type: "source",
  provider: "doubao",
  mode: "live",
  config: { channel: "doubao_research", channelType: "doubao_research" },
}

const WEBHOOK_NOTIFY_ADAPTER: AdapterBinding = {
  id: "webhook-notifier",
  type: "notification",
  provider: "webhook",
  mode: "webhook",
  config: { notifierType: "webhook", target: "webhook" },
}

const TURBOPUSH_ADAPTER: AdapterBinding = {
  id: "turbopush-local",
  type: "notification",
  provider: "turbopush",
  mode: "live",
  config: { channel: "turbopush", mcpServer: "turbo-push", resourceMode: "auto" },
}

export type OpenCLISourceSlot = {
  id: string
  label: string
  sourceGroup: string
  site: string
  command: string
  args: Record<string, unknown>
  positionalArgs?: string[]
  adapterId?: string
  format?: string
  mode?: string
  profileId?: string
  profileBinding?: string
  sessionPolicy?: string
  workerTags?: string[]
  resourceTags?: string[]
  /** Workspace browser account owning this source session. */
  accountId?: string
  sourceBindingId?: string
  sourceBindingRevisionId?: string
  sourceBindingRevisionNumber?: number
}

export function isOpenCLISourceSlotArray(value: unknown): value is OpenCLISourceSlot[] {
  return Array.isArray(value) && value.every((source) => {
    if (!source || typeof source !== "object" || Array.isArray(source)) return false
    const slot = source as Record<string, unknown>
    return (
      typeof slot.id === "string" &&
      slot.id.trim().length > 0 &&
      typeof slot.label === "string" &&
      typeof slot.sourceGroup === "string" &&
      typeof slot.site === "string" &&
      slot.site.trim().length > 0 &&
      typeof slot.command === "string" &&
      slot.command.trim().length > 0 &&
      !!slot.args &&
      typeof slot.args === "object" &&
      !Array.isArray(slot.args) &&
      (slot.accountId === undefined || typeof slot.accountId === "string") &&
      (slot.sourceBindingId === undefined || typeof slot.sourceBindingId === "string") &&
      (slot.sourceBindingRevisionId === undefined || typeof slot.sourceBindingRevisionId === "string") &&
      (
        slot.sourceBindingRevisionNumber === undefined ||
        (
          typeof slot.sourceBindingRevisionNumber === "number" &&
          Number.isInteger(slot.sourceBindingRevisionNumber) &&
          slot.sourceBindingRevisionNumber >= 1
        )
      )
    )
  })
}

export const DEFAULT_OPENCLI_HDA_SOURCES: OpenCLISourceSlot[] = [
  {
    id: "douyin",
    label: "Douyin Search",
    sourceGroup: "short-video",
    site: "douyin",
    command: "search",
    args: { query: "ai" },
  },
  {
    id: "bilibili",
    label: "Bilibili Search",
    sourceGroup: "video",
    site: "bilibili",
    command: "search",
    args: { limit: 20 },
    positionalArgs: ["ai"],
  },
  {
    id: "xiaohongshu",
    label: "Xiaohongshu Search",
    sourceGroup: "social",
    site: "xiaohongshu",
    command: "search",
    args: {},
    positionalArgs: ["ai"],
  },
  {
    id: "twitter",
    label: "Twitter Search",
    sourceGroup: "social",
    site: "twitter",
    command: "search",
    args: { query: "ai", product: "live" },
  },
]

export function opencliAdaptersForSourceSlots(sources: OpenCLISourceSlot[]): AdapterBinding[] {
  const adapters = sources.map((source) => ({
    id: source.adapterId ?? opencliAdapterId(source.site),
    type: "source" as const,
    provider: "opencli",
    mode: "live" as const,
    config: { channel: "opencli" },
  }))
  return Array.from(new Map(adapters.map((adapter) => [adapter.id, adapter])).values())
}

export function buildOpenCLIMultiSourceHDAInternals(
  sources: OpenCLISourceSlot[],
  options: { exposeRawSourceItems?: boolean } = {},
): WorkflowProjectNode["internals"] {
  const sourceGroups = sources.map((source) => source.sourceGroup || source.site)
  const sourcePoolNode = {
    id: "source-pool",
    kind: "agent" as const,
    capability: "normalize" as const,
    params: { sourceCount: sources.length, sourceGroups, fanout: "parallel" },
    ui: {
      label: "Source Pool",
      description: "Fanout source intent into parallel OpenCLI source slots",
      icon: "Network",
      color: "var(--chart-4)",
      catalogId: "intelligence.source.pool",
      position: { x: 0, y: Math.max(0, ((sources.length - 1) * 150) / 2) },
    },
  }
  const sourceNodes = sources.map((source, index) => ({
    id: opencliSourceNodeId(source),
    kind: "source" as const,
    capability: "fetch" as const,
    adapter: source.adapterId ?? opencliAdapterId(source.site),
    params: {
      site: source.site,
      command: source.command,
      args: source.args,
      ...(source.positionalArgs ? { positionalArgs: source.positionalArgs } : {}),
      sourceGroup: source.sourceGroup,
      ...(source.format ? { format: source.format } : {}),
      ...(source.mode ? { mode: source.mode } : {}),
      ...(source.profileId ? { profileId: source.profileId } : {}),
      ...(source.profileBinding ? { profileBinding: source.profileBinding } : {}),
      ...(source.sessionPolicy ? { sessionPolicy: source.sessionPolicy } : {}),
      ...(source.workerTags ? { workerTags: source.workerTags } : {}),
      ...(source.resourceTags ? { resourceTags: source.resourceTags } : {}),
      ...(source.accountId ? { accountId: source.accountId } : {}),
      ...(source.sourceBindingId ? { sourceBindingId: source.sourceBindingId } : {}),
      ...(source.sourceBindingRevisionId ? { sourceBindingRevisionId: source.sourceBindingRevisionId } : {}),
      ...(source.sourceBindingRevisionNumber
        ? { sourceBindingRevisionNumber: source.sourceBindingRevisionNumber }
        : {}),
    },
    ui: {
      label: source.label,
      description: `${source.site} ${source.command}`,
      icon: "Globe",
      color: "var(--chart-4)",
      catalogId: "intelligence.source.opencli-slot",
      position: { x: 280, y: index * 150 },
    },
  }))
  const midpointY = Math.max(0, ((sourceNodes.length - 1) * 150) / 2)
  const sourcePoolEdges = sourceNodes.map((sourceNode) => ({
    id: `source-pool-${sourceNode.id}`,
    source: "source-pool",
    target: sourceNode.id,
    sourcePort: "out",
    targetPort: "in",
  }))
  if (options.exposeRawSourceItems) {
    return {
      locked: true,
      nodes: [sourcePoolNode, ...sourceNodes],
      edges: sourcePoolEdges,
    }
  }
  const outputNode = {
    id: "collection-output",
    kind: "inbox" as const,
    capability: "store" as const,
    params: { queue: "opencli-hda-output", archive: false },
    ui: {
      label: "Collection Output",
      description: "Expose normalized items as the package output",
      icon: "Inbox",
      color: "var(--chart-4)",
      catalogId: "intelligence.output.collection-result",
      position: { x: 920, y: midpointY },
    },
  }
  return {
    locked: true,
    nodes: [
      sourcePoolNode,
      ...sourceNodes,
      {
        id: "internal-normalize",
        kind: "agent",
        capability: "normalize",
        params: { language: "zh-CN", preserveSourceRefs: true },
        ui: {
          label: "Normalize Items",
          description: "Normalize OpenCLI source slot results",
          icon: "ArrowRightLeft",
          color: "var(--chart-2)",
          catalogId: "intelligence.processing.normalize",
          position: { x: 620, y: midpointY },
        },
      },
      outputNode,
    ],
    edges: [
      ...sourcePoolEdges,
      ...sourceNodes.map((sourceNode) => ({
        id: `${sourceNode.id}-normalize`,
        source: sourceNode.id,
        target: "internal-normalize",
        sourcePort: "out",
        targetPort: "in",
      })),
      {
        id: "internal-normalize-output",
        source: "internal-normalize",
        target: "collection-output",
        sourcePort: "out",
        targetPort: "in",
      },
    ],
  }
}

function buildToolPackageInternals(
  toolId: string,
  executorMode:
    | "situation_awareness"
    | "swarm_simulation"
    | "gaojixing_doubao_batch"
    | "gaojixing_batch_certify",
  label: string,
  toolParams: Record<string, unknown>,
  options: { includeOutput?: boolean } = {},
): WorkflowProjectNode["internals"] {
  const internals: NonNullable<WorkflowProjectNode["internals"]> = {
    locked: true,
    nodes: [
      {
        id: "tool",
        kind: "action",
        capability: "store",
        params: {
          toolCapability: {
            id: toolId,
            executor: { mode: executorMode, params: {} },
          },
          toolParams,
        },
        ui: {
          label,
          description: `${label} internal Tool Capability`,
          icon: executorMode === "situation_awareness" ? "Radar" : "Network",
          color: executorMode === "situation_awareness" ? "var(--chart-2)" : "var(--chart-5)",
          catalogId: "external.tool.capability",
          position: { x: 0, y: 0 },
        },
      },
      {
        id: "output",
        kind: "inbox",
        capability: "store",
        params: { queue: `${executorMode}-output`, archive: false },
        ui: {
          label: `${label} Output`,
          description: "Expose the complete result with workflow lineage",
          icon: "Inbox",
          color: "var(--chart-4)",
          catalogId: "intelligence.output.inbox",
          position: { x: 340, y: 0 },
        },
      },
    ],
    edges: [
      {
        id: "tool-output",
        source: "tool",
        target: "output",
        sourcePort: "out",
        targetPort: "in",
      },
    ],
  }

  return options.includeOutput === false
    ? { ...internals, nodes: [internals.nodes[0]], edges: [] }
    : internals
}

function opencliAdapterId(site: string): string {
  return `opencli-${safeIdPart(site)}`
}

function opencliSourceNodeId(source: OpenCLISourceSlot): string {
  const identity = source.accountId ?? source.sourceBindingRevisionId
  return `source-${safeIdPart(source.id || source.sourceGroup || source.site)}${identity ? `-${safeIdPart(identity)}` : ""}`
}

function safeIdPart(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "source"
}

export const WORKFLOW_NODE_CATALOG: WorkflowNodeCatalogItem[] = [
  ...DIFY_COMPATIBLE_WORKFLOW_BLOCKS,
  {
    id: "collection.source.web",
    idPrefix: "web-collector",
    label: "网页采集",
    description: "从多个网页配置采集内容，并保留逐来源状态与时间信息",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    params: {
      version: 1,
      execution: {
        concurrency: 3,
        timeoutMs: 30_000,
        retry: { maxAttempts: 2, backoffMs: 1_000 },
      },
      sources: [createCollectorSource("web", "web-1")],
    },
    keywords: ["web", "page", "crawler", "网页", "网站", "采集"],
  },
  {
    id: "collection.source.api",
    idPrefix: "api-collector",
    label: "API 采集",
    description: "从多个 HTTP API 采集结构化数据，并保留逐来源状态",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "FileCode2",
    color: "var(--chart-4)",
    params: {
      version: 1,
      execution: {
        concurrency: 3,
        timeoutMs: 30_000,
        retry: { maxAttempts: 2, backoffMs: 1_000 },
      },
      sources: [createCollectorSource("api", "api-1")],
    },
    keywords: ["api", "http", "json", "接口", "采集"],
  },
  {
    id: "collection.source.rss",
    idPrefix: "rss-collector",
    label: "RSS 采集",
    description: "从多个 RSS 或 Atom Feed 采集条目",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Radio",
    color: "var(--chart-4)",
    params: {
      version: 1,
      execution: {
        concurrency: 3,
        timeoutMs: 30_000,
        retry: { maxAttempts: 2, backoffMs: 1_000 },
      },
      sources: [createCollectorSource("rss", "rss-1")],
    },
    keywords: ["rss", "atom", "feed", "订阅", "采集"],
  },
  {
    id: "collection.source.cli",
    idPrefix: "cli-collector",
    label: "CLI 采集",
    description: "从已注册 OpenCLI 工具目录选择只读工具并填写 typed arguments",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Terminal",
    color: "var(--chart-4)",
    params: {
      version: 1,
      execution: {
        concurrency: 3,
        timeoutMs: 30_000,
        retry: { maxAttempts: 2, backoffMs: 1_000 },
      },
      sources: [],
    },
    keywords: ["cli", "opencli", "adapter", "typed args", "命令行", "采集"],
  },
  {
    id: IMAGE_GENERATION_CATALOG_ID,
    idPrefix: "image-generation",
    label: "Image Generation",
    description: "在第一方全屏图像工作台中编辑配方；运行时生成并输出 OpenCLI 资产引用",
    category: "media",
    profile: "intelligence",
    kind: "media",
    capability: "generate",
    icon: "ImagePlus",
    color: "var(--chart-5)",
    params: { canvasDocumentId: "" },
    keywords: ["image", "canvas", "generation", "invoke", "图像", "画布", "生成"],
  },
  {
    id: IMAGE_ASSET_CATALOG_ID,
    idPrefix: "image-asset",
    label: "Image Asset",
    description: "选择工作区已有资产，运行时直接输出固定的 OpenCLI mediaAsset[]",
    category: "media",
    profile: "intelligence",
    kind: "media",
    capability: "fetch",
    icon: "Images",
    color: "var(--chart-4)",
    params: { assetIds: [] },
    keywords: ["image", "asset", "gallery", "图像", "资产", "图库"],
  },
  {
    id: COLLECTION_NEED_CATALOG_ID,
    idPrefix: "collection-need",
    label: "Collection Need",
    description: "用户只输入采集需求，由后端 demand-draft 组装真实节点 patch",
    category: "trigger",
    profile: "intelligence",
    kind: "schedule",
    capability: "trigger",
    icon: "MessageSquare",
    color: "var(--chart-1)",
    params: { text: "抓小红书热帖", locale: "zh-CN", mode: "demand-draft" },
    keywords: ["need", "demand", "input", "manual", "需求", "输入", "采集"],
  },
  {
    id: "intelligence.schedule.cron",
    idPrefix: "schedule",
    label: "Cron Schedule",
    description: "按 cron/interval 周期触发情报工作流",
    category: "trigger",
    profile: "intelligence",
    kind: "schedule",
    capability: "trigger",
    icon: "Clock",
    color: "var(--chart-1)",
    params: { interval: "5m", timezone: "Asia/Shanghai" },
    keywords: ["schedule", "cron", "hourly", "daily", "定时", "触发"],
  },
  {
    id: "intelligence.source.jin10",
    idPrefix: "source-jin10",
    label: "JIN10 Source",
    description: "读取金十快讯 fixture/live feed",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    adapter: JIN10_ADAPTER.id,
    requiredAdapters: [JIN10_ADAPTER],
    params: { limit: 20, importantOnly: false, channel: "kuaixun" },
    keywords: ["jin10", "金十", "source", "news", "kuaixun", "fetch"],
  },
  {
    id: "intelligence.source.rss",
    idPrefix: "source-rss",
    label: "RSS / Atom Reader",
    description: "读取已有的 RSS 或 Atom 地址，并以 sourceGroup 保留来源分组和血缘",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Rss",
    color: "var(--chart-4)",
    adapter: RSS_ADAPTER.id,
    requiredAdapters: [RSS_ADAPTER],
    params: {
      feedUrl: "https://www.federalreserve.gov/feeds/press_all.xml",
      maxEntries: 20,
      sourceGroup: "macro-policy",
      site: "federal-reserve",
    },
    keywords: ["rss", "atom", "reader", "feed", "finance", "news", "财经", "订阅", "数据源", "读取"],
  },
  {
    id: "intelligence.source.rsshub",
    idPrefix: "source-rsshub",
    label: "RSSHub Reader",
    description: "通过受管 RSSHub Provider 和 route 生成订阅源，再输出标准 RSS 条目",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Rss",
    color: "var(--chart-4)",
    adapter: RSS_ADAPTER.id,
    requiredAdapters: [RSS_ADAPTER],
    params: {
      providerId: "",
      generatorType: "rsshub",
      route: "",
      routeParameters: {},
      generatorSelection: { route: "", parameters: {} },
      maxEntries: 20,
      sourceGroup: "rsshub",
      site: "rsshub",
    },
    keywords: ["rsshub", "rss hub", "reader", "route", "feed", "订阅生成", "路由", "读取"],
  },
  {
    id: "intelligence.source.rss-bridge",
    idPrefix: "source-rss-bridge",
    label: "RSS-Bridge Reader",
    description: "通过受管 RSS-Bridge Provider 为缺少订阅源的网页生成并读取 RSS",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Rss",
    color: "var(--chart-4)",
    adapter: RSS_ADAPTER.id,
    requiredAdapters: [RSS_ADAPTER],
    params: {
      providerId: "",
      generatorType: "rss_bridge",
      bridge: "",
      bridgeParameters: {},
      generatorSelection: { bridge: "", parameters: {} },
      maxEntries: 20,
      sourceGroup: "rss-bridge",
      site: "rss-bridge",
    },
    keywords: ["rss-bridge", "rss bridge", "reader", "bridge", "feed", "网页订阅", "读取"],
  },
  {
    id: "intelligence.source.http",
    idPrefix: "source-http",
    label: "HTTP / API Reader",
    description: "通过受控 GET/POST 网络请求读取 JSON API，作为可与其他来源并行组合的原子节点",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    adapter: HTTP_ADAPTER.id,
    requiredAdapters: [HTTP_ADAPTER],
    params: {
      url: "",
      method: "GET",
      resultPath: "",
      headers: {},
      query: {},
      sourceGroup: "http-api",
      site: "http-api",
    },
    keywords: ["http", "https", "api", "rest", "json", "reader", "network", "网络", "接口", "读取"],
  },
  {
    id: "intelligence.source.feishu-table",
    idPrefix: "source-feishu-table",
    label: "Feishu Bitable Keywords",
    description: "只读读取飞书多维表格中的搜索词，输出带 Feishu 行引用的 items[]",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Table2",
    color: "var(--chart-4)",
    adapter: FEISHU_TABLE_ADAPTER.id,
    requiredAdapters: [FEISHU_TABLE_ADAPTER],
    params: {
      sourceId: "",
      app_token: "",
      table_id: "",
      keyword_field: "关键词",
      status_field: "状态",
      eligible_status: "待采集",
      max_rows: 500,
      source_group: "feishu-keywords",
      provider: "feishu",
      channelType: "feishu_table",
    },
    keywords: ["feishu", "lark", "bitable", "table", "keyword", "飞书", "多维表格", "关键词", "数据源"],
  },
  {
    id: "intelligence.source.doubao-research",
    idPrefix: "source-doubao-research",
    label: "Doubao Research",
    description: "把上游关键词转换成豆包研究问题，保留引用和问题血缘",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Bot",
    color: "var(--chart-2)",
    adapter: DOUBAO_RESEARCH_ADAPTER.id,
    requiredAdapters: [DOUBAO_RESEARCH_ADAPTER],
    params: { question: "", site_session: "ephemeral", provider: "doubao", channelType: "doubao_research" },
    keywords: ["doubao", "research", "ai", "豆包", "研究", "分析", "问题"],
  },
  {
    id: "intelligence.processing.normalize",
    idPrefix: "normalize",
    label: "Normalize Items",
    description: "统一字段与时间格式，并记录语言标注（不翻译内容）",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "ArrowRightLeft",
    color: "var(--chart-2)",
    params: { language: "zh-CN", preserveSourceRefs: true },
    keywords: ["normalize", "clean", "format", "标准化", "清洗"],
  },
  {
    id: "intelligence.processing.dedupe",
    idPrefix: "dedupe",
    label: "Dedupe Items",
    description: "按标题、时间和来源去重",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "dedupe",
    icon: "Filter",
    color: "var(--chart-2)",
    params: { key: "title+source+publishedAt", window: "24h" },
    keywords: ["dedupe", "duplicate", "去重", "重复"],
  },
  {
    id: "intelligence.data.generate",
    idPrefix: "data-generate",
    label: "Generate Data",
    description: "从后端 Data Operator Pack 选择生成节点，用于 chunk、QA 和训练数据生成",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "WandSparkles",
    color: "var(--chart-2)",
    params: { operatorId: "core.generate.instruction-pairs", packVersion: "1.0.0", config: {} },
    keywords: ["data", "generate", "chunk", "qa", "生成", "切块"],
  },
  {
    id: "intelligence.data.filter",
    idPrefix: "data-filter",
    label: "Filter Data",
    description: "从后端 Data Operator Pack 选择规则过滤、质量过滤或去重节点",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Filter",
    color: "var(--chart-2)",
    params: { operatorId: "core.filter.quality", packVersion: "1.0.0", config: {} },
    keywords: ["data", "filter", "quality", "deduplicate", "过滤", "去重"],
  },
  {
    id: "intelligence.data.evaluate",
    idPrefix: "data-evaluate",
    label: "Evaluate Data",
    description: "从后端 Data Operator Pack 选择质量和统计评估节点",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "ClipboardCheck",
    color: "var(--chart-3)",
    params: { operatorId: "core.evaluate.quality", packVersion: "1.0.0", config: {} },
    keywords: ["data", "evaluate", "statistics", "quality", "评估", "统计"],
  },
  {
    id: "intelligence.data.refine",
    idPrefix: "data-refine",
    label: "Refine Data",
    description: "从后端 Data Operator Pack 选择清洗、投影和训练格式转换节点",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "PencilLine",
    color: "var(--chart-3)",
    params: { operatorId: "core.refine.text", packVersion: "1.0.0", config: {} },
    keywords: ["data", "refine", "clean", "project", "format", "清洗", "转换"],
  },
  {
    id: "intelligence.flow.merge",
    idPrefix: "merge",
    label: "Merge",
    description: "Houdini-style typed fan-in，合并多路候选流并保留 lineage",
    category: "flow",
    profile: "intelligence",
    kind: "flow",
    capability: "merge",
    icon: "GitMerge",
    color: "var(--chart-5)",
    params: {
      strategy: "concat",
      preserveLineage: true,
      inputType: "recordCandidate[]",
      outputType: "recordCandidate[]",
    },
    keywords: ["merge", "join", "fan-in", "lineage", "合并", "汇流"],
  },
  {
    id: "intelligence.agent.summary",
    idPrefix: "summary",
    label: "LLM Summary",
    description: "生成短摘要和影响解释",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "summarize",
    icon: "Sparkles",
    color: "var(--chart-2)",
    params: { model: "deepseek", style: "macro-brief", maxChars: 280 },
    keywords: ["deepseek", "gpt", "claude", "llm", "agent", "summary", "摘要"],
  },
  {
    id: "intelligence.agent.score",
    idPrefix: "score",
    label: "Importance Score",
    description: "按影响范围和紧急度打分",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "score",
    icon: "Sigma",
    color: "var(--chart-3)",
    params: { threshold: 0.7, dimensions: ["market", "policy", "urgency"] },
    keywords: ["score", "rating", "importance", "打分", "重要性"],
  },
  {
    id: "intelligence.agent.tag",
    idPrefix: "tag",
    label: "Auto Tag",
    description: "给条目打主题、市场和风险标签",
    category: "processing",
    profile: "intelligence",
    kind: "agent",
    capability: "tag",
    icon: "Code",
    color: "var(--chart-3)",
    params: { taxonomy: ["macro", "fx", "commodity", "policy", "risk"] },
    keywords: ["tag", "label", "topic", "标签", "分类"],
  },
  {
    id: "intelligence.router.importance",
    idPrefix: "router-importance",
    label: "Importance Router",
    description: "按分数和条件路由到 Inbox/Notify",
    category: "decision",
    profile: "intelligence",
    kind: "router",
    capability: "route",
    icon: "GitBranch",
    color: "var(--chart-5)",
    params: { expression: "item.important === true || item.score >= 0.7" },
    keywords: ["score", "router", "condition", "threshold", "路由", "阈值"],
  },
  {
    id: "intelligence.control.record-acceptance",
    idPrefix: "record-acceptance",
    label: "Record Acceptance Gate",
    description: "把 Record Candidate 通过 schema、去重、质量和 lineage 检查后接收为 Record",
    category: "control",
    profile: "intelligence",
    kind: "control",
    capability: "accept",
    icon: "BadgeCheck",
    color: "var(--chart-3)",
    params: {
      mode: "automatic_with_review",
      schema: "record.v1",
      dedupe: "required",
      lineageRequired: true,
      minQuality: 0,
    },
    keywords: ["record", "acceptance", "gate", "quality", "lineage", "入库", "审核"],
  },
  {
    id: RECORD_HYGIENE_PACKAGE_CATALOG_ID,
    idPrefix: "pkg-record-hygiene",
    label: "Record Hygiene & Acceptance",
    description: "默认清洗管线：标准化、去重并通过 Record Acceptance Gate 准入",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "ShieldCheck",
    color: "var(--chart-2)",
    params: {
      template: "record-hygiene",
      lockedInternals: true,
      language: "zh-CN",
      preserveSourceRefs: true,
      key: "title+source+publishedAt",
      window: "24h",
      mode: "automatic_with_review",
      schema: "record.v1",
      lineageRequired: true,
      minQuality: 0,
    },
    topicCollapse: {
      groupId: "record-hygiene-package",
      nodeCount: 3,
      mode: "locked",
      packageInternal: true,
    },
    internals: RECORD_HYGIENE_INTERNALS,
    keywords: [
      "package",
      "record hygiene",
      "normalize",
      "dedupe",
      "acceptance",
      "cleaning",
      "记录清洗",
      "准入",
      "标准化",
      "去重",
    ],
  },
  {
    id: "intelligence.output.inbox",
    idPrefix: "inbox",
    label: "Inbox Store",
    description: "保存到人工复核队列",
    category: "output",
    profile: "intelligence",
    kind: "inbox",
    capability: "store",
    icon: "Inbox",
    color: "var(--chart-4)",
    params: { queue: "macro-watch", archive: true },
    keywords: ["inbox", "store", "cache", "archive", "收件箱", "归档"],
  },
  {
    id: "intelligence.sink.records",
    idPrefix: "record-sink",
    label: "Record Sink",
    description: "把已接收的 Record 写入 records 系统，保留 lineage 和 run trace 指针",
    category: "sink",
    profile: "intelligence",
    kind: "sink",
    capability: "store",
    icon: "Database",
    color: "var(--chart-4)",
    params: { target: "records", writeMode: "append", preserveLineage: true },
    keywords: ["record", "sink", "database", "records", "落库", "存储"],
  },
  {
    id: "intelligence.sink.feishu-bitable",
    idPrefix: "feishu-bitable",
    label: "Feishu Bitable Sink",
    description: "把已入库且通过认证的 Record 幂等投递到已有飞书多维表格",
    category: "sink",
    profile: "intelligence",
    kind: "sink",
    capability: "store",
    icon: "Database",
    color: "var(--chart-4)",
    params: {
      connectionId: "",
      appToken: "",
      tableId: "",
      fieldMap: {
        recordId: "Record ID",
        workflowRunId: "Run ID",
        evidenceDigest: "Evidence Digest",
        "normalizedData.title": "Title",
      },
    },
    keywords: ["feishu", "bitable", "飞书", "多维表格", "delivery", "sink"],
  },
  {
    id: "intelligence.output.webhook",
    idPrefix: "notify",
    label: "Webhook Notify",
    description: "通过后端 guarded webhook notifier 发送工作流通知",
    category: "output",
    profile: "intelligence",
    kind: "notify",
    capability: "send",
    icon: "Bell",
    color: "var(--chart-1)",
    adapter: WEBHOOK_NOTIFY_ADAPTER.id,
    requiredAdapters: [WEBHOOK_NOTIFY_ADAPTER],
    params: { template: "brief", target: "webhook" },
    keywords: ["feishu", "wecom", "tg", "telegram", "qq", "notify", "webhook", "通知"],
  },
  {
    id: TURBOPUSH_PUBLISH_CATALOG_ID,
    idPrefix: "turbopush-publish",
    label: "TurboPush Publish",
    description: "通过本机 TurboPush 服务发布文章/图文/视频到已登录平台账号",
    category: "output",
    profile: "intelligence",
    kind: "notify",
    capability: "send",
    icon: "Send",
    color: "var(--state-action)",
    adapter: TURBOPUSH_ADAPTER.id,
    requiredAdapters: [TURBOPUSH_ADAPTER],
    params: {
      contentType: "graph_text",
      contentSource: "upstream",
      title: "{{item.title}}",
      markdown: "{{item.markdown}}",
      desc: "{{item.summary}}",
      files: [],
      thumb: [],
      targetPlatforms: ["xiaohongshu"],
      accountSelector: "logged_accounts_by_platform",
      platformSettings: {},
      syncDraft: false,
    },
    keywords: [
      "turbopush",
      "publish",
      "send",
      "wechat",
      "douyin",
      "xiaohongshu",
      "youtube",
      "bilibili",
      "多平台",
      "发布",
      "发送",
    ],
  },
  {
    id: "package.collection.pipeline",
    idPrefix: "pkg-collection",
    label: "Collection Pipeline",
    description: "封装调度触发、多源采集（JIN10/RSS/HTTP）、标准化、去重和富化的采集管线",
    category: "package",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    adapter: JIN10_ADAPTER.id,
    requiredAdapters: [JIN10_ADAPTER],
    params: { template: "collection-pipeline", runtime: "fixture", lockedInternals: true },
    keywords: ["package", "collection", "source", "rss", "http", "采集", "封装"],
  },
  {
    id: "intelligence.source.pool",
    idPrefix: "source-pool",
    label: "Source Pool",
    description: "把业务来源组展开为并行 source slots，资源由 runtime resolver 隐式处理",
    category: "source",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Network",
    color: "var(--chart-4)",
    params: { sourceCount: 2, sourceGroups: ["video", "social"], fanout: "parallel" },
    keywords: ["source", "pool", "fanout", "registry", "来源池", "数据源"],
  },
  {
    id: "intelligence.source.opencli-slot",
    idPrefix: "source-opencli",
    label: "OpenCLI Source Slot",
    description: "一个由 HDA/source planner 生成的 OpenCLI source 槽位，运行时交给 OpenCLI channel 执行",
    category: "source",
    profile: "intelligence",
    kind: "source",
    capability: "fetch",
    icon: "Globe",
    color: "var(--chart-4)",
    params: { site: "bilibili", command: "search", sourceGroup: "video", args: { keyword: "ai" } },
    keywords: ["opencli", "source", "slot", "bilibili", "xiaohongshu", "adapter", "来源槽"],
  },
  {
    id: "intelligence.output.collection-result",
    idPrefix: "collection-output",
    label: "Collection Output",
    description: "把 HDA 内部标准化结果暴露为可审计的 items[] 输出",
    category: "output",
    profile: "intelligence",
    kind: "inbox",
    capability: "store",
    icon: "Inbox",
    color: "var(--chart-4)",
    params: { queue: "opencli-hda-output", archive: false },
    keywords: ["output", "items", "collection", "result", "采集输出", "结果"],
  },
  {
    id: "package.opencli.multi-source-hda",
    idPrefix: "pkg-opencli-hda",
    label: "多站点数据采集",
    description: "从选定网站并行采集数据，并整理为可审查、可追溯的结果",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Network",
    color: "var(--chart-4)",
    requiredAdapters: opencliAdaptersForSourceSlots(DEFAULT_OPENCLI_HDA_SOURCES),
    params: {
      template: "opencli-multi-source",
      runtime: "iii",
      lockedInternals: true,
      execution: {
        fanout: "parallel",
      },
      sources: DEFAULT_OPENCLI_HDA_SOURCES,
      aiCallable: {
        schema: "opencli.multi_source_hda.v1",
        editable: ["sources", "sources[].args"],
        sourceMode: "parallel",
      },
    },
    topicCollapse: {
      groupId: "opencli-package",
      nodeCount: DEFAULT_OPENCLI_HDA_SOURCES.length + 3,
      mode: "locked",
      packageInternal: true,
    },
    internals: buildOpenCLIMultiSourceHDAInternals(DEFAULT_OPENCLI_HDA_SOURCES),
    keywords: ["package", "hda", "opencli", "bilibili", "xiaohongshu", "multi-source", "采集", "封装"],
  },
  {
    id: "package.intelligence.situation-awareness",
    idPrefix: "pkg-situation",
    label: "近 30 天事态感知",
    description: "独立研究能力：严格时间窗、去重、主题聚合、基线对比、异常信号和证据简报",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Radar",
    color: "var(--chart-2)",
    params: {
      template: "situation-awareness",
      runtime: "iii",
      lockedInternals: true,
      provider: "opencli-native",
      query: "人工智能",
      windowDays: 30,
      baselineDays: 30,
      includeUnknownDates: false,
      topK: 10,
    },
    topicCollapse: {
      groupId: "situation-awareness-package",
      nodeCount: 2,
      mode: "locked",
      packageInternal: true,
    },
    internals: buildToolPackageInternals(
      "tool.intelligence.situation-awareness",
      "situation_awareness",
      "近 30 天事态感知",
      {
        provider: "opencli-native",
        query: "人工智能",
        windowDays: 30,
        baselineDays: 30,
        includeUnknownDates: false,
        topK: 10,
      },
    ),
    keywords: ["last30days", "research", "situation", "awareness", "事态感知", "近30天", "研究"],
  },
  {
    id: "package.gaojixing.doubao-batch",
    idPrefix: "pkg-gaojixing-doubao",
    label: "豆包证据批次采集",
    description: "每次运行接收一个新题包，冻结批次快照并自动计算非品牌题与品牌题数量；先完成本批全部非品牌题，再进入品牌题。支持离线夹具或现有规范 2.2 归档；live_preflight 是独立的只读就绪检查，不产生批次结果，也不进入本模板终审；验证异常恢复通知仅在通知权限与 feishuWebhookEnv 同时满足时发送",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "SearchCheck",
    color: "var(--chart-2)",
    params: {
      template: "gaojixing-doubao-batch",
      runtime: "iii",
      lockedInternals: true,
      sourceMode: "project_archive",
      requirePhase1BeforePhase2: true,
      feishuWebhookEnv: "GAOJIXING_FEISHU_WEBHOOK_URL",
    },
    topicCollapse: {
      groupId: "gaojixing-doubao-batch-package",
      nodeCount: 1,
      mode: "locked",
      packageInternal: true,
    },
    internals: buildToolPackageInternals(
      "tool.gaojixing.doubao-batch.run",
      "gaojixing_doubao_batch",
      "豆包证据批次采集",
      {
        sourceMode: "project_archive",
        requirePhase1BeforePhase2: true,
        feishuWebhookEnv: "GAOJIXING_FEISHU_WEBHOOK_URL",
      },
      { includeOutput: false },
    ),
    keywords: ["高吉星", "豆包", "evidence", "batch", "一题一审", "checkpoint", "HDA"],
  },
  {
    id: "package.gaojixing.batch-certification",
    idPrefix: "pkg-gaojixing-certify",
    label: "批次证据结构终审与交付",
    description: "对 raw、Markdown、进度日志和证据文件执行证据结构终审，核对截图等文件存在、命名及引用一致性，并核对参考资料数量、视频记录和高吉星观察字段，输出可审计交付报告；不执行截图视觉或 OCR 内容判定",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "BadgeCheck",
    color: "var(--chart-3)",
    params: {
      template: "gaojixing-batch-certification",
      runtime: "iii",
      lockedInternals: true,
      sourceMode: "project_archive",
    },
    topicCollapse: {
      groupId: "gaojixing-batch-certification-package",
      nodeCount: 1,
      mode: "locked",
      packageInternal: true,
    },
    internals: buildToolPackageInternals(
      "tool.gaojixing.batch-certify",
      "gaojixing_batch_certify",
      "批次证据结构终审与交付",
      {
        sourceMode: "project_archive",
      },
      { includeOutput: false },
    ),
    keywords: ["高吉星", "证据结构终审", "certification", "raw", "Markdown", "截图", "HDA"],
  },
  {
    id: "package.intelligence.native-lifecycle",
    idPrefix: "pkg-native-intelligence",
    label: "采集研究与报告",
    description: "采集多平台证据，完成关系构建、推演、访谈、报告与问答；默认离线数据可直接运行",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Brain",
    color: "var(--chart-3)",
    params: {
      template: "native-intelligence-lifecycle",
      runtime: "iii",
      lockedInternals: true,
      offline: true,
      credentialFree: true,
      sourceMode: "offline_fixture",
      fixtureId: "native-intelligence-offline-v1",
    },
    topicCollapse: {
      groupId: "native-intelligence-lifecycle-package",
      nodeCount: 21,
      mode: "locked",
      packageInternal: true,
    },
    keywords: [
      "native",
      "intelligence",
      "offline",
      "research",
      "ontology",
      "graph",
      "simulation",
      "interview",
      "report",
      "qa",
    ],
  },
  {
    id: "package.simulation.swarm-forecast",
    idPrefix: "pkg-swarm",
    label: "群体智能推演",
    description: "独立推演能力：本地可复现模拟或固定版本 MiroFish provider，输出模拟轨迹和报告",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Network",
    color: "var(--chart-5)",
    params: {
      template: "swarm-forecast",
      runtime: "iii",
      lockedInternals: true,
      provider: "local",
      requirement: "推演事态在不同群体中的传播、立场变化和可能结果",
      agentCount: 12,
      maxRounds: 8,
      platforms: ["twitter", "reddit"],
      enableGraphMemoryUpdate: false,
    },
    topicCollapse: {
      groupId: "swarm-forecast-package",
      nodeCount: 2,
      mode: "locked",
      packageInternal: true,
    },
    internals: buildToolPackageInternals(
      "tool.simulation.swarm-forecast",
      "swarm_simulation",
      "群体智能推演",
      {
        provider: "local",
        requirement: "推演事态在不同群体中的传播、立场变化和可能结果",
        agentCount: 12,
        maxRounds: 8,
        platforms: ["twitter", "reddit"],
        enableGraphMemoryUpdate: false,
      },
    ),
    keywords: ["mirofish", "swarm", "simulation", "forecast", "群体智能", "推演", "模拟"],
  },
  {
    id: "package.dispatch.fanout",
    idPrefix: "pkg-dispatch",
    label: "Dispatch Fanout",
    description: "封装重要性路由、限流和 Webhook/Telegram/邮件多通道发送与 Postgres 存档",
    category: "package",
    profile: "intelligence",
    kind: "notify",
    capability: "send",
    icon: "Bell",
    color: "var(--chart-1)",
    adapter: WEBHOOK_NOTIFY_ADAPTER.id,
    requiredAdapters: [WEBHOOK_NOTIFY_ADAPTER],
    params: { template: "dispatch-fanout", runtime: "mock", lockedInternals: true },
    keywords: ["package", "dispatch", "fanout", "telegram", "email", "发送", "分发", "封装"],
  },
  {
    id: "package.intelligence.pipeline",
    idPrefix: "pkg-intelligence",
    label: "Intelligence Pipeline",
    description: "封装定时抓取、标准化、摘要评分、复核和通知的情报流水线",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "normalize",
    icon: "Network",
    color: "var(--chart-2)",
    params: { template: "jin10-intelligence", runtime: "fixture", lockedInternals: true },
    keywords: ["package", "dop", "intelligence", "pipeline", "情报", "封装"],
  },
  {
    id: "package.ops.event",
    idPrefix: "pkg-ops-event",
    label: "Ops Event",
    description: "封装触发、队列、重试、日志和执行证据的任务事件",
    category: "package",
    profile: "intelligence",
    kind: "action",
    capability: "send",
    icon: "ServerCog",
    color: "var(--chart-4)",
    params: { template: "ops-event", runtime: "template", lockedInternals: true },
    keywords: ["package", "ops", "event", "job", "automation", "任务"],
  },
  {
    id: "package.ops.monitor-guard",
    idPrefix: "pkg-monitor",
    label: "Monitor Guard",
    description: "封装指标采集、阈值、delta 和限流的监控闸门",
    category: "package",
    profile: "intelligence",
    kind: "router",
    capability: "route",
    icon: "Activity",
    color: "var(--chart-4)",
    params: { template: "monitor-guard", runtime: "template", lockedInternals: true },
    keywords: ["package", "monitor", "guard", "metric", "alert", "监控"],
  },
  {
    id: "package.ops.alert-response",
    idPrefix: "pkg-alert",
    label: "Alert Response",
    description: "封装告警分派、通知、工单、快照和升级动作",
    category: "package",
    profile: "intelligence",
    kind: "notify",
    capability: "send",
    icon: "Bell",
    color: "var(--chart-1)",
    params: { template: "alert-response", runtime: "template", lockedInternals: true },
    keywords: ["package", "alert", "response", "ticket", "snapshot", "告警"],
  },
  {
    id: "package.ai.prompt-experiment",
    idPrefix: "pkg-prompt-exp",
    label: "Prompt Experiment",
    description: "封装 prompt 版本、测试用例、模型对比和实验记录",
    category: "package",
    profile: "intelligence",
    kind: "agent",
    capability: "summarize",
    icon: "FlaskConical",
    color: "var(--state-action)",
    params: { template: "prompt-experiment", runtime: "mock", lockedInternals: true },
    keywords: ["package", "prompt", "experiment", "model", "eval", "实验"],
  },
  {
    id: "package.verify.regression-gate",
    idPrefix: "pkg-regression",
    label: "Regression Gate",
    description: "封装 dataset、evaluator、scorecard 和回归门禁",
    category: "package",
    profile: "intelligence",
    kind: "router",
    capability: "route",
    icon: "ShieldCheck",
    color: "#4ade80",
    params: { template: "regression-gate", runtime: "mock", lockedInternals: true },
    keywords: ["package", "regression", "scorecard", "coverage", "gate", "回归"],
  },
  {
    id: "package.map.knowledge-map",
    idPrefix: "pkg-knowledge-map",
    label: "Knowledge Map",
    description: "封装来源锚点、语义连线、主题折叠和知识导出",
    category: "package",
    profile: "intelligence",
    kind: "action",
    capability: "store",
    icon: "Network",
    color: "var(--chart-3)",
    params: { template: "knowledge-map", runtime: "template", lockedInternals: true },
    keywords: ["package", "knowledge", "map", "turnmap", "obsidian", "知识图"],
  },
  {
    id: "package.review.human-review",
    idPrefix: "pkg-human-review",
    label: "Human Review",
    description: "封装人工审核、Inbox、审批分支和审计证据",
    category: "package",
    profile: "intelligence",
    kind: "inbox",
    capability: "store",
    icon: "Inbox",
    color: "var(--chart-4)",
    params: { template: "human-review", runtime: "template", lockedInternals: true },
    keywords: ["package", "human", "review", "approval", "inbox", "人工"],
  },
]

export function nativeIntelligenceCatalogItems(
  tools: WorkflowToolCapability[],
): WorkflowNodeCatalogItem[] {
  return tools.map((tool) => {
    const action =
      typeof tool.executor.params?.action === "string"
        ? tool.executor.params.action
        : tool.id.replace("tool.intelligence.native.", "")
    const runtimeContract =
      tool.manifest.runtimeContract &&
      typeof tool.manifest.runtimeContract === "object"
        ? (tool.manifest.runtimeContract as WorkflowRuntimeIOContract)
        : undefined
    const readiness =
      tool.manifest.readiness && typeof tool.manifest.readiness === "object"
        ? (tool.manifest.readiness as Record<string, unknown>)
        : {}
    const missing = Array.isArray(readiness.missingReasons)
      ? readiness.missingReasons.filter((value): value is string => typeof value === "string")
      : []
    return {
      id: `intelligence.native.${action}`,
      idPrefix: `native-${action.replaceAll(".", "-")}`,
      label: tool.label,
      description:
        tool.description ??
        `Native intelligence action ${action} with durable provenance and limits.`,
      category: action === "close" || action === "cancel" ? "control" : "processing",
      profile: "intelligence",
      kind: "action",
      capability: "store",
      icon: action.startsWith("report")
        ? "FileText"
        : action.startsWith("simulation")
          ? "Network"
          : action.startsWith("interviews")
            ? "MessageSquare"
            : "Brain",
      color: "var(--chart-3)",
      params: {
        toolCapability: {
          id: tool.id,
          executor: {
            mode: tool.executor.mode,
            params: tool.executor.params ?? { action },
          },
        },
        toolParams: {},
      },
      runtimeCapability: {
        id: `resource.tool-capability.${tool.id}`,
        label: tool.label,
        surface: "resource",
        status: tool.status,
        backendAvailable: tool.status === "runnable",
        kind: "action",
        capability: "store",
        provider: tool.provider,
        runtimeBinding: "workflow.external-tool.capability",
        reason: tool.description,
        missing,
        tags: tool.tags,
        source: "backend.workflow.tool_capabilities",
        manifest: tool.manifest,
      },
      runtimeContract,
      keywords: ["native", "intelligence", "offline", action, ...tool.tags],
    }
  })
}

export function getWorkflowNodeCatalog(
  profile: WorkflowProfile,
  capabilities?: WorkflowCapabilitiesResponse | null,
): WorkflowNodeCatalogItem[] {
  const collectorsEnabled = process.env.NEXT_PUBLIC_COLLECTION_L1_NODES !== "false"
  const staticCatalog = WORKFLOW_NODE_CATALOG
    .filter((item) => item.profile === profile)
    .filter((item) => collectorsEnabled || !COLLECTOR_NODE_CATALOG_IDS.includes(item.id as typeof COLLECTOR_NODE_CATALOG_IDS[number]))
    .map((item) => {
    const runtimeCapability = projectedCatalogRuntimeCapability(
      catalogRuntimeCapability(capabilities, item.id),
      item,
      Boolean(capabilities),
    )
    return {
      ...item,
      runtimeCapability,
      runtimeContract: runtimeContractForCapability(runtimeCapability),
    }
  })
  if (profile !== "intelligence") return staticCatalog
  const dynamicBackendCatalog = (capabilities?.catalog ?? []).flatMap((runtimeCapability) => {
    const item = backendNodeCatalogItem(runtimeCapability)
    return item && (
      collectorsEnabled ||
      !COLLECTOR_NODE_CATALOG_IDS.includes(item.id as typeof COLLECTOR_NODE_CATALOG_IDS[number])
    )
      ? [item]
      : []
  })
  const catalogById = new Map<string, WorkflowNodeCatalogItem>(
    staticCatalog.map((item) => [item.id, item]),
  )
  for (const item of dynamicBackendCatalog) catalogById.set(item.id, item)
  return [...catalogById.values()]
}

export function workflowCatalogItemLocked(item: WorkflowNodeCatalogItem): boolean {
  const manifest = readCatalogRecord(item.runtimeCapability?.manifest)
  const canvas = readCatalogRecord(manifest?.canvas)
  return canvas?.locked === true
}

export function workflowCatalogPluginProvenance(
  item: WorkflowNodeCatalogItem,
): { providerKey: string; version: string } | null {
  const manifest = readCatalogRecord(item.runtimeCapability?.manifest)
  const plugin = readCatalogRecord(manifest?.plugin)
  const providerKey = typeof plugin?.providerKey === "string" ? plugin.providerKey : null
  const version = typeof plugin?.version === "string" ? plugin.version : null
  return providerKey && version ? { providerKey, version } : null
}

export function workflowCatalogIsBackendNode(item: WorkflowNodeCatalogItem): boolean {
  const manifest = readCatalogRecord(item.runtimeCapability?.manifest)
  const nodeCatalog = readCatalogRecord(manifest?.nodeCatalog)
  return nodeCatalog?.authority === "backend"
}

function backendNodeCatalogItem(
  runtimeCapability: WorkflowRuntimeCapability,
): WorkflowNodeCatalogItem | null {
  const manifest = readCatalogRecord(runtimeCapability.manifest)
  const nodeCatalog = readCatalogRecord(manifest?.nodeCatalog)
  const canvas = readCatalogRecord(manifest?.canvas)
  const legacyPlugin = runtimeCapability.source === "backend.services.plugin_registry_service"
  if (!legacyPlugin && (nodeCatalog?.authority !== "backend" || canvas?.node !== true)) return null
  const kind = catalogNodeKind(runtimeCapability.kind)
  const capability = catalogNodeCapability(runtimeCapability.capability)
  if (!kind || !capability) return null
  const plugin = readCatalogRecord(manifest?.plugin)
  const presentation = readCatalogRecord(manifest?.presentation)
  const providerKey = typeof plugin?.providerKey === "string"
    ? plugin.providerKey
    : runtimeCapability.provider ?? "opencli"
  const version = typeof plugin?.version === "string" ? plugin.version : "catalog"
  const category = typeof nodeCatalog?.category === "string"
    ? backendCatalogCategory(nodeCatalog.category)
    : pluginCatalogCategory(typeof plugin?.family === "string" ? plugin.family : "tool")
  const origin = typeof nodeCatalog?.origin === "string" ? nodeCatalog.origin : "plugin"
  const adapterValue = nodeCatalog?.adapter
  const parsedAdapter = adapterValue === undefined
    ? undefined
    : adapterBindingSchema.safeParse(adapterValue)
  if (parsedAdapter && !parsedAdapter.success) return null
  const adapter = parsedAdapter?.data
  const description = typeof presentation?.description === "string"
    ? presentation.description
    : runtimeCapability.reason ?? "后端节点能力"
  const icon = typeof presentation?.icon === "string"
    ? presentation.icon
    : backendCatalogIcon(category)
  return {
    id: runtimeCapability.id,
    idPrefix: safeIdPart(`${providerKey}-${runtimeCapability.label}`),
    label: runtimeCapability.label,
    description,
    category,
    profile: "intelligence",
    kind,
    capability,
    icon,
    color: "var(--muted-foreground)",
    adapter: adapter?.id,
    requiredAdapters: adapter ? [adapter] : undefined,
    params: {
      ...catalogParameterDefaults(presentation?.parameters),
      pluginInstallationId: plugin?.installationId,
      ...(origin === "plugin" ? { pluginProviderKey: providerKey, pluginVersion: version } : {}),
      pluginCapabilityId: plugin?.capabilityId,
    },
    runtimeCapability,
    runtimeContract: runtimeContractForCapability(runtimeCapability),
    keywords: [
      "node-capability",
      "dify",
      providerKey,
      version,
      category,
      origin,
      runtimeCapability.label,
      ...runtimeCapability.tags,
    ],
  }
}

function catalogNodeKind(value: string | null | undefined): WorkflowNodeKind | null {
  return ["schedule", "source", "agent", "router", "notify", "inbox", "action", "flow", "control", "sink"].includes(value ?? "")
    ? value as WorkflowNodeKind
    : null
}

function catalogNodeCapability(
  value: string | null | undefined,
): WorkflowCapability | null {
  return ["trigger", "fetch", "normalize", "dedupe", "summarize", "score", "tag", "route", "send", "store", "merge", "accept"].includes(value ?? "")
    ? value as WorkflowCapability
    : null
}

function pluginCatalogCategory(family: string): WorkflowNodeCatalogCategory {
  if (family === "trigger") return "trigger"
  if (family === "datasource") return "source"
  if (family === "agent_strategy") return "processing"
  return "output"
}

function backendCatalogCategory(value: string): WorkflowNodeCatalogCategory {
  if (value === "input" || value === "trigger") return "trigger"
  if (value === "source" || value === "knowledge") return "source"
  if (value === "logic") return "decision"
  if (value === "flow") return "flow"
  if (value === "human") return "control"
  if (value === "output") return "output"
  if (value === "compatibility") return "package"
  if (value === "tool" || value === "plugin") return "output"
  return "processing"
}

function backendCatalogIcon(category: WorkflowNodeCatalogCategory): string {
  if (category === "trigger") return "Clock"
  if (category === "source") return "Database"
  if (category === "decision") return "GitBranch"
  if (category === "flow") return "GitMerge"
  if (category === "control") return "BadgeCheck"
  if (category === "output") return "Send"
  if (category === "package") return "Package"
  return "Sparkles"
}

function catalogParameterDefaults(value: unknown): Record<string, unknown> {
  if (!Array.isArray(value)) return {}
  return Object.fromEntries(value.flatMap((entry) => {
    const parameter = readCatalogRecord(entry)
    const name = typeof parameter?.name === "string" ? parameter.name : null
    const defaultValue = parameter ? backendParameterDefault(parameter) : undefined
    return name && defaultValue !== undefined ? [[name, defaultValue]] : []
  }))
}

function readCatalogRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function backendCatalogPrimitivePorts(
  item: WorkflowNodeCatalogItem,
): Array<{ id: string; direction: "input" | "output"; type: string; required: boolean }> {
  const manifest = readCatalogRecord(item.runtimeCapability?.manifest)
  const ports = readCatalogRecord(manifest?.ports)
  const collect = (
    values: unknown,
    direction: "input" | "output",
  ): Array<{ id: string; direction: "input" | "output"; type: string; required: boolean }> => (
    Array.isArray(values)
      ? values.flatMap((entry) => {
          const port = readCatalogRecord(entry)
          const id = typeof port?.name === "string" ? port.name : null
          const type = typeof port?.type === "string" ? port.type : null
          return id && type
            ? [{ id, direction, type, required: port?.required === true }]
            : []
        })
      : []
  )
  return [
    ...collect(ports?.inputs, "input"),
    ...collect(ports?.outputs, "output"),
  ]
}

export function createWorkflowNodeFromCatalog(
  item: WorkflowNodeCatalogItem,
  id: string,
  position: { x: number; y: number },
): WorkflowProjectNode {
  const primitiveId = workflowCatalogIsBackendNode(item) && item.id.startsWith("primitive.")
    ? item.id
    : undefined
  const parameterInterface = createDataOperatorParameterInterface(
    id,
    item.id,
    item.params,
    item.runtimeCapability,
  ) ?? createBackendParameterInterface(id, item.runtimeCapability?.manifest)
    ?? (item.category === "package" && !item.internals
      ? undefined
      : createParameterInterfaceFromInternals(
        id,
        getNodeInternals({
          id,
          kind: item.kind,
          capability: item.capability,
          adapter: item.adapter,
          params: item.params,
          ui: { catalogId: item.id },
        }),
      ))

  return {
    id,
    kind: item.kind,
    capability: item.capability,
    adapter: item.adapter,
    params: cloneCatalogValue(item.params) ?? {},
    proposalState: item.proposalState,
    topicCollapse: cloneCatalogValue(item.topicCollapse),
    ...(parameterInterface ? { parameterInterface } : {}),
    internals: cloneCatalogValue(item.internals),
    ui: {
      label: item.label,
      description: item.description,
      icon: item.icon,
      color: item.color,
      position,
      catalogId: item.id,
      ...(primitiveId
        ? { primitiveId, primitivePorts: backendCatalogPrimitivePorts(item) }
        : {}),
      runtimeCapability: cloneCatalogValue(item.runtimeCapability),
      runtimeContract: cloneCatalogValue(item.runtimeContract),
    },
  }
}

export type WorkflowOperatorNodeOptions = {
  label?: string
  description?: string
}

/**
 * Build the Dify-style business layer without replacing the existing OpenCLI node.
 *
 * The operator is a structural/governance container (L1). The catalog node remains
 * intact as its implementation child (L2), including its adapter, parameter
 * interface, runtime contract, and deeper internal network.
 */
export function createOperatorNodeFromCatalog(
  item: WorkflowNodeCatalogItem,
  operatorId: string,
  implementationId: string,
  position: { x: number; y: number },
  options: WorkflowOperatorNodeOptions = {},
): WorkflowProjectNode {
  const implementation = createWorkflowNodeFromCatalog(item, implementationId, { x: 120, y: 160 })
  const implementationNode: WorkflowProjectNode = {
    ...implementation,
    ui: {
      ...implementation.ui,
      networkRole: "implementation",
    },
  }

  return {
    id: operatorId,
    kind: item.kind,
    capability: item.capability,
    params: {
      operator: {
        execution: "internals",
        implementationCatalogId: item.id,
        implementationNodeId: implementationId,
      },
    },
    internals: {
      locked: false,
      nodes: [implementationNode],
      edges: [],
    },
    miniNetwork: {
      nodes: 1,
      edges: 0,
      mode: "title-only",
    },
    ui: {
      label: options.label ?? item.label,
      description: options.description ?? `${item.description}；双击进入 OpenCLI 实现网络`,
      icon: item.icon,
      color: item.color,
      position,
      catalogId: item.id,
      preferCustomLabel: true,
      networkRole: "operator",
      implementationCatalogId: item.id,
    },
  }
}

export function addCatalogNodeToWorkflowProject(
  project: WorkflowProject,
  item: WorkflowNodeCatalogItem,
  id: string,
  position: { x: number; y: number },
): WorkflowProject {
  const existingAdapters = new Set(project.adapters.map((adapter) => adapter.id))
  const requiredAdapters = (item.requiredAdapters ?? []).filter((adapter) => !existingAdapters.has(adapter.id))
  return parseWorkflowProject({
    ...project,
    agentPermissions: {
      ...project.agentPermissions,
      ...item.agentPermissionPatch,
    },
    adapters: [...project.adapters, ...requiredAdapters],
    nodes: [
      ...project.nodes,
      item.category === "package"
        ? createOperatorNodeFromCatalog(item, id, `${id}-implementation`, position)
        : createWorkflowNodeFromCatalog(item, id, position),
    ],
  })
}

function cloneCatalogValue<T>(value: T | undefined): T | undefined {
  return value === undefined ? undefined : (JSON.parse(JSON.stringify(value)) as T)
}
