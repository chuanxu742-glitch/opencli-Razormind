import LZString from "lz-string"
import { z } from "zod"
import { MarkerType, Position } from "@xyflow/react"
import {
  parseWorkflowProject, workflowNodeSchema, sourceAnchorSchema, miniNetworkPreviewSchema,
  topicCollapseStateSchema, parameterInterfaceSchema, semanticLinkSchema, proposalStateSchema,
  type WorkflowProject,
} from "@/lib/workflow/schema"
import type { FlowSnapshot, WorkflowEdge, WorkflowNode } from "./types"

const SHARE_PARAM = "flow"
const { compressToEncodedURIComponent, decompressFromEncodedURIComponent } = LZString

const pointSchema = z.object({ x: z.number().finite(), y: z.number().finite() })
const styleSchema = z.record(z.string(), z.union([z.string(), z.number().finite()]))
const positionSideSchema = z.enum(Position)
const markerSchema = z.union([z.string(), z.object({
  type: z.enum(MarkerType), color: z.string().nullable().optional(),
  width: z.number().optional(), height: z.number().optional(), markerUnits: z.string().optional(),
  orient: z.string().optional(), strokeWidth: z.number().optional(),
})])
// Only restore persisted editor data. Runtime responses are derived locally, not
// trusted from a URL; stripping them also keeps old shares usable after upgrades.
const nodeDataSchema = z.object({
  label: z.string(),
  description: z.string().optional(),
  nodeType: z.enum(["trigger", "action", "condition", "transform", "delay", "http", "note", "group", "shape"]),
  category: z.enum(["trigger", "action", "logic", "data", "annotation", "shape"]),
  icon: z.string(),
  status: z.enum(["idle", "running", "waiting", "success", "partial_success", "error"]).optional(),
  fields: z.array(z.object({ id: z.string(), label: z.string(), value: z.string() })).optional(),
  condition: z.string().optional(),
  collapsed: z.boolean().optional(),
  expandedHeight: z.number().finite().optional(),
  color: z.string().optional(),
  internalStepId: z.string().optional(),
  internalLocked: z.boolean().optional(),
  internalDraft: z.boolean().optional(),
  canonical: workflowNodeSchema.pick({ kind: true, capability: true, adapter: true, params: true })
    .extend({ catalogId: z.string().optional() }).optional(),
  shape: z.enum(["rectangle", "round", "circle", "diamond", "hexagon", "parallelogram", "cylinder"]).optional(),
  sourceAnchor: sourceAnchorSchema.optional(),
  runArtifact: workflowNodeSchema.shape.runArtifact,
  miniNetwork: miniNetworkPreviewSchema.optional(),
  topicCollapse: topicCollapseStateSchema.optional(),
  proposalState: proposalStateSchema.optional(),
  parameterInterface: parameterInterfaceSchema.optional(),
  externalWorkflow: z.object({ source: z.string(), originalId: z.string().optional(), originalName: z.string().optional(), type: z.string().optional() }).optional(),
  imageStudioSummary: z.object({ snapshotId: z.string().optional(), modelFingerprint: z.string().optional(), recentAssetIds: z.array(z.string()) }).optional(),
  operatorShape: z.string().optional(),
  canvasShape: z.string().optional(),
  nodeShape: z.string().optional(),
  useSemanticShape: z.boolean().optional(),
  primitivePorts: z.array(z.object({ id: z.string(), direction: z.string(), type: z.string() })).optional(),
  kind: z.enum(["input", "op", "output"]).optional(),
  op: z.enum(["add", "sub", "mul", "div"]).optional(),
  value: z.number().finite().optional(),
  handleType: z.literal("number").optional(),
})
const canvasNodeSchema = z.object({
  id: z.string().min(1),
  type: z.enum(["workflow", "note", "group", "shape", "math", "default", "input", "output"]).optional(),
  position: pointSchema,
  data: nodeDataSchema,
  parentId: z.string().min(1).optional(),
  extent: z.union([z.literal("parent"), z.tuple([z.tuple([z.number(), z.number()]), z.tuple([z.number(), z.number()])])]).optional(),
  expandParent: z.boolean().optional(),
  hidden: z.boolean().optional(),
  selected: z.boolean().optional(),
  draggable: z.boolean().optional(),
  selectable: z.boolean().optional(),
  connectable: z.boolean().optional(),
  deletable: z.boolean().optional(),
  width: z.number().finite().nonnegative().optional(),
  height: z.number().finite().nonnegative().optional(),
  zIndex: z.number().finite().optional(),
  style: styleSchema.optional(),
  sourcePosition: positionSideSchema.optional(), targetPosition: positionSideSchema.optional(),
  origin: z.tuple([z.number(), z.number()]).optional(),
  className: z.string().optional(), ariaLabel: z.string().optional(),
})
const canvasEdgeSchema = z.object({
  id: z.string().min(1), source: z.string().min(1), target: z.string().min(1),
  type: z.enum(["workflow", "editable", "routed", "default", "straight", "step", "smoothstep", "simplebezier"]).optional(),
  sourceHandle: z.string().nullable().optional(), targetHandle: z.string().nullable().optional(),
  label: z.union([z.string(), z.number()]).optional(),
  animated: z.boolean().optional(), hidden: z.boolean().optional(), selected: z.boolean().optional(),
  style: styleSchema.optional(),
  markerStart: markerSchema.optional(), markerEnd: markerSchema.optional(),
  labelStyle: styleSchema.optional(), labelBgStyle: styleSchema.optional(),
  labelShowBg: z.boolean().optional(), labelBgPadding: z.tuple([z.number(), z.number()]).optional(),
  labelBgBorderRadius: z.number().optional(), zIndex: z.number().optional(),
  interactionWidth: z.number().optional(), className: z.string().optional(), ariaLabel: z.string().optional(),
  selectable: z.boolean().optional(), deletable: z.boolean().optional(), focusable: z.boolean().optional(),
  data: z.object({
    sourcePort: z.string().optional(), targetPort: z.string().optional(),
    label: z.string().optional(), semantic: semanticLinkSchema.optional(),
    weight: z.number().min(0).max(1).optional(), contractId: z.string().optional(),
    proposalState: proposalStateSchema.optional(), waypoints: z.array(pointSchema).optional(), routed: z.boolean().optional(),
    mapping: z.object({ mode: z.enum(["auto", "override"]), fields: z.array(z.object({ source: z.string(), target: z.string(), transform: z.string().optional() })), preserveRaw: z.literal(true), compatible: z.boolean(), conflicts: z.array(z.string()) }).optional(),
  }).optional(),
})
const shareStateSchema = z.object({
  schema: z.literal("react-flow-powerpack.share.v1"),
  workflowProject: z.unknown().transform((value) => parseWorkflowProject(value)),
  nodes: z.array(canvasNodeSchema), edges: z.array(canvasEdgeSchema),
  drawings: z.array(z.object({
    id: z.string().min(1), points: z.array(z.array(z.number().finite()).min(2)),
    color: z.string(), size: z.number().finite().positive(),
  })).optional(),
})

