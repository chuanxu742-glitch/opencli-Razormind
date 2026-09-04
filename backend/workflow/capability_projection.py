"""Workflow capability projection for Canvas-visible nodes.

This is an audit/runtime-status surface, not an executor registry. It reports
which existing backend capabilities can support Canvas nodes today, and which
visible nodes are blocked until a real binding is added.
"""

from __future__ import annotations

from collections import Counter

from backend.channels.registry import list_channel_types
from backend.notifiers.registry import list_notifier_types
from backend.schemas.plugin import PluginInstallationRead
from backend.schemas.workflow import (
    WorkflowCapabilitiesResponse,
    WorkflowCapability,
    WorkflowCapabilityStatus,
    WorkflowCapabilitySurface,
    WorkflowNodeKind,
    WorkflowRuntimeCapability,
)
from backend.workflow.data_operators import list_data_operator_specs
from backend.workflow.dify_graphon_client import DIFY_GRAPHON_BINDING_ID
from backend.workflow.native_intelligence_executor import (
    NATIVE_INTELLIGENCE_LIFECYCLE_ACTIONS,
)
from backend.workflow.node_registry import WORKFLOW_PRIMITIVE_IDS
from backend.workflow.opencli_adapter_nodes import get_opencli_adapter_node_summary
from backend.workflow.runtime_contracts import runtime_io_contract_manifest
from backend.workflow.runtime_registry import (
    COLLECTION_OUTPUT_BINDING_ID,
    DATA_OPERATOR_CATALOG_BINDINGS,
    DEDUPE_BINDING_ID,
    DEMAND_DRAFT_BINDING_ID,
    EXTERNAL_TOOL_BINDING_ID,
    FEISHU_BITABLE_SINK_BINDING_ID,
    IMAGE_ASSET_BINDING_ID,
    IMAGE_GENERATION_BINDING_ID,
    MERGE_BINDING_ID,
    NORMALIZE_BINDING_ID,
    OPENCLI_BINDING_ID,
    RECORD_ACCEPTANCE_BINDING_ID,
    RECORD_SINK_BINDING_ID,
    SCHEDULE_TRIGGER_BINDING_ID,
    SOURCE_FETCH_BINDING_ID,
    SOURCE_POOL_BINDING_ID,
    TURBOPUSH_BINDING_ID,
    WEBHOOK_NOTIFY_BINDING_ID,
    WEBHOOK_TRIGGER_BINDING_ID,
)
from backend.workflow.tool_capabilities import list_workflow_tool_capabilities
from backend.workflow.turbopush_runtime import TURBOPUSH_PROVIDER


def build_workflow_capabilities(
    plugin_installations: list[PluginInstallationRead] | None = None,
) -> WorkflowCapabilitiesResponse:
    """Project real backend capabilities into Canvas runtime status rows."""

    return WorkflowCapabilitiesResponse(
        catalog=[
            *_catalog_capabilities(
                dify_runtime_ready=_dify_runtime_ready(plugin_installations or [])
            ),
            *_tool_catalog_capabilities(),
            *_plugin_catalog_capabilities(plugin_installations or []),
        ],
        primitives=_primitive_capabilities(),
        channels=_channel_capabilities(),
        notifiers=_notifier_capabilities(),
        triggers=_trigger_capabilities(),
        resources=_resource_capabilities(),
    )


def _capability(
    *,
    id: str,
    label: str,
    surface: WorkflowCapabilitySurface,
    status: WorkflowCapabilityStatus,
    backend_available: bool = False,
    kind: WorkflowNodeKind | None = None,
    capability: WorkflowCapability | None = None,
    provider: str | None = None,
    channel_type: str | None = None,
    notifier_type: str | None = None,
    runtime_binding: str | None = None,
    reason: str | None = None,
    missing: list[str] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    manifest: dict[str, object] | None = None,
) -> WorkflowRuntimeCapability:
    resolved_manifest = _manifest_with_runtime_contract(manifest or {}, runtime_binding)
    return WorkflowRuntimeCapability(
        id=id,
        label=label,
        surface=surface,
        status=status,
        backendAvailable=backend_available,
        kind=kind,
        capability=capability,
        provider=provider,
        channelType=channel_type,
        notifierType=notifier_type,
        runtimeBinding=runtime_binding,
        reason=reason,
        missing=missing or [],
        tags=tags or [],
        source=source,
        manifest=resolved_manifest,
    )


