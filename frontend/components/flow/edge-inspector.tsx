import { useFlowStore } from "@/lib/flow/store"
import type { GeneratedWorkflowEdgeMapping, WorkflowEdge } from "@/lib/flow/types"
import type { WorkflowLanguage } from "@/lib/workflow/node-i18n"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { MonoRow, PanelShell, SectionCaption } from "./inspector-shell"
import { houdiniInputClass } from "./inspector-styles"

const edgeTypeOptions = [
  { value: "workflow", label: "默认（贝塞尔曲线）" },
  { value: "editable", label: "可编辑路径" },
  { value: "routed", label: "智能避障（正交路由）" },
]

const edgeTypeHints: Record<string, string> = {
  workflow: "标准平滑曲线连线。",
  editable: "选中后可拖动控制点调整路径，双击线条添加控制点、双击控制点删除。",
  routed: "自动绕开中间节点的正交折线，适合密集流程图。",
}

type EdgeInspectorCopy = {
  fieldMapping: string
  fieldMappingGap: string
  legacyMapping: string
  transform: string
  compatibility: string
  compilable: string
  blocked: string
}

export function EdgeInspector({ edge, compact, onClose, language, copy }: {
  edge: WorkflowEdge
  compact: boolean
  onClose: () => void
  language: WorkflowLanguage
  copy: EdgeInspectorCopy
}) {
  const updateEdgeData = useFlowStore((s) => s.updateEdgeData)
  const updateEdgeType = useFlowStore((s) => s.updateEdgeType)
  const toggleEdgeAnimated = useFlowStore((s) => s.toggleEdgeAnimated)
  const takeSnapshot = useFlowStore((s) => s.takeSnapshot)
  const edgeType = edge.type ?? "workflow"
  const mapping: GeneratedWorkflowEdgeMapping = edge.data?.mapping ?? {
    mode: "auto",
    fields: [],
    preserveRaw: true,
    compatible: true,
    conflicts: [],
  }
  const updateMapping = (patch: Partial<GeneratedWorkflowEdgeMapping>) => {
    updateEdgeData(edge.id, {
      mapping: {
        ...mapping,
        ...patch,
        preserveRaw: true,
      },
    })
  }
  const updateMappingTransform = (
    index: number,
    transform: string | undefined,
  ) => {
    updateMapping({
      fields: mapping.fields.map((field, fieldIndex) =>
        fieldIndex === index ? { ...field, transform } : field,
      ),
    })
  }
  return (
    <PanelShell
      compact={compact}
      title="Connection"
      typeLine={`EDGE::${edgeType.toUpperCase()}`}
      onClose={onClose}
    >
      <div className="space-y-4 p-4">
        <div className="space-y-1.5">
          <Label htmlFor="edge-label" className="font-mono text-[10px] uppercase tracking-wider">
            Label
          </Label>
          <Input
            id="edge-label"
            value={(edge.data?.label as string) ?? ""}
            onFocus={takeSnapshot}
            onChange={(e) => updateEdgeData(edge.id, { label: e.target.value })}
            placeholder="例如：成功 / 失败"
          />
        </div>

        <div className="space-y-3 rounded-md border bg-card p-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <SectionCaption>{copy.fieldMapping}</SectionCaption>
              <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                {copy.fieldMappingGap}
              </p>
            </div>
            <span className="shrink-0 rounded-xs border border-ops-line bg-ops-raised px-2 py-1 font-mono text-3xs uppercase text-zinc-400">
              {mapping.mode}
            </span>
          </div>

          {mapping.fields.map((field, index) => (
            <div key={index} className="space-y-2 rounded-md border bg-background p-2">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 font-mono text-2xs">
                <span className="truncate rounded-xs border border-ops-line bg-ops-raised px-2 py-1.5 text-zinc-300" title={field.source}>
                  {field.source}
                </span>
                <span className="font-mono text-xs text-muted-foreground">→</span>
                <span className="truncate rounded-xs border border-ops-line bg-ops-raised px-2 py-1.5 text-zinc-300" title={field.target}>
                  {field.target}
                </span>
              </div>
              <div className="space-y-1">
                <Label htmlFor={`edge-transform-${index}`} className="font-mono text-3xs uppercase tracking-wider text-zinc-500">
                  {copy.transform}
                </Label>
                <Input
                  id={`edge-transform-${index}`}
                  aria-label={`映射 ${index + 1} 转换`}
                  value={field.transform ?? ""}
                  onFocus={takeSnapshot}
                  onChange={(event) => updateMappingTransform(index, event.target.value || undefined)}
                  placeholder={language === "zh-CN" ? "可选转换表达式" : "Optional transform expression"}
                  className={houdiniInputClass}
                />
              </div>
            </div>
          ))}

          {mapping.fields.length === 0 ? (
            <p className="rounded-xs border border-dashed border-ops-line p-3 text-2xs leading-relaxed text-zinc-500">
              {copy.fieldMappingGap}
            </p>
          ) : (
            <p className="font-mono text-3xs uppercase tracking-wider text-zinc-500">
              {copy.legacyMapping} · {mapping.fields.length}
            </p>
          )}

          <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
            <span className="text-muted-foreground">{copy.compatibility}</span>
            <span className={mapping.compatible ? "text-success" : "text-destructive"}>
              {mapping.compatible ? copy.compilable : copy.blocked}
            </span>
          </div>
          {mapping.conflicts.map((conflict) => (
            <p key={conflict} className="text-[11px] leading-relaxed text-destructive">
              {conflict}
            </p>
          ))}
        </div>

        <div className="space-y-1.5">
          <Label className="font-mono text-[10px] uppercase tracking-wider">Type</Label>
          <Select value={edgeType} onValueChange={(v) => v && updateEdgeType(edge.id, v)}>
            <SelectTrigger>
              <SelectValue>
                {(value: string | null) => edgeTypeOptions.find((o) => o.value === value)?.label ?? value}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {edgeTypeOptions.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-[11px] leading-relaxed text-muted-foreground">{edgeTypeHints[edgeType]}</p>
        </div>

        <Separator />

        <div className="flex items-center justify-between">
          <div className="space-y-0.5">
            <Label htmlFor="edge-anim" className="font-mono text-[10px] uppercase tracking-wider">
              Flow Animation
            </Label>
            <p className="text-[11px] text-muted-foreground">显示流向的虚线动画</p>
          </div>
          <input
            id="edge-anim"
            type="checkbox"
            checked={!!edge.animated}
            onChange={() => toggleEdgeAnimated(edge.id)}
            className="houdini-checkbox"
          />
        </div>

        <Separator />
        <div className="space-y-1.5 rounded-md border bg-card p-3">
          <SectionCaption>Debug</SectionCaption>
          <MonoRow k="id" v={edge.id} />
          <MonoRow k="wire" v={`${edge.source} → ${edge.target}`} />
        </div>
      </div>
    </PanelShell>
  )
}