function hasValidCanvasReferences(state: ShareState): boolean {
  const nodes = new Map(state.nodes.map((node) => [node.id, node]))
  if (nodes.size !== state.nodes.length || new Set(state.edges.map((edge) => edge.id)).size !== state.edges.length) return false
  if (state.edges.some((edge) => !nodes.has(edge.source) || !nodes.has(edge.target))) return false
  for (const node of state.nodes) {
    const ancestors = new Set([node.id])
    let parentId = node.parentId
    while (parentId) {
      if (!nodes.has(parentId) || ancestors.has(parentId)) return false
      ancestors.add(parentId)
      parentId = nodes.get(parentId)?.parentId
    }
  }
  return true
}

export interface ShareState {
  schema: "react-flow-powerpack.share.v1"
  workflowProject: WorkflowProject
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  drawings?: FlowSnapshot["drawings"]
}

export function encodeShareState(state: Omit<ShareState, "schema">): string {
  return compressToEncodedURIComponent(JSON.stringify({ schema: "react-flow-powerpack.share.v1", ...state }))
}

export function decodeShareState(encoded: string): ShareState | null {
  try {
    const raw = decompressFromEncodedURIComponent(encoded)
    if (!raw) return null
    const parsed = shareStateSchema.parse(JSON.parse(raw))
    return hasValidCanvasReferences(parsed) ? parsed : null
  } catch {
    return null
  }
}

export function buildShareUrl(state: Omit<ShareState, "schema">, baseHref: string): string {
  const url = new URL(baseHref)
  url.searchParams.set(SHARE_PARAM, encodeShareState(state))
  return url.toString()
}

export function loadShareStateFromUrl(href: string): ShareState | null {
  const url = new URL(href)
  const encoded = url.searchParams.get(SHARE_PARAM)
  return encoded ? decodeShareState(encoded) : null
}