def _catalog_capabilities(*, dify_runtime_ready: bool = False) -> list[WorkflowRuntimeCapability]:
    expected_native_children = {
        action: f"tool.intelligence.native.{action}"
        for action in NATIVE_INTELLIGENCE_LIFECYCLE_ACTIONS
    }
    expected_native_ids = set(expected_native_children.values())
    native_tools = [
        tool
        for tool in list_workflow_tool_capabilities().tools
        if tool.executor.mode == "native_intelligence"
        and (
            tool.executor.params.get("action") in expected_native_children
            or tool.id in expected_native_ids
        )
    ]
    native_actions = [
        action if isinstance(action := tool.executor.params.get("action"), str) else "<missing>"
        for tool in native_tools
    ]
    native_ids = [tool.id for tool in native_tools]
    action_counts = Counter(native_actions)
    id_counts = Counter(native_ids)
    missing_actions = sorted(set(expected_native_children) - set(native_actions))
    extra_actions = sorted(set(native_actions) - set(expected_native_children))
    duplicate_actions = sorted(action for action, count in action_counts.items() if count > 1)
    missing_tool_ids = sorted(expected_native_ids - set(native_ids))
    extra_tool_ids = sorted(set(native_ids) - expected_native_ids)
    duplicate_tool_ids = sorted(tool_id for tool_id, count in id_counts.items() if count > 1)
    expected_pairs = {(tool_id, action) for action, tool_id in expected_native_children.items()}
    actual_pairs = set(zip(native_ids, native_actions, strict=True))
    missing_children = [
        {"id": tool_id, "action": action}
        for tool_id, action in sorted(expected_pairs - actual_pairs)
    ]
    extra_children = [
        {"id": tool_id, "action": action}
        for tool_id, action in sorted(actual_pairs - expected_pairs)
    ]
    native_blocked = [tool for tool in native_tools if tool.status != "runnable"]
    native_action_set_invalid = bool(
        missing_actions
        or extra_actions
        or duplicate_actions
        or missing_tool_ids
        or extra_tool_ids
        or duplicate_tool_ids
        or missing_children
        or extra_children
    )
    native_package_status: WorkflowCapabilityStatus = (
        "runnable" if not native_action_set_invalid and not native_blocked else "blocked"
    )
    native_package_missing = [
        reason
        for tool in native_blocked
        for reason in (
            tool.manifest.get("readiness", {}).get("missingReasons", [])
            if isinstance(tool.manifest.get("readiness"), dict)
            else ["child_readiness_missing"]
        )
        if isinstance(reason, str)
    ]
    if native_package_status != "runnable":
        native_package_missing.append("missing_native_intelligence_action_set")
    native_package_missing = sorted(set(native_package_missing))
    return [
        _capability(
            id="package.compat.dify-workflow",
            label="Dify Workflow Package",
            surface="catalog",
            status="runnable" if dify_runtime_ready else "blocked",
            backend_available=dify_runtime_ready,
            kind="action",
            capability="store",
            provider="opencli-admin/dify-graphon-runtime",
            runtime_binding=DIFY_GRAPHON_BINDING_ID,
            reason=(
                "Dify DSL is imported as one managed package and executed through the "
                "pinned Graphon compatibility runtime. Unsupported dependencies remain "
                "structured compile blockers."
                if dify_runtime_ready
                else "The pinned Graphon compatibility runtime is unavailable or has an "
                "unexpected identity."
            ),
            missing=[] if dify_runtime_ready else ["dify_graphon_runtime"],
            tags=["dify", "graphon", "managed-package", "compatibility"],
            source="backend.workflow.dify_compile",
            manifest={
                "schema": "capability.package.dify-graphon.v1",
                "runtime": {"binding": DIFY_GRAPHON_BINDING_ID},
                "canvas": {"node": True, "managedInternals": True},
            },
        ),
        _capability(
            id="media.image-generation",
            label="Image Generation",
            surface="catalog",
            status="blocked",
            backend_available=False,
            kind="media",
            capability="generate",
            provider="invokeai",
            runtime_binding=IMAGE_GENERATION_BINDING_ID,
            reason=(
                "Canvas publication and durable job creation are wired, but runtime "
                "version binding, dispatch reconciliation, and an attested InvokeAI "
                "image are still required."
            ),
            missing=[
                "published_version_run_binding",
                "durable_dispatch_reconciliation",
                "attested_image_runtime",
            ],
            tags=["media", "image", "generation", "async"],
            source="backend.workflow.opencli_hda_tracer",
        ),
        _capability(
            id="media.image-asset",
            label="Image Asset",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="media",
            capability="fetch",
            provider="opencli-admin",
            runtime_binding=IMAGE_ASSET_BINDING_ID,
            reason="The node emits fixed OpenCLI media asset references without "
            "submitting a new generation job.",
            tags=["media", "image", "asset"],
            source="backend.workflow.opencli_hda_tracer",
        ),
        _capability(
            id="intelligence.input.collection-need",
            label="Collection Need",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            provider="workflow",
            runtime_binding=DEMAND_DRAFT_BINDING_ID,
            reason="Canvas demand input calls the backend demand-draft endpoint "
            "to assemble existing real source/package nodes into a reviewable patch.",
            tags=["input", "demand", "patch"],
            source="backend.workflow.demand_assembler",
            manifest=_manifest(
                schema="capability.workflow.demand-draft.v1",
                output_ports=[_port("patch", "workflowPatch")],
                resources=["capability_catalog"],
                permissions=["canvas_review_required"],
                runtime_binding=DEMAND_DRAFT_BINDING_ID,
                trace_events=["patch_preview", "compile_preview"],
                probes=["demand_draft_endpoint_available"],
            ),
        ),
        _capability(
            id="intelligence.schedule.cron",
            label="Cron Schedule",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            provider="workflow",
            runtime_binding=SCHEDULE_TRIGGER_BINDING_ID,
            reason="Canvas Run creates an authoritative workflow trigger tick "
            "from the schedule node params. Automatic scheduler-to-run creation "
            "is a separate scheduler integration.",
            tags=["trigger", "schedule", "run"],
            source="backend.workflow.runtime_registry",
        ),
        _capability(
            id="intelligence.source.jin10",
            label="JIN10 Source",
            surface="catalog",
            status="preview_only",
            backend_available=False,
            kind="source",
            capability="fetch",
            provider="jin10",
            reason="JIN10 is wired as a frontend fixture/live adapter, not as "
            "an authoritative backend workflow runtime binding.",
            missing=["backend_source_channel_binding"],
            tags=["source", "adapter", "preview"],
        ),
        _capability(
            id="intelligence.source.rss",
            label="RSS / Atom Reader",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="rss",
            channel_type="rss",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Canvas Run executes RSS and Atom feeds through the backend RSS "
                "channel while preserving sourceGroup lineage."
            ),
            tags=["source", "rss", "atom", "live"],
            source="backend.workflow.rss_source_executor",
            manifest=_manifest(
                schema="capability.source.rss.v1",
                input_ports=[_port("in", "trigger")],
                output_ports=[_port("out", "items[]")],
                resources=["rss_channel", "feed_provider_registry", "allowed_domains"],
                permissions=["network.fetch"],
                runtime_binding=SOURCE_FETCH_BINDING_ID,
            ),
        ),
        _capability(
            id="intelligence.source.doubao-research",
            label="Doubao Research Capture",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="doubao_research",
            channel_type="doubao_research",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Canvas Run captures a live Doubao answer through the existing "
                "session-bound doubao_research channel and preserves its evidence."
            ),
            tags=["source", "doubao", "research", "chat-ai.capture", "live"],
            source="backend.workflow.gaojixing_runtime",
            manifest={
                **_manifest(
                    schema="capability.source.doubao-research.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=["opencli_session", "browser_profile"],
                    permissions=["network.fetch", "canFetchNetwork"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                    trace_events=["partial:gaojixing.capture", "completed"],
                    probes=["doubao_research_channel_available", "session_authenticated"],
                ),
                "canvas": {"node": True},
                "nodeCatalog": {
                    "authority": "backend",
                    "origin": "source-preset",
                    "category": "source",
                    "kind": "source",
                    "capability": "fetch",
                    "adapter": {
                        "id": "source-doubao-research-capture",
                        "type": "source",
                        "provider": "doubao_research",
                        "mode": "live",
                        "config": {
                            "channelType": "doubao_research",
                            "liveMode": "live",
                            "settle_seconds": 150,
                        },
                    },
                },
                "presentation": {
                    "icon": "Search",
                    "description": "通过已认证的豆包研究会话捕获问题回答与证据。",
                    "parameters": [
                        {
                            "name": "question",
                            "label": "研究问题 / Research question",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "sourceGroup",
                            "label": "来源分组 / Source group",
                            "type": "string",
                            "default": "doubao-research",
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.searxng",
            label="SearXNG 元搜索 / Metasearch",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="http",
            channel_type="rest",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Runs a configured SearXNG JSON Search API through the guarded "
                "HTTP source executor and emits results as workflow items."
            ),
            tags=["source", "osint", "searxng", "metasearch", "live"],
            source="backend.workflow.http_source_executor",
            manifest={
                **_manifest(
                    schema="capability.source.searxng.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=["searxng_service", "allowed_domains"],
                    permissions=["network.fetch", "canFetchNetwork"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                    probes=["searxng_json_enabled", "source_domain_allowed"],
                ),
                "canvas": {"node": True},
                "nodeCatalog": {
                    "authority": "backend",
                    "origin": "source-preset",
                    "category": "source",
                    "kind": "source",
                    "capability": "fetch",
                    "adapter": {
                        "id": "source-searxng-http",
                        "type": "source",
                        "provider": "http",
                        "mode": "live",
                        "config": {
                            "channelType": "rest",
                            "method": "GET",
                            "query": {"format": "json"},
                        },
                    },
                },
                "presentation": {
                    "icon": "Search",
                    "description": (
                        "连接自托管 SearXNG JSON API；查询参数随节点保存，"
                        "域名仍受工作流网络权限约束。"
                    ),
                    "parameters": [
                        {
                            "name": "endpoint",
                            "label": "搜索地址 / Search endpoint",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "query",
                            "label": "查询参数 / Query parameters",
                            "type": "object",
                            "required": True,
                            "default": {
                                "q": "OpenCLI",
                                "format": "json",
                                "categories": "general",
                            },
                        },
                        {
                            "name": "resultPath",
                            "label": "结果路径 / Result path",
                            "type": "string",
                            "required": True,
                            "default": "results",
                        },
                        {
                            "name": "sourceGroup",
                            "label": "来源分组 / Source group",
                            "type": "string",
                            "default": "web-search",
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.rsshub",
            label="RSSHub 路由 / Route",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="rss",
            channel_type="rss",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Runs an RSSHub route through the existing RSS/Atom executor "
                "with source grouping, retry, lineage, and Provider support."
            ),
            tags=["source", "osint", "rsshub", "rss", "live"],
            source="backend.workflow.rss_source_executor",
            manifest={
                **_manifest(
                    schema="capability.source.rsshub.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=[
                        "rss_channel",
                        "feed_provider_registry",
                        "allowed_domains",
                    ],
                    permissions=["network.fetch", "canFetchNetwork"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                    probes=["rsshub_route_available", "source_domain_allowed"],
                ),
                "canvas": {"node": True},
                "nodeCatalog": {
                    "authority": "backend",
                    "origin": "source-preset",
                    "category": "source",
                    "kind": "source",
                    "capability": "fetch",
                    "adapter": {
                        "id": "source-rsshub-feed",
                        "type": "source",
                        "provider": "rss",
                        "mode": "live",
                        "config": {"channel": "rss"},
                    },
                },
                "presentation": {
                    "icon": "Rss",
                    "description": (
                        "读取自托管 RSSHub 路由或已登记的 Feed Provider，保留来源分组与运行血缘。"
                    ),
                    "parameters": [
                        {
                            "name": "feedUrl",
                            "label": "订阅地址 / Feed URL",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "maxEntries",
                            "label": "最大条目 / Max entries",
                            "type": "integer",
                            "default": 20,
                        },
                        {
                            "name": "sourceGroup",
                            "label": "来源分组 / Source group",
                            "type": "string",
                            "default": "rsshub",
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.rss-bridge",
            label="RSS-Bridge Reader",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="rss",
            channel_type="rss",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Canvas Run resolves the selected RSS-Bridge Provider and bridge, "
                "then reads the generated feed through the backend RSS channel."
            ),
            tags=["source", "rss-bridge", "reader", "live"],
            source="backend.workflow.rss_source_executor",
            manifest=_manifest(
                schema="capability.source.rss-bridge.v1",
                input_ports=[_port("in", "trigger")],
                output_ports=[_port("out", "items[]")],
                resources=["rss_channel", "feed_provider_registry", "allowed_domains"],
                permissions=["network.fetch"],
                runtime_binding=SOURCE_FETCH_BINDING_ID,
            ),
        ),
        _capability(
            id="intelligence.source.http",
            label="HTTP / API Reader",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="http",
            channel_type="http",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Canvas Run executes guarded GET or POST JSON requests through "
                "the workflow HTTP source executor."
            ),
            tags=["source", "http", "api", "reader", "live"],
            source="backend.workflow.http_source_executor",
            manifest=_manifest(
                schema="capability.source.http.v1",
                input_ports=[_port("in", "trigger")],
                output_ports=[_port("out", "items[]")],
                resources=["guarded_http_client", "allowed_domains"],
                permissions=["network.fetch"],
                runtime_binding=SOURCE_FETCH_BINDING_ID,
            ),
        ),
        _capability(
            id="intelligence.source.opencli-slot",
            label="OpenCLI Source Slot",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="opencli",
            channel_type="opencli",
            runtime_binding=OPENCLI_BINDING_ID,
            reason="Backend workflow compile resolves OpenCLI source/fetch "
            "nodes to the III OpenCLI collector binding.",
            missing=["canvas_resource_resolution"],
            tags=["source", "opencli", "hda"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.source.opencli-slot.v1",
                input_ports=[_port("in", "trigger")],
                output_ports=[_port("out", "items[]")],
                resources=[
                    "opencli_channel",
                    "sourceOutputs_or_bound_task_or_worker_dispatch",
                ],
                permissions=["canFetchNetwork"],
                runtime_binding=OPENCLI_BINDING_ID,
                trace_events=[
                    "batch_ready",
                    "sourceOutputs",
                    "bound_task_records",
                    "completed",
                ],
                probes=["opencli_adapter_registered", "source_output_ingest_available"],
            ),
        ),
        _capability(
            id="intelligence.source.feishu-table",
            label="Feishu Bitable Keywords",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="feishu",
            channel_type="feishu_table",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Reads a bounded, read-only Feishu Bitable keyword table through "
                "the encrypted source credential boundary."
            ),
            missing=["source_id_with_feishu_token"],
            tags=["source", "feishu", "bitable", "keywords", "live"],
            source="backend.channels.feishu_table_channel",
            manifest={
                **_manifest(
                    schema="capability.source.feishu-table.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=["source_credentials", "feishu_table_channel"],
                    permissions=["network.fetch"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                    probes=["feishu_source_configured", "feishu_token_configured"],
                ),
                "canvas": {"node": True},
                "presentation": {
                    "icon": "Table2",
                    "description": "只读读取飞书多维表格中的搜索词；凭证通过数据源连接配置。",
                    "parameters": [
                        {
                            "name": "sourceId",
                            "label": "数据源连接",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "appToken",
                            "label": "Base / App token",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "tableId",
                            "label": "Table ID",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "keywordField",
                            "label": "关键词列",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "statusField",
                            "label": "状态列",
                            "type": "string",
                            "required": False,
                        },
                        {
                            "name": "eligibleStatus",
                            "label": "可采集状态",
                            "type": "string",
                            "required": False,
                        },
                        {
                            "name": "maxRows",
                            "label": "最多读取行数",
                            "type": "number",
                            "required": False,
                            "default": 500,
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.ecommerce-platform",
            label="E-commerce Platform",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="browser_act",
            channel_type="browser_act",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Runs the vendored ecommerce-platform BrowserAct pack for "
                "listing, product detail, or review extraction from a rendered "
                "e-commerce page."
            ),
            tags=["source", "ecommerce", "browser-act", "products", "reviews", "live"],
            source="backend.browser_act_packs.ecommerce.ecommerce-platform",
            manifest={
                **_manifest(
                    schema="capability.source.ecommerce-platform.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=["browser_act_channel", "ecommerce-platform_pack", "cdp_browser_session"],
                    permissions=["network.fetch", "canFetchNetwork"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                ),
                "canvas": {"node": True},
                "nodeCatalog": {
                    "authority": "backend",
                    "origin": "source-preset",
                    "category": "source",
                    "kind": "source",
                    "capability": "fetch",
                    "adapter": {
                        "id": "ecommerce-platform-source",
                        "type": "source",
                        "provider": "browser_act",
                        "mode": "live",
                        "config": {
                            "channel": "browser_act",
                            "channelType": "browser_act",
                            "pack": "ecommerce/ecommerce-platform",
                        },
                    },
                },
                "presentation": {
                    "icon": "ShoppingBag",
                    "description": "读取电商平台商品列表、商品详情或评论；支持已登录 Chrome 会话。",
                    "parameters": [
                        {
                            "name": "url",
                            "label": "商品或列表页 URL",
                            "type": "string",
                            "required": True,
                            "default": "",
                        },
                        {
                            "name": "platform",
                            "label": "平台",
                            "type": "string",
                            "required": False,
                            "default": "auto",
                        },
                        {
                            "name": "operation",
                            "label": "操作",
                            "type": "string",
                            "required": False,
                            "default": "detail",
                        },
                        {
                            "name": "max_results",
                            "label": "最多结果数",
                            "type": "number",
                            "required": False,
                            "default": 20,
                        },
                        {
                            "name": "max_pages",
                            "label": "最多页数",
                            "type": "number",
                            "required": False,
                            "default": 5,
                        },
                        {
                            "name": "cdp_endpoint",
                            "label": "Chrome CDP 地址",
                            "type": "string",
                            "required": False,
                            "default": "",
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.kuaishou-search",
            label="Kuaishou Video Search",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="source",
            capability="fetch",
            provider="kuaishou",
            channel_type="kuaishou_search",
            runtime_binding=SOURCE_FETCH_BINDING_ID,
            reason=(
                "Runs keyword video search through the logged-in Kuaishou "
                "browser session, with optional detail-page comments."
            ),
            tags=["source", "video", "kuaishou", "search", "live"],
            source="backend.channels.kuaishou_search_channel",
            manifest={
                **_manifest(
                    schema="capability.source.kuaishou-search.v1",
                    input_ports=[_port("in", "trigger")],
                    output_ports=[_port("out", "items[]")],
                    resources=["kuaishou_search_channel", "cdp_browser_session"],
                    permissions=["network.fetch", "canFetchNetwork"],
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                ),
                "canvas": {"node": True},
                "nodeCatalog": {
                    "authority": "backend",
                    "origin": "source-preset",
                    "category": "source",
                    "kind": "source",
                    "capability": "fetch",
                    "adapter": {
                        "id": "kuaishou-search-source",
                        "type": "source",
                        "provider": "kuaishou",
                        "mode": "live",
                        "config": {
                            "channel": "kuaishou_search",
                            "channelType": "kuaishou_search",
                        },
                    },
                },
                "presentation": {
                    "icon": "Video",
                    "description": (
                        "通过已登录浏览器按关键词搜索快手视频；可选采集详情页评论。"
                    ),
                    "parameters": [
                        {
                            "name": "query",
                            "label": "搜索关键词",
                            "type": "string",
                            "required": True,
                            "default": "ai",
                        },
                        {
                            "name": "limit",
                            "label": "最多结果数",
                            "type": "number",
                            "required": False,
                            "default": 10,
                        },
                        {
                            "name": "with_comments",
                            "label": "采集评论",
                            "type": "boolean",
                            "required": False,
                            "default": False,
                        },
                        {
                            "name": "comment_limit",
                            "label": "每条视频评论数",
                            "type": "number",
                            "required": False,
                            "default": 20,
                        },
                        {
                            "name": "max_comment_videos",
                            "label": "采集评论的视频数",
                            "type": "number",
                            "required": False,
                            "default": 5,
                        },
                        {
                            "name": "cdp_endpoint",
                            "label": "Chrome CDP 地址",
                            "type": "string",
                            "required": False,
                            "default": "",
                        },
                    ],
                },
            },
        ),
        _capability(
            id="intelligence.source.pool",
            label="Source Pool",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="workflow",
            runtime_binding=SOURCE_POOL_BINDING_ID,
            reason="OpenCLI HDA internals use this real workflow node to "
            "fan out package demand to source slots in parallel.",
            tags=["source", "pool", "fanout", "hda"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.source.pool.v1",
                input_ports=[_port("in", "trigger")],
                output_ports=[_port("out", "trigger")],
                resources=["capability_catalog"],
                permissions=[],
                runtime_binding=SOURCE_POOL_BINDING_ID,
                trace_events=["partial:sourceCount", "completed"],
                probes=["source_slots_present"],
            ),
        ),
        _capability(
            id="intelligence.processing.normalize",
            label="Normalize Items",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="workflow",
            runtime_binding=NORMALIZE_BINDING_ID,
            reason="OpenCLI Admin owns normalization as a native Transform node "
            "that turns source items into Record Candidates while preserving "
            "source references.",
            tags=["transform", "normalize", "record-candidate", "lineage"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.transform.normalize.v1",
                input_ports=[_port("in", "items[]")],
                output_ports=[_port("out", "recordCandidate[]")],
                resources=[],
                permissions=[],
                runtime_binding=NORMALIZE_BINDING_ID,
                trace_events=["partial:recordCandidate[]", "completed"],
                probes=["normalizer_import_available"],
            ),
        ),
        _capability(
            id="intelligence.processing.dedupe",
            label="Dedupe Items",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="dedupe",
            provider="workflow",
            runtime_binding=DEDUPE_BINDING_ID,
            reason="Batch deduplication runs through the shared RecordHygiene module and "
            "emits duplicate evidence plus deterministic metrics.",
            tags=[
                "transform",
                "dedupe",
                "record-candidate",
                "record-hygiene",
                "lineage",
            ],
            source="backend.workflow.record_hygiene",
            manifest=_manifest(
                schema="capability.transform.dedupe.v1",
                input_ports=[_port("in", "recordCandidate[]")],
                output_ports=[_port("out", "recordCandidate[]")],
                resources=[],
                permissions=[],
                runtime_binding=DEDUPE_BINDING_ID,
                trace_events=[
                    "partial:deduplicatedCandidateCount",
                    "partial:rejectedCount",
                    "partial:metrics",
                    "completed",
                ],
                probes=[
                    "record_hygiene_module_available",
                    "dedupe_runtime_binding_available",
                ],
            ),
        ),
        _capability(
            id="intelligence.flow.merge",
            label="Merge",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="flow",
            capability="merge",
            provider="workflow",
            runtime_binding=MERGE_BINDING_ID,
            reason="OpenCLI Admin owns typed fan-in as a native Flow node. "
            "The first loop supports concat while preserving lineage.",
            tags=["flow", "merge", "lineage", "typed-port"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.flow.merge.v1",
                input_ports=[
                    _port("in1", "recordCandidate[]"),
                    _port("in2", "recordCandidate[]"),
                ],
                output_ports=[_port("out", "recordCandidate[]")],
                resources=[],
                permissions=[],
                runtime_binding=MERGE_BINDING_ID,
                trace_events=["partial:mergedCandidateCount", "completed"],
                probes=["typed_port_contract_registered"],
            ),
        ),
        *_data_operator_capabilities(),
        _blocked_catalog(
            "intelligence.agent.summary",
            "LLM Summary",
            "agent",
            "summarize",
            backend_available=True,
            missing=["provider_resource_binding", "workflow_agent_executor"],
        ),
        _blocked_catalog("intelligence.agent.score", "Importance Score", "agent", "score"),
        _blocked_catalog("intelligence.agent.tag", "Auto Tag", "agent", "tag"),
        _blocked_catalog(
            "intelligence.router.importance",
            "Importance Router",
            "router",
            "route",
            missing=["workflow_router_executor"],
        ),
        _capability(
            id="intelligence.control.record-acceptance",
            label="Record Acceptance Gate",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="control",
            capability="accept",
            provider="workflow",
            runtime_binding=RECORD_ACCEPTANCE_BINDING_ID,
            reason="Record acceptance is a native Gate node that promotes "
            "Record Candidates to Records only after schema, dedupe, quality, "
            "and lineage checks.",
            tags=["control", "gate", "record", "quality", "lineage"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.control.record-acceptance.v1",
                input_ports=[_port("candidates", "recordCandidate[]")],
                output_ports=[_port("records", "record[]")],
                resources=["record_schema_registry"],
                permissions=["record_acceptance_policy"],
                runtime_binding=RECORD_ACCEPTANCE_BINDING_ID,
                trace_events=[
                    "partial:acceptedRecordCount",
                    "partial:reviewRequiredCount",
                    "completed",
                ],
                probes=["record_schema_available", "lineage_required_check"],
            ),
        ),
        _blocked_catalog(
            "intelligence.output.inbox",
            "Inbox Store",
            "inbox",
            "store",
            backend_available=True,
            missing=["workflow_storage_sink_binding"],
        ),
        _capability(
            id="intelligence.output.collection-result",
            label="Collection Output",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="inbox",
            capability="store",
            provider="workflow",
            runtime_binding=COLLECTION_OUTPUT_BINDING_ID,
            reason="OpenCLI HDA internals expose normalized items through this "
            "real package output boundary.",
            tags=["output", "items", "hda"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.output.collection-result.v1",
                input_ports=[_port("in", "recordCandidate[]")],
                output_ports=[_port("out", "items[]")],
                resources=["run_trace"],
                permissions=[],
                runtime_binding=COLLECTION_OUTPUT_BINDING_ID,
                trace_events=["partial:itemCount", "completed"],
                probes=["package_output_boundary_available"],
            ),
        ),
        _capability(
            id="intelligence.sink.records",
            label="Record Sink",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="sink",
            capability="store",
            provider="workflow",
            runtime_binding=RECORD_SINK_BINDING_ID,
            reason="Accepted Records write through this native sink boundary "
            "instead of raw scrape output entering records directly.",
            tags=["sink", "records", "lineage"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.sink.records.v1",
                input_ports=[_port("records", "record[]")],
                output_ports=[_port("stored", "storedItems[]")],
                resources=[
                    "data_sources",
                    "collection_tasks",
                    "collected_records",
                ],
                permissions=["canWriteInbox"],
                runtime_binding=RECORD_SINK_BINDING_ID,
                trace_events=["partial:storedRefs", "completed"],
                probes=["record_table_available", "task_source_ownership_available"],
            ),
        ),
        _capability(
            id="intelligence.sink.feishu-bitable",
            label="Feishu Bitable",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="sink",
            capability="store",
            provider="feishu",
            runtime_binding=FEISHU_BITABLE_SINK_BINDING_ID,
            reason="Stored certified Records are delivered through a saved encrypted connection.",
            tags=["sink", "feishu", "bitable", "delivery"],
            source="backend.workflow.runtime_registry",
            manifest=_manifest(
                schema="capability.sink.feishu-bitable.v1",
                input_ports=[_port("records", "storedItems[]")],
                output_ports=[_port("delivery", "deliveryAttempt[]")],
                resources=["delivery_connections", "delivery_attempts"],
                permissions=[],
                runtime_binding=FEISHU_BITABLE_SINK_BINDING_ID,
                trace_events=["partial:deliveryAttempts", "blocked", "completed"],
                probes=["saved_connection", "existing_bitable_target"],
            ),
        ),
        _capability(
            id="intelligence.output.webhook",
            label="Webhook Notify",
            surface="catalog",
            status="blocked",
            backend_available=True,
            kind="notify",
            capability="send",
            provider="webhook",
            notifier_type="webhook",
            runtime_binding=WEBHOOK_NOTIFY_BINDING_ID,
            reason="Backend notifier and real workflow delivery path exist; "
            "each run still requires send permission, an upstream EvidenceBatch "
            "projection, and a configured webhook URL.",
            missing=[
                "evidencebatch_projection_input",
                "send_permission",
                "webhook_url_configuration",
            ],
            tags=["catalog", "notify", "webhook"],
            source="backend.workflow.runtime_registry",
        ),
        _capability(
            id="intelligence.output.turbopush-publish",
            label="TurboPush Publish",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="notify",
            capability="send",
            provider=TURBOPUSH_PROVIDER,
            notifier_type=TURBOPUSH_PROVIDER,
            runtime_binding=TURBOPUSH_BINDING_ID,
            reason="Backend workflow compile resolves this node to the local "
            "TurboPush MCP/HTTP publishing flow: logged accounts, platform "
            "setting schemas, content creation, SSE publish, and records.",
            missing=["local_turbopush_service_when_not_running", "send_permission"],
            tags=["catalog", "notify", "publish", "turbopush"],
            source="backend.workflow.turbopush_runtime",
        ),
        _capability(
            id="external.tool.capability",
            label="Imported Tool Capability",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="action",
            capability="store",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason="LangGraph, LangChain, and other external runtime tool nodes "
            "import as OpenCLI Admin Tool Capability placeholders. The original "
            "runtime remains provenance only; each node must still provide an "
            "OpenCLI Admin toolCapability binding before execution.",
            missing=["node_level_tool_capability_binding_when_unconfigured"],
            tags=["catalog", "external-runtime", "tool-capability", "import"],
            source="backend.workflow.external_importer",
            manifest=_manifest(
                schema="capability.external.tool.v1",
                input_ports=[_port("in", "unknown")],
                output_ports=[_port("out", "unknown")],
                resources=[
                    "capability_catalog",
                    "tool_capability_registry",
                    "external_workflow_origin",
                    "node_params.toolCapability",
                ],
                permissions=["canvas_review_required"],
                runtime_binding=EXTERNAL_TOOL_BINDING_ID,
                trace_events=[
                    "blocked:missing_tool_capability_binding",
                    "tool_call_started",
                    "partial:outputItemCount",
                    "tool_call_completed",
                    "completed",
                ],
                probes=[
                    "external_origin_metadata_present",
                    "tool_capability_binding_present_when_runnable",
                ],
            ),
        ),
        _blocked_catalog(
            "package.collection.pipeline",
            "Collection Pipeline",
            "source",
            "fetch",
            backend_available=True,
            reason="The package describes existing channel capabilities, but is "
            "not generated from the backend channel registry yet.",
            missing=["channel_capability_projection", "package_materializer"],
        ),
        _capability(
            id="package.processing.record-hygiene",
            label="Record Hygiene",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="workflow",
            reason="Structural package composing Normalize, Dedupe, and Record Acceptance. "
            "Its locked internal nodes remain the executable runtime authority.",
            tags=["package", "structural", "composed", "record-hygiene"],
            source="backend.workflow.record_hygiene",
            manifest={
                "schema": "capability.package.record-hygiene.v1",
                "ports": {
                    "inputs": [_port("in", "items[]")],
                    "outputs": [_port("out", "record[]")],
                },
                "artifacts": {
                    "trace": ["rejected[]", "metrics"],
                    "routable": False,
                },
                "canvas": {
                    "node": True,
                    "structural": True,
                    "composed": True,
                    "managedInternals": True,
                },
                "implementation": {
                    "executor": None,
                    "authority": "locked-internal-nodes",
                    "internalCatalogIds": [
                        "intelligence.processing.normalize",
                        "intelligence.processing.dedupe",
                        "intelligence.control.record-acceptance",
                    ],
                },
            },
        ),
        _capability(
            id="package.opencli.multi-source-hda",
            label="多站点数据采集",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="opencli",
            channel_type="opencli",
            runtime_binding=OPENCLI_BINDING_ID,
            reason="This package materializes params.sources into real OpenCLI "
            "source/fetch nodes. OpenCLI itself is the node capability; the "
            "package is only a composition wrapper.",
            missing=["canvas_resource_resolution", "projection_workbench"],
            tags=["package", "hda", "opencli"],
            source="backend.workflow.opencli_hda_tracer",
        ),
        _capability(
            id="package.gaojixing.doubao-batch",
            label="豆包证据批次采集",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason=(
                "Materializes to the registered governed Gaojixing Doubao batch Tool Capability. "
                "offline_fixture and project_archive are executable evidence audits; "
                "live_preflight is a non-mutating readiness check. New live search is not enabled."
            ),
            missing=[],
            tags=["package", "gaojixing", "doubao", "evidence", "batch"],
            source="backend.workflow.hda_templates",
        ),
        _capability(
            id="package.gaojixing.batch-certification",
            label="批次终审与交付",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason=(
                "Materializes to the registered规范2.2 structural certification Tool "
                "Capability; screenshot pixels and OCR content are not authenticated."
            ),
            missing=[],
            tags=["package", "gaojixing", "certification", "evidence"],
            source="backend.workflow.hda_templates",
        ),
        _capability(
            id="package.intelligence.situation-awareness",
            label="近 30 天事态感知",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason="Materializes to the registered situation-awareness Tool Capability.",
            missing=[],
            tags=["package", "hda", "research", "situation-awareness"],
            source="backend.workflow.hda_templates",
        ),
        _capability(
            id="package.simulation.swarm-forecast",
            label="群体智能推演",
            surface="catalog",
            status="runnable",
            backend_available=True,
            kind="agent",
            capability="normalize",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason="Materializes to the registered local or MiroFish swarm Tool Capability.",
            missing=["mirofish_provider_credentials_when_selected"],
            tags=["package", "hda", "simulation", "swarm", "mirofish"],
            source="backend.workflow.hda_templates",
        ),
        _capability(
            id="package.intelligence.native-lifecycle",
            label="采集研究与报告",
            surface="catalog",
            status=native_package_status,
            backend_available=native_package_status == "runnable",
            kind="agent",
            capability="normalize",
            provider="opencli-admin",
            runtime_binding=EXTERNAL_TOOL_BINDING_ID,
            reason=(
                "Credential-free HDA composed from machine-certified native lifecycle "
                "Tool Capabilities on the WorkflowRun/Event spine."
            ),
            missing=native_package_missing,
            tags=["package", "hda", "intelligence", "native", "offline"],
            source="backend.workflow.hda_templates",
            manifest={
                "presentation": {
                    "parameters": [
                        {
                            "name": "seed",
                            "label": "随机种子 / Seed",
                            "type": "integer",
                            "default": 0,
                            "minimum": 0,
                        },
                        {
                            "name": "personaCount",
                            "label": "人物数量 / Persona count",
                            "type": "integer",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 100,
                        },
                        {
                            "name": "maxRounds",
                            "label": "推演轮数 / Simulation rounds",
                            "type": "integer",
                            "default": 3,
                            "minimum": 1,
                            "maximum": 100,
                        },
                        {
                            "name": "agentCount",
                            "label": "推演 Agent 数 / Agent count",
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                        {
                            "name": "requirement",
                            "label": "推演要求 / Simulation requirement",
                            "type": "string",
                            "default": "Explore evidence-grounded reactions.",
                        },
                        {
                            "name": "platforms",
                            "label": "模拟平台 / Platforms",
                            "type": "array",
                            "default": ["twitter", "reddit"],
                        },
                        {
                            "name": "question",
                            "label": "报告问题 / Report question",
                            "type": "string",
                            "default": "What is the most likely evidence-grounded outcome?",
                        },
                    ]
                },
                "readiness": {
                    "status": native_package_status,
                    "childCount": len(native_tools),
                    "expectedChildCount": len(expected_native_children),
                    "blockedChildren": sorted({tool.id for tool in native_blocked}),
                    "missingReasons": native_package_missing,
                    "missingActions": missing_actions,
                    "extraActions": extra_actions,
                    "duplicateActions": duplicate_actions,
                    "missingToolIds": missing_tool_ids,
                    "extraToolIds": extra_tool_ids,
                    "duplicateToolIds": duplicate_tool_ids,
                    "missingChildren": missing_children,
                    "extraChildren": extra_children,
                },
            },
        ),
        _blocked_catalog(
            "package.dispatch.fanout",
            "Dispatch Fanout",
            "notify",
            "send",
            backend_available=True,
            missing=["workflow_notifier_sink_binding", "fanout_materializer"],
        ),
        _blocked_catalog(
            "package.intelligence.pipeline",
            "Intelligence Pipeline",
            "agent",
            "normalize",
        ),
        _blocked_catalog("package.ops.event", "Ops Event", "action", "send"),
        _blocked_catalog("package.ops.monitor-guard", "Monitor Guard", "router", "route"),
        _blocked_catalog(
            "package.ops.alert-response",
            "Alert Response",
            "notify",
            "send",
            backend_available=True,
            missing=["workflow_notifier_sink_binding", "ticket_sink_binding"],
        ),
        _blocked_catalog(
            "package.ai.prompt-experiment",
            "Prompt Experiment",
            "agent",
            "summarize",
            backend_available=True,
            missing=["provider_resource_binding", "experiment_executor"],
        ),
        _blocked_catalog(
            "package.verify.regression-gate",
            "Regression Gate",
            "router",
            "route",
            missing=["evaluator_runtime_binding"],
        ),
        _blocked_catalog("package.map.knowledge-map", "Knowledge Map", "action", "store"),
        _blocked_catalog(
            "package.review.human-review",
            "Human Review",
            "inbox",
            "store",
            backend_available=True,
            missing=["workflow_review_sink_binding"],
        ),
    ]


def _data_operator_capabilities() -> list[WorkflowRuntimeCapability]:
    specs_by_kind: dict[str, list[object]] = {}
    for spec in list_data_operator_specs():
        specs_by_kind.setdefault(spec.kind, []).append(spec)

    rows: list[WorkflowRuntimeCapability] = []
    for catalog_id, binding_id in DATA_OPERATOR_CATALOG_BINDINGS.items():
        operator_kind = catalog_id.rsplit(".", 1)[-1]
        specs = sorted(
            specs_by_kind.get(operator_kind, []),
            key=lambda spec: spec.operator_id,
        )
        if not specs:
            continue
        operators = [
            {
                "id": spec.operator_id,
                "operatorId": spec.operator_id,
                "kind": spec.kind,
                "label": spec.label,
                "description": spec.description,
                "pack": spec.pack_id,
                "packId": spec.pack_id,
                "version": spec.pack_version,
                "packVersion": spec.pack_version,
                "status": "runnable",
                "readiness": "ready",
                "configKeys": list(spec.config_keys),
            }
            for spec in specs
        ]
        rows.append(
            _capability(
                id=catalog_id,
                label=f"Data {operator_kind.title()}",
                surface="catalog",
                status="runnable",
                backend_available=True,
                kind="agent",
                capability="normalize",
                provider="workflow",
                runtime_binding=binding_id,
                reason="Registered versioned data operators execute on Record Candidates.",
                tags=["data", "operator", operator_kind],
                source="backend.workflow.data_operators",
                manifest={
                    **_manifest(
                        schema=f"capability.data.{operator_kind}.v1",
                        input_ports=[_port("in", "recordCandidate[]")],
                        output_ports=[_port("out", "recordCandidate[]")],
                        runtime_binding=binding_id,
                        trace_events=[
                            "partial:outputItemCount",
                            "completed",
                            "failed",
                        ],
                        probes=["data_operator_registry"],
                    ),
                    "operatorIds": list(dict.fromkeys(operator["id"] for operator in operators)),
                    "operators": operators,
                    "packs": sorted({operator["packId"] for operator in operators}),
                    "params": list(
                        dict.fromkeys(key for spec in specs for key in spec.config_keys)
                    ),
                    "artifacts": [
                        "recordCandidate[]",
                        "metrics",
                        "rejectedCandidateIds",
                    ],
                },
            )
        )
    return rows


def _dify_runtime_ready(installations: list[PluginInstallationRead]) -> bool:
    return any(
        installation.id == "bundled:dify-graphon-runtime" and installation.runtime_status == "READY"
        for installation in installations
    )


def _plugin_catalog_capabilities(
    installations: list[PluginInstallationRead],
) -> list[WorkflowRuntimeCapability]:
    projected: list[WorkflowRuntimeCapability] = []
    for installation in installations:
        if installation.bundled:
            continue
        for node in installation.node_definitions:
            kind, capability = _plugin_node_shape(node.family)
            projected.append(
                _capability(
                    id=node.id,
                    label=node.label,
                    surface="catalog",
                    status="blocked" if node.status == "BLOCKED" else "runnable",
                    backend_available=node.status == "READY",
                    kind=kind,
                    capability=capability,
                    provider=installation.provider_key,
                    runtime_binding=None,
                    reason=(
                        node.lock_reason
                        or "This plugin capability has no compatible OpenCLI runtime adapter."
                    ),
                    missing=(["dify_plugin_runtime_adapter"] if node.status == "BLOCKED" else []),
                    tags=[
                        "plugin",
                        "dify",
                        node.family,
                        installation.provider_key,
                        installation.version,
                    ],
                    source="backend.services.plugin_registry_service",
                    manifest={
                        "schema": "capability.plugin-projection.v1",
                        "plugin": {
                            "installationId": installation.id,
                            "providerKey": installation.provider_key,
                            "version": installation.version,
                            "capabilityId": node.capability_id,
                            "family": node.family,
                        },
                        "canvas": {
                            "node": True,
                            "locked": node.locked,
                            "lockReason": node.lock_reason,
                        },
                    },
                )
            )
    return projected


def _plugin_node_shape(
    family: str,
) -> tuple[WorkflowNodeKind, WorkflowCapability]:
    if family == "datasource":
        return "source", "fetch"
    if family == "trigger":
        return "schedule", "trigger"
    if family == "agent_strategy":
        return "agent", "summarize"
    return "action", "store"


def _manifest(
    *,
    schema: str,
    input_ports: list[dict[str, str]] | None = None,
    output_ports: list[dict[str, str]] | None = None,
    resources: list[str] | None = None,
    permissions: list[str] | None = None,
    runtime_binding: str | None = None,
    trace_events: list[str] | None = None,
    probes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema": schema,
        "ports": {
            "inputs": input_ports or [],
            "outputs": output_ports or [],
        },
        "resources": resources or [],
        "permissions": permissions or [],
        "runtime": {
            "binding": runtime_binding,
        },
        "trace": {
            "events": trace_events or ["queued", "started", "partial", "completed"],
        },
        "probes": probes or [],
    }


def _port(name: str, type: str) -> dict[str, str]:
    return {"name": name, "type": type}


def _blocked_catalog(
    id: str,
    label: str,
    kind: WorkflowNodeKind,
    capability: WorkflowCapability,
    *,
    backend_available: bool = False,
    reason: str | None = None,
    missing: list[str] | None = None,
) -> WorkflowRuntimeCapability:
    return _capability(
        id=id,
        label=label,
        surface="catalog",
        status="blocked",
        backend_available=backend_available,
        kind=kind,
        capability=capability,
        reason=reason
        or "The node is visible in Canvas, but no authoritative workflow "
        "runtime binding exists yet.",
        missing=missing or ["workflow_runtime_binding"],
        tags=["catalog", kind, capability],
    )


def _primitive_capabilities() -> list[WorkflowRuntimeCapability]:
    special = {
        "primitive.core.webhook-trigger": _capability(
            id="primitive.core.webhook-trigger",
            label="Webhook Trigger",
            surface="primitive",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            runtime_binding=WEBHOOK_TRIGGER_BINDING_ID,
            reason="The workflow webhook input contract has HTTP ingress and run dispatch.",
            missing=[],
            tags=["primitive", "webhook", "trigger"],
        ),
        "primitive.ops.trigger-webhook": _capability(
            id="primitive.ops.trigger-webhook",
            label="Webhook Trigger",
            surface="primitive",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            runtime_binding=WEBHOOK_TRIGGER_BINDING_ID,
            reason="The workflow webhook input contract has HTTP ingress and run dispatch.",
            missing=[],
            tags=["primitive", "webhook", "trigger"],
        ),
        "primitive.ops.action-webhook": _capability(
            id="primitive.ops.action-webhook",
            label="Webhook Action",
            surface="primitive",
            status="blocked",
            backend_available=True,
            kind="notify",
            capability="send",
            reason="Backend webhook notifier and the catalog sink contract exist, "
            "but primitive action execution and projection delivery are not bound.",
            missing=["primitive_executor_binding", "delivery_projection"],
            tags=["primitive", "webhook", "notify"],
        ),
        "primitive.core.respond-webhook": _capability(
            id="primitive.core.respond-webhook",
            label="Respond to Webhook",
            surface="primitive",
            status="blocked",
            backend_available=False,
            kind="notify",
            capability="send",
            reason="Respond-to-webhook requires workflow run input envelopes and "
            "projection APIs before it can be real.",
            missing=["runtime_input_envelope", "projection_api"],
            tags=["primitive", "webhook", "response"],
        ),
    }

    rows: list[WorkflowRuntimeCapability] = []
    for primitive_id in sorted(WORKFLOW_PRIMITIVE_IDS):
        rows.append(
            special.get(primitive_id)
            or _capability(
                id=primitive_id,
                label=_label_from_id(primitive_id),
                surface="primitive",
                status="design_only",
                backend_available=False,
                reason="Primitive ids are accepted as import/design vocabulary, "
                "but no primitive executor binding exists yet.",
                missing=["primitive_executor_binding"],
                tags=["primitive"],
            )
        )
    return rows


def _channel_capabilities() -> list[WorkflowRuntimeCapability]:
    rows: list[WorkflowRuntimeCapability] = []
    for channel_type in sorted(list_channel_types()):
        if channel_type == "opencli":
            rows.append(
                _capability(
                    id=f"channel.{channel_type}",
                    label="OpenCLI channel",
                    surface="channel",
                    status="runnable",
                    backend_available=True,
                    kind="source",
                    capability="fetch",
                    provider="opencli",
                    channel_type=channel_type,
                    runtime_binding=OPENCLI_BINDING_ID,
                    reason="The workflow runtime registry resolves OpenCLI "
                    "source/fetch nodes to this channel.",
                    missing=["canvas_resource_resolution"],
                    tags=["channel", "source", "opencli"],
                    source="backend.channels.opencli_channel",
                )
            )
            continue
        if channel_type == "kuaishou_search":
            rows.append(
                _capability(
                    id=f"channel.{channel_type}",
                    label="Kuaishou search channel",
                    surface="channel",
                    status="runnable",
                    backend_available=True,
                    kind="source",
                    capability="fetch",
                    provider="kuaishou",
                    channel_type=channel_type,
                    runtime_binding=SOURCE_FETCH_BINDING_ID,
                    reason=(
                        "The workflow runtime dispatches Kuaishou keyword search "
                        "through the logged-in browser-bound channel."
                    ),
                    tags=["channel", "source", "kuaishou", "video"],
                    source="backend.channels.kuaishou_search_channel",
                )
            )
            continue

        rows.append(
            _capability(
                id=f"channel.{channel_type}",
                label=f"{channel_type} channel",
                surface="channel",
                status="blocked",
                backend_available=True,
                kind="source",
                capability="fetch",
                provider=channel_type,
                channel_type=channel_type,
                reason="A real DataSource channel exists, but it has not been "
                "projected into Canvas source nodes or workflow runtime binding.",
                missing=["canvas_source_projection", "workflow_runtime_binding"],
                tags=["channel", "source"],
                source=f"backend.channels.{channel_type}_channel",
            )
        )
    return rows


def _notifier_capabilities() -> list[WorkflowRuntimeCapability]:
    rows: list[WorkflowRuntimeCapability] = []
    for notifier_type in sorted(list_notifier_types()):
        if notifier_type == "webhook":
            rows.append(
                _capability(
                    id="notifier.webhook",
                    label="webhook notifier",
                    surface="notifier",
                    status="blocked",
                    backend_available=True,
                    kind="notify",
                    capability="send",
                    provider="webhook",
                    notifier_type="webhook",
                    runtime_binding=WEBHOOK_NOTIFY_BINDING_ID,
                    reason="The guarded webhook notifier is wired into workflow "
                    "delivery; each run still requires projection input and URL "
                    "configuration.",
                    missing=[
                        "evidencebatch_projection_input",
                        "webhook_url_configuration",
                    ],
                    tags=["notifier", "output", "webhook"],
                    source="backend.notifiers.webhook_notifier",
                )
            )
            continue

        rows.append(
            _capability(
                id=f"notifier.{notifier_type}",
                label=f"{notifier_type} notifier",
                surface="notifier",
                status="blocked",
                backend_available=True,
                kind="notify",
                capability="send",
                provider=notifier_type,
                notifier_type=notifier_type,
                reason="The notifier exists, but Canvas output nodes do not yet "
                "bind to this notifier type.",
                missing=["workflow_notifier_sink_binding", "delivery_projection"],
                tags=["notifier", "output"],
            )
        )
    return rows


def _trigger_capabilities() -> list[WorkflowRuntimeCapability]:
    return [
        _capability(
            id="trigger.manual",
            label="Manual workflow run",
            surface="trigger",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            reason="Frontend Canvas Run calls the backend workflow run API and "
            "replays node events onto existing Canvas nodes. User collection "
            "needs are drafted separately before the run starts.",
            missing=[],
            tags=["trigger", "manual"],
        ),
        _capability(
            id="trigger.webhook",
            label="Inbound webhook trigger",
            surface="trigger",
            status="runnable",
            backend_available=True,
            kind="schedule",
            capability="trigger",
            runtime_binding=WEBHOOK_TRIGGER_BINDING_ID,
            reason="The workflow webhook input contract has HTTP ingress and run dispatch.",
            missing=[],
            tags=["trigger", "webhook"],
        ),
    ]


def _resource_capabilities() -> list[WorkflowRuntimeCapability]:
    opencli_adapter_summary = get_opencli_adapter_node_summary()
    rows = [
        _resource("resource.source-credentials", "Source credentials"),
        _resource("resource.cookie-jar", "Cookie/session state"),
        _resource("resource.browser-profile", "Browser profile binding"),
        _resource("resource.browser-worker-pool", "Browser worker pool"),
        _resource("resource.turbopush-local-service", "TurboPush local service"),
        _capability(
            id="resource.workflow-fleet-runtime",
            label="Workflow Fleet Runtime Projection",
            surface="resource",
            status="runnable",
            backend_available=True,
            provider="opencli-admin",
            runtime_binding="workflow.fleet.inventory",
            reason=(
                "Existing browser pool, HTTP/WS agents, EdgeNode state, and "
                "site bindings are projected into a workflow-runtime fleet view."
            ),
            missing=[],
            tags=["resource", "fleet", "agent", "browser-pool"],
            source="backend.workflow.fleet_inventory",
            manifest={
                "schema": "resource.workflow-fleet-runtime.v1",
                "canvas": {"node": False},
                "endpoints": {
                    "inventory": "/api/v1/workflows/fleet/inventory",
                    "match": "/api/v1/workflows/fleet/match",
                },
                "inputs": [
                    "browser_pool",
                    "browser_instances",
                    "edge_nodes",
                    "browser_bindings",
                    "ws_agent_connections",
                    "opencli_adapter_nodes",
                ],
                "trace": {
                    "events": [
                        "fleet_agent_selected",
                        "fleet_dispatch_started",
                        "fleet_dispatch_completed",
                    ]
                },
            },
        ),
        _capability(
            id="resource.opencli-adapter-nodes",
            label="OpenCLI Adapter Node Registry",
            surface="resource",
            status="runnable" if opencli_adapter_summary.get("total") else "blocked",
            backend_available=bool(opencli_adapter_summary.get("total")),
            provider="opencli",
            runtime_binding=OPENCLI_BINDING_ID,
            reason=(
                "Every OpenCLI adapter command is projected as a stable node "
                "manifest. Read adapters materialize through OpenCLI Source Slot; "
                "write adapters require Tool Capability review."
            ),
            missing=[] if opencli_adapter_summary.get("total") else ["opencli_catalog"],
            tags=["resource", "opencli", "adapter-node-registry"],
            source="backend.workflow.opencli_adapter_nodes",
            manifest={
                "schema": "resource.opencli-adapter-node-registry.v1",
                "endpoint": "/api/v1/workflows/opencli-adapter-nodes",
                "summary": opencli_adapter_summary,
                "canvas": {"node": False},
                "catalogModel": {
                    "kind": "core_nodes_plus_presets",
                    "coreNodes": {
                        "endpoint": "/api/v1/workflows/capabilities",
                        "role": "node_definition",
                    },
                    "adapterCommands": {
                        "endpoint": "/api/v1/workflows/opencli-adapter-nodes",
                        "role": "node_preset",
                        "presetKinds": ["source_slot", "tool_capability"],
                    },
                },
                "query": {
                    "filters": [
                        "site",
                        "q",
                        "access",
                        "capability",
                        "browser",
                        "presetKind",
                        "runtimeReadiness",
                    ],
                    "grouping": "facets",
                },
                "materialization": {
                    "readNoRequiredArgs": "intelligence.source.opencli-slot",
                    "readRequiredArgs": "intelligence.source.opencli-slot with params",
                    "write": "external.tool.capability with review",
                },
                "runtime": {"binding": OPENCLI_BINDING_ID},
            },
        ),
    ]
    rows.extend(
        _capability(
            id=f"resource.tool-capability.{tool.id}",
            label=tool.label,
            surface="resource",
            status=tool.status,
            backend_available=tool.status == "runnable",
            provider=tool.provider,
            runtime_binding=_read_manifest_runtime_binding(tool.manifest),
            reason=tool.description,
            missing=[] if tool.status == "runnable" else ["tool_capability_unavailable"],
            tags=["resource", "tool-capability", *tool.tags],
            source="backend.workflow.tool_capabilities",
            manifest={
                **tool.manifest,
                "toolCapability": {
                    "id": tool.id,
                    "versionPin": tool.versionPin.model_dump() if tool.versionPin else None,
                    "inputPorts": [port.model_dump() for port in tool.inputPorts],
                    "outputPorts": [port.model_dump() for port in tool.outputPorts],
                    "executor": tool.executor.model_dump(),
                },
            },
        )
        for tool in list_workflow_tool_capabilities().tools
    )
    return rows


def _tool_catalog_capabilities() -> list[WorkflowRuntimeCapability]:
    rows: list[WorkflowRuntimeCapability] = []
    for tool in list_workflow_tool_capabilities().tools:
        canvas = tool.manifest.get("canvas")
        node_catalog = tool.manifest.get("nodeCatalog")
        if (
            not isinstance(canvas, dict)
            or canvas.get("node") is not True
            or not isinstance(node_catalog, dict)
            or node_catalog.get("authority") != "backend"
        ):
            continue

        catalog_id = node_catalog.get("id")
        kind = node_catalog.get("kind")
        capability = node_catalog.get("capability")
        if (
            not isinstance(catalog_id, str)
            or kind
            not in {
                "schedule",
                "source",
                "agent",
                "router",
                "notify",
                "inbox",
                "action",
                "flow",
                "control",
                "sink",
            }
            or capability
            not in {
                "trigger",
                "fetch",
                "normalize",
                "dedupe",
                "summarize",
                "score",
                "tag",
                "route",
                "send",
                "store",
                "merge",
                "accept",
            }
        ):
            continue

        presentation = tool.manifest.get("presentation")
        presentation = dict(presentation) if isinstance(presentation, dict) else {}
        parameters = presentation.get("parameters")
        parameters = list(parameters) if isinstance(parameters, list) else []
        tool_capability = {
            "id": tool.id,
            "versionPin": tool.versionPin.model_dump() if tool.versionPin else None,
            "inputPorts": [port.model_dump() for port in tool.inputPorts],
            "outputPorts": [port.model_dump() for port in tool.outputPorts],
            "executor": tool.executor.model_dump(),
        }
        manifest = {
            **tool.manifest,
            "toolCapability": tool_capability,
            "presentation": {
                **presentation,
                "parameters": [
                    {
                        "name": "toolCapability",
                        "label": "系统工具绑定 / Tool binding",
                        "type": "object",
                        "required": True,
                        "default": {
                            "id": tool.id,
                            "versionPin": tool_capability["versionPin"],
                            "executor": tool_capability["executor"],
                        },
                    },
                    {
                        "name": "toolParams",
                        "label": "运行参数 / Runtime parameters",
                        "type": "object",
                        "required": False,
                        "default": dict(tool.executor.params),
                    },
                    *parameters,
                ],
            },
        }
        rows.append(
            _capability(
                id=catalog_id,
                label=tool.label,
                surface="catalog",
                status=tool.status,
                backend_available=tool.status == "runnable",
                kind=kind,
                capability=capability,
                provider=tool.provider,
                runtime_binding=_read_manifest_runtime_binding(tool.manifest),
                reason=tool.description,
                missing=([] if tool.status == "runnable" else ["tool_capability_unavailable"]),
                tags=["catalog", "tool-capability", *tool.tags],
                source="backend.workflow.tool_capabilities",
                manifest=manifest,
            )
        )
    return rows


def _resource(id: str, label: str) -> WorkflowRuntimeCapability:
    return _capability(
        id=id,
        label=label,
        surface="resource",
        status="blocked",
        backend_available=True,
        reason="The runtime resource exists in the backend surface, but Canvas "
        "source materialization does not resolve it implicitly yet.",
        missing=["canvas_resource_resolver"],
        tags=["resource"],
    )


def _read_manifest_runtime_binding(manifest: dict[str, object]) -> str | None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        return None
    binding = runtime.get("binding")
    return binding if isinstance(binding, str) else None


def _manifest_with_runtime_contract(
    manifest: dict[str, object],
    runtime_binding: str | None,
) -> dict[str, object]:
    contract = runtime_io_contract_manifest(runtime_binding)
    if contract is None:
        return manifest
    return {**manifest, "contract": contract}


def _label_from_id(value: str) -> str:
    return value.rsplit(".", 1)[-1].replace("-", " ").title()
