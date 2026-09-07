"""Template expansion for package/HDA workflow nodes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from backend.schemas.workflow import (
    WorkflowAdapterBinding,
    WorkflowPackageInternals,
    WorkflowProject,
    WorkflowProjectEdge,
    WorkflowProjectNode,
    WorkflowTopicCollapse,
)
from backend.workflow.gaojixing_certification import (
    GAOJIXING_BATCH_CERTIFY_EXECUTOR,
    GAOJIXING_BATCH_CERTIFY_TOOL_ID,
)
from backend.workflow.gaojixing_doubao import (
    GAOJIXING_DOUBAO_BATCH_EXECUTOR,
    GAOJIXING_DOUBAO_BATCH_TOOL_ID,
    GAOJIXING_FEISHU_WEBHOOK_ENV,
)
from backend.workflow.tool_capabilities import resolve_workflow_tool_capability

OPENCLI_MULTI_SOURCE_TEMPLATE = "opencli-multi-source"
OPENCLI_SOURCE_POOL_CATALOG_ID = "intelligence.source.pool"
OPENCLI_SOURCE_SLOT_CATALOG_ID = "intelligence.source.opencli-slot"
OPENCLI_COLLECTION_OUTPUT_CATALOG_ID = "intelligence.output.collection-result"
OPENCLI_HDA_CATALOG_ID = "package.opencli.multi-source-hda"
SITUATION_AWARENESS_TEMPLATE = "situation-awareness"
SITUATION_AWARENESS_CATALOG_ID = "package.intelligence.situation-awareness"
SWARM_FORECAST_TEMPLATE = "swarm-forecast"
SWARM_FORECAST_CATALOG_ID = "package.simulation.swarm-forecast"
NATIVE_INTELLIGENCE_LIFECYCLE_TEMPLATE = "native-intelligence-lifecycle"
NATIVE_INTELLIGENCE_LIFECYCLE_CATALOG_ID = "package.intelligence.native-lifecycle"
NATIVE_INTELLIGENCE_FIXTURE_ID = "native-intelligence-offline-v1"
NATIVE_INTELLIGENCE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "native_intelligence_offline.json"
)
GAOJIXING_DOUBAO_BATCH_TEMPLATE = "gaojixing-doubao-batch"
GAOJIXING_DOUBAO_BATCH_CATALOG_ID = "package.gaojixing.doubao-batch"
GAOJIXING_BATCH_CERTIFICATION_TEMPLATE = "gaojixing-batch-certification"
GAOJIXING_BATCH_CERTIFICATION_CATALOG_ID = "package.gaojixing.batch-certification"



def _with_registry_pin(tool_capability: dict) -> dict:
    """Stamp the registry's version pin onto a materialized tool reference.

    Materialized package nodes are assembled from templates, not authored in
    Studio, so they have no operator-provided pin; without the registry pin the
    plan-level pin gate rejects every materialized package."""
    tool = resolve_workflow_tool_capability(str(tool_capability.get("id") or ""))
    if tool is not None and tool.versionPin is not None:
        tool_capability["versionPin"] = tool.versionPin.model_dump()
    return tool_capability

def materialize_hda_templates(project: WorkflowProject) -> WorkflowProject:
    """Expand known package templates before validation/compile.

    The frontend and AI clients should treat HDA internals as derived from
    public package parameters. This keeps AI calls small and prevents callers
    from inventing raw internal primitive graphs.
    """

    nodes = [_materialize_node(node) for node in project.nodes]
    adapters = _merge_adapters(project.adapters, nodes)
    return project.model_copy(update={"nodes": nodes, "adapters": adapters})


def _materialize_node(node: WorkflowProjectNode) -> WorkflowProjectNode:
    materialized = node
    if _is_opencli_multi_source_hda(node):
        sources = _source_slots(node.params.get("sources"))
        if sources:
            expose_raw_source_items = node.params.get("exposeRawSourceItems") is True
            failure_mode = _read_string(
                _read_dict(node.params.get("execution")).get("failureMode")
            )
            internals = _opencli_multi_source_internals(
                sources,
                expose_raw_source_items=expose_raw_source_items,
            )
            topic = _topic_collapse(node, len(internals.nodes))
            params = {
                **node.params,
                "template": OPENCLI_MULTI_SOURCE_TEMPLATE,
                "runtime": node.params.get("runtime", "iii"),
                "lockedInternals": node.params.get("lockedInternals", True),
                "execution": {
                    "fanout": "parallel",
                    **({"failureMode": failure_mode} if failure_mode else {}),
                },
            }
            ui = {**(node.ui or {}), "catalogId": OPENCLI_HDA_CATALOG_ID}
            materialized = node.model_copy(
                update={
                    "params": params,
                    "topicCollapse": topic,
                    "internals": internals,
                    "ui": ui,
                }
            )
    elif _is_tool_package(
        node,
        template=GAOJIXING_DOUBAO_BATCH_TEMPLATE,
        catalog_id=GAOJIXING_DOUBAO_BATCH_CATALOG_ID,
    ):
        materialized = _materialize_tool_package(
            node,
            template=GAOJIXING_DOUBAO_BATCH_TEMPLATE,
            catalog_id=GAOJIXING_DOUBAO_BATCH_CATALOG_ID,
            tool_id=GAOJIXING_DOUBAO_BATCH_TOOL_ID,
            executor_mode=GAOJIXING_DOUBAO_BATCH_EXECUTOR,
            label="豆包证据批次采集",
            defaults={
                "sourceMode": "offline_fixture",
                "fixtureId": "gaojixing-doubao-offline-v1",
                "requirePhase1BeforePhase2": True,
                "feishuWebhookEnv": GAOJIXING_FEISHU_WEBHOOK_ENV,
            },
            include_output=False,
            clear_parameter_interface=True,
            public_params_override_tool_params=True,
            discarded_params={"phase1Expected", "phase2Expected"},
        )
    elif _is_tool_package(
        node,
        template=GAOJIXING_BATCH_CERTIFICATION_TEMPLATE,
        catalog_id=GAOJIXING_BATCH_CERTIFICATION_CATALOG_ID,
    ):
        materialized = _materialize_tool_package(
            node,
            template=GAOJIXING_BATCH_CERTIFICATION_TEMPLATE,
            catalog_id=GAOJIXING_BATCH_CERTIFICATION_CATALOG_ID,
            tool_id=GAOJIXING_BATCH_CERTIFY_TOOL_ID,
            executor_mode=GAOJIXING_BATCH_CERTIFY_EXECUTOR,
            label="批次终审与交付",
            defaults={
                "sourceMode": "offline_fixture",
                "fixtureId": "gaojixing-doubao-offline-v1",
            },
            include_output=False,
            clear_parameter_interface=True,
            public_params_override_tool_params=True,
            discarded_params={"phase1Expected", "phase2Expected"},
        )
    elif _is_tool_package(
        node,
        template=SITUATION_AWARENESS_TEMPLATE,
        catalog_id=SITUATION_AWARENESS_CATALOG_ID,
    ):
        materialized = _materialize_tool_package(
            node,
            template=SITUATION_AWARENESS_TEMPLATE,
            catalog_id=SITUATION_AWARENESS_CATALOG_ID,
            tool_id="tool.intelligence.situation-awareness",
            executor_mode="situation_awareness",
            label="近 30 天事态感知",
            defaults={
                "provider": "opencli-native",
                "windowDays": 30,
                "baselineDays": 30,
                "includeUnknownDates": False,
                "topK": 10,
            },
        )
    elif _is_tool_package(
        node,
        template=NATIVE_INTELLIGENCE_LIFECYCLE_TEMPLATE,
        catalog_id=NATIVE_INTELLIGENCE_LIFECYCLE_CATALOG_ID,
    ):
        internals = _native_intelligence_lifecycle_internals(node.params)
        materialized = node.model_copy(
            update={
                "params": {
                    **node.params,
                    "template": NATIVE_INTELLIGENCE_LIFECYCLE_TEMPLATE,
                    "runtime": "iii",
                    "lockedInternals": True,
                    "offline": True,
                    "credentialFree": True,
                },
                "topicCollapse": node.topicCollapse
                or WorkflowTopicCollapse(
                    groupId="native-intelligence-lifecycle-package",
                    nodeCount=len(internals.nodes),
                    mode="locked",
                    packageInternal=True,
                ),
                "internals": internals,
                "ui": {
                    **(node.ui or {}),
                    "catalogId": NATIVE_INTELLIGENCE_LIFECYCLE_CATALOG_ID,
                },
            }
        )
    elif _is_tool_package(
        node,
        template=SWARM_FORECAST_TEMPLATE,
        catalog_id=SWARM_FORECAST_CATALOG_ID,
    ):
        materialized = _materialize_tool_package(
            node,
            template=SWARM_FORECAST_TEMPLATE,
            catalog_id=SWARM_FORECAST_CATALOG_ID,
            tool_id="tool.simulation.swarm-forecast",
            executor_mode="swarm_simulation",
            label="群体智能推演",
            defaults={
                "provider": "local",
                "agentCount": 12,
                "maxRounds": 8,
                "platforms": ["twitter", "reddit"],
                "enableGraphMemoryUpdate": False,
            },
        )

    if not materialized.internals:
        return materialized

    child_nodes = [_materialize_node(child) for child in materialized.internals.nodes]
    internals = materialized.internals.model_copy(update={"nodes": child_nodes})
    return materialized.model_copy(update={"internals": internals})


def _is_opencli_multi_source_hda(node: WorkflowProjectNode) -> bool:
    template = _read_string(node.params.get("template"))
    catalog_id = _read_string((node.ui or {}).get("catalogId"))
    return template == OPENCLI_MULTI_SOURCE_TEMPLATE or catalog_id == OPENCLI_HDA_CATALOG_ID


def _is_tool_package(
    node: WorkflowProjectNode,
    *,
    template: str,
    catalog_id: str,
) -> bool:
    return (
        _read_string(node.params.get("template")) == template
        or _read_string((node.ui or {}).get("catalogId")) == catalog_id
    )


def _materialize_tool_package(
    node: WorkflowProjectNode,
    *,
    template: str,
    catalog_id: str,
    tool_id: str,
    executor_mode: str,
    label: str,
    defaults: dict[str, Any],
    include_output: bool = True,
    clear_parameter_interface: bool = False,
    public_params_override_tool_params: bool = False,
    discarded_params: set[str] | None = None,
) -> WorkflowProjectNode:
    explicit_tool_params = _read_dict(node.params.get("toolParams"))
    public_params = {
        key: value
        for key, value in node.params.items()
        if key not in {"template", "runtime", "lockedInternals", "toolParams", "execution"}
    }
    if public_params_override_tool_params:
        tool_params = {**defaults, **explicit_tool_params, **public_params}
    else:
        tool_params = {**defaults, **public_params, **explicit_tool_params}
    for key in discarded_params or set():
        tool_params.pop(key, None)
    internals = _tool_package_internals(
        tool_id=tool_id,
        executor_mode=executor_mode,
        label=label,
        tool_params=tool_params,
        include_output=include_output,
    )
    return node.model_copy(
        update={
            "params": {
                **{
                    key: value
                    for key, value in node.params.items()
                    if key not in (discarded_params or set())
                },
                "template": template,
                "runtime": node.params.get("runtime", "iii"),
                "lockedInternals": node.params.get("lockedInternals", True),
                "toolParams": tool_params,
            },
            "topicCollapse": node.topicCollapse
            or WorkflowTopicCollapse(
                groupId=f"{template}-package",
                nodeCount=len(internals.nodes),
                mode="locked",
                packageInternal=True,
            ),
            "internals": internals,
            "ui": {**(node.ui or {}), "catalogId": catalog_id},
            **({"parameterInterface": None} if clear_parameter_interface else {}),
        }
    )


def _tool_package_internals(
    *,
    tool_id: str,
    executor_mode: str,
    label: str,
    tool_params: dict[str, Any],
    include_output: bool = True,
) -> WorkflowPackageInternals:
    tool_node = WorkflowProjectNode(
        id="tool",
        kind="action",
        capability="store",
        params={
            "toolCapability": _with_registry_pin({
                "id": tool_id,
                "executor": {
                    "mode": executor_mode,
                    "params": {},
                },
            }),
            "toolParams": tool_params,
        },
        ui={
            "catalogId": "external.tool.capability",
            "label": label,
            "position": {"x": 0, "y": 0},
        },
    )
    if not include_output:
        return WorkflowPackageInternals(locked=True, nodes=[tool_node], edges=[])

    output_node = WorkflowProjectNode(
        id="output",
        kind="inbox",
        capability="store",
        params={"queue": f"{executor_mode}-output", "archive": False},
        ui={
            "catalogId": "intelligence.output.inbox",
            "label": f"{label} Output",
            "position": {"x": 340, "y": 0},
        },
    )
    return WorkflowPackageInternals(
        locked=True,
        nodes=[tool_node, output_node],
        edges=[
            WorkflowProjectEdge(
                id="tool-output",
                source=tool_node.id,
                target=output_node.id,
                sourcePort="out",
                targetPort="in",
            )
        ],
    )


def _native_intelligence_lifecycle_internals(
    package_params: dict[str, Any] | None = None,
) -> WorkflowPackageInternals:
    package_params = package_params or {}
    seed = package_params.get("seed")
    seed = seed if isinstance(seed, int) and not isinstance(seed, bool) else 0
    persona_count = package_params.get("personaCount")
    persona_count = (
        persona_count
        if isinstance(persona_count, int) and not isinstance(persona_count, bool)
        else 5
    )
    max_rounds = package_params.get("maxRounds")
    max_rounds = (
        max_rounds
        if isinstance(max_rounds, int) and not isinstance(max_rounds, bool)
        else 3
    )
    agent_count = package_params.get("agentCount")
    agent_count = (
        agent_count
        if isinstance(agent_count, int) and not isinstance(agent_count, bool)
        else None
    )
    requirement = _read_string(package_params.get("requirement")) or (
        "Explore evidence-grounded reactions."
    )
    platforms = package_params.get("platforms")
    platforms = (
        [item for item in platforms if isinstance(item, str) and item]
        if isinstance(platforms, list)
        else ["twitter", "reddit"]
    )
    question = _read_string(package_params.get("question")) or (
        "What is the most likely evidence-grounded outcome?"
    )
    actions = [
        ("research", "Research", {"seed": seed}),
        ("ontology", "Ontology", {"seed": seed}),
        ("graph", "Evidence Graph", {"seed": seed}),
        ("personas", "Personas", {"seed": seed, "personaCount": persona_count}),
        (
            "simulation.start",
            "Simulation Start",
            {
                "seed": seed,
                "maxRounds": max_rounds,
                **({"agentCount": agent_count} if agent_count is not None else {}),
                "platforms": platforms,
                "requirement": requirement,
            },
        ),
        ("simulation.run", "Simulation Run", {}),
        ("simulation.timeline", "Simulation Timeline", {"limit": 100}),
        ("simulation.stats", "Simulation Stats", {}),
        ("interviews.all", "Interviews Start", {"seed": seed}),
        ("interviews.run", "Interviews Run", {}),
        ("interviews.history", "Interview History", {"limit": 20}),
        ("report.start", "Report Start", {"seed": seed}),
        ("report.progress", "Report Progress", {}),
        ("report.run", "Report Run", {}),
        ("report.read", "Report Sections", {}),
        (
            "report.ask",
            "Report Q&A",
            {"seed": seed, "question": question},
        ),
        ("report.answers", "Report Answers", {"limit": 20}),
        ("close", "Close Session", {"seed": seed}),
    ]
    action_nodes = [
        WorkflowProjectNode(
            id=action.replace(".", "-"),
            kind="action",
            capability="store",
            params={
                "toolCapability": _with_registry_pin({
                    "id": f"tool.intelligence.native.{action}",
                    "executor": {
                        "mode": "native_intelligence",
                        "params": {"action": action},
                    },
                }),
                "toolParams": params,
            },
            ui={
                "catalogId": "external.tool.capability",
                "label": label,
                "position": {"x": index * 300, "y": 0},
            },
        )
        for index, (action, label, params) in enumerate(actions)
    ]
    source_node = WorkflowProjectNode(
        id="collection-source",
        kind="source",
        capability="fetch",
        adapter="native-intelligence-offline",
        params={
            "provider": "native-offline",
            "channelType": "fixture",
            "liveMode": "fixture",
            "sourceMode": "offline_fixture",
            "fixtureId": NATIVE_INTELLIGENCE_FIXTURE_ID,
            "fixtureItems": _native_intelligence_fixture_evidence(),
            "sourceGroup": "native-intelligence-evidence",
        },
        ui={
            "catalogId": OPENCLI_SOURCE_SLOT_CATALOG_ID,
            "label": "Offline Evidence Source",
            "position": {"x": 0, "y": 0},
        },
    )
    normalize_node = WorkflowProjectNode(
        id="collection-normalize",
        kind="agent",
        capability="normalize",
        params={"preserveSourceRefs": True},
        ui={
            "catalogId": "intelligence.processing.normalize",
            "label": "Normalize Evidence",
            "position": {"x": 300, "y": 0},
        },
    )
    output_node = WorkflowProjectNode(
        id="collection-output",
        kind="inbox",
        capability="store",
        params={"queue": "native-intelligence-evidence", "archive": False},
        ui={
            # Native intelligence consumes durable stored evidence refs, while
            # the OpenCLI collection package exposes live items downstream.
            "catalogId": "intelligence.output.inbox",
            "label": "Collection Evidence",
            "position": {"x": 600, "y": 0},
        },
    )
    nodes = [source_node, normalize_node, output_node, *action_nodes]
    edges = [
        WorkflowProjectEdge(
            id="collection-source-normalize",
            source=source_node.id,
            target=normalize_node.id,
            sourcePort="out",
            targetPort="in",
        ),
        WorkflowProjectEdge(
            id="collection-normalize-output",
            source=normalize_node.id,
            target=output_node.id,
            sourcePort="out",
            targetPort="in",
        ),
        WorkflowProjectEdge(
            id="collection-output-research",
            source=output_node.id,
            target=action_nodes[0].id,
            sourcePort="out",
            targetPort="in",
        ),
    ] + [
        WorkflowProjectEdge(
            id=f"{action_nodes[index].id}-{action_nodes[index + 1].id}",
            source=action_nodes[index].id,
            target=action_nodes[index + 1].id,
            sourcePort="out",
            targetPort="in",
        )
        for index in range(len(action_nodes) - 1)
    ]
    return WorkflowPackageInternals(locked=True, nodes=nodes, edges=edges)


def _native_intelligence_fixture_evidence() -> list[dict[str, Any]]:
    try:
        fixture = json.loads(NATIVE_INTELLIGENCE_FIXTURE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("native_intelligence_offline_fixture_invalid") from exc
    evidence = fixture.get("evidence") if isinstance(fixture, dict) else None
    if (
        fixture.get("id") != NATIVE_INTELLIGENCE_FIXTURE_ID
        or not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, dict) for item in evidence)
    ):
        raise ValueError("native_intelligence_offline_fixture_invalid")
    return [
        {
            **item,
            "published_at": item.get("publishedAt"),
        }
        for item in evidence
    ]


def _source_slots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    slots: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            continue
        site = _read_string(raw.get("site"))
        command = _read_string(raw.get("command"))
        if not site or not command:
            continue
        source_group = (
            _read_string(raw.get("sourceGroup"))
            or _read_string(raw.get("source_group"))
            or site
        )
        requested_id = _read_string(raw.get("id")) or source_group or site or f"source-{index}"
        slot_id = _unique_id(_safe_id(requested_id), used_ids)
        slots.append(
            {
                **raw,
                "id": slot_id,
                "sourceGroup": source_group,
                "site": site,
                "command": command,
            }
        )
    return slots


def _opencli_multi_source_internals(
    sources: list[dict[str, Any]],
    *,
    expose_raw_source_items: bool = False,
) -> WorkflowPackageInternals:
    source_pool_node = _source_pool_node(sources)
    source_nodes = [
        _opencli_source_node(
            source,
            index,
            dispatch_policy="inline" if expose_raw_source_items else None,
        )
        for index, source in enumerate(sources)
    ]
    source_edges = [
        WorkflowProjectEdge(
            id=f"source-pool-{source.id}",
            source=source_pool_node.id,
            target=source.id,
            sourcePort="out",
            targetPort="in",
        )
        for source in source_nodes
    ]
    if expose_raw_source_items:
        return WorkflowPackageInternals(
            locked=True,
            nodes=[source_pool_node, *source_nodes],
            edges=source_edges,
        )
    normalize_node = WorkflowProjectNode(
        id="internal-normalize",
        kind="agent",
        capability="normalize",
        params={"language": "zh-CN", "preserveSourceRefs": True},
        ui={
            "catalogId": "intelligence.processing.normalize",
            "label": "Internal Normalize",
            "position": {"x": 620, "y": 64 + max(len(sources) - 1, 0) * 72},
        },
    )
    output_node = WorkflowProjectNode(
        id="collection-output",
        kind="inbox",
        capability="store",
        params={"queue": "opencli-hda-output", "archive": False},
        ui={
            "catalogId": OPENCLI_COLLECTION_OUTPUT_CATALOG_ID,
            "label": "Collection Output",
            "position": {"x": 920, "y": 64 + max(len(sources) - 1, 0) * 72},
        },
    )
    edges = source_edges + [
        WorkflowProjectEdge(
            id=f"{source.id}-normalize",
            source=source.id,
            target=normalize_node.id,
            sourcePort="out",
            targetPort="in",
        )
        for source in source_nodes
    ] + [
        WorkflowProjectEdge(
            id="internal-normalize-output",
            source=normalize_node.id,
            target=output_node.id,
            sourcePort="out",
            targetPort="in",
        )
    ]
    return WorkflowPackageInternals(
        locked=True,
        nodes=[source_pool_node, *source_nodes, normalize_node, output_node],
        edges=edges,
    )


def _source_pool_node(sources: list[dict[str, Any]]) -> WorkflowProjectNode:
    return WorkflowProjectNode(
        id="source-pool",
        kind="agent",
        capability="normalize",
        params={
            "sourceCount": len(sources),
            "sourceGroups": [
                _read_string(source.get("sourceGroup"))
                or _read_string(source.get("site"))
                or "source"
                for source in sources
            ],
            "fanout": "parallel",
        },
        ui={
            "catalogId": OPENCLI_SOURCE_POOL_CATALOG_ID,
            "label": "Source Pool",
            "position": {"x": 0, "y": 64 + max(len(sources) - 1, 0) * 72},
        },
    )


def _opencli_source_node(
    source: dict[str, Any],
    index: int,
    *,
    dispatch_policy: str | None = None,
) -> WorkflowProjectNode:
    source_id = f"source-{source['id']}"
    args = _read_dict(source.get("args"))
    adapter_id = _adapter_id(source)
    return WorkflowProjectNode(
        id=source_id,
        kind="source",
        capability="fetch",
        adapter=adapter_id,
        params={
            "site": source["site"],
            "command": source["command"],
            "args": args,
            "sourceGroup": source["sourceGroup"],
            "format": _read_string(source.get("format")) or "json",
            **({"dispatchPolicy": dispatch_policy} if dispatch_policy else {}),
            **_optional_source_runtime_params(source),
        },
        ui={
            "catalogId": OPENCLI_SOURCE_SLOT_CATALOG_ID,
            "label": _read_string(source.get("label")) or f"OpenCLI {source['site']}",
            "position": {"x": 280, "y": index * 150},
            "sourceSlot": {
                "sourceGroup": source["sourceGroup"],
                "parallel": True,
            },
        },
    )


def _optional_source_runtime_params(source: dict[str, Any]) -> dict[str, Any]:
    optional: dict[str, Any] = {}
    for key in (
        "mode",
        "positionalArgs",
        "positional_args",
        "sourceBindingId",
        "source_binding_id",
        "sourceBindingRevisionId",
        "source_binding_revision_id",
        "sourceBindingRevisionNumber",
        "source_binding_revision_number",
        "accountId",
        "account_id",
        "workspaceId",
        "workspace_id",
        "executionId",
        "execution_id",
        "callerId",
        "caller_id",
    ):
            optional[key] = source[key]
    return optional


def _topic_collapse(node: WorkflowProjectNode, node_count: int) -> WorkflowTopicCollapse:
    topic = node.topicCollapse
    if topic:
        return topic.model_copy(update={"nodeCount": node_count, "mode": "locked"})
    return WorkflowTopicCollapse(
        groupId="opencli-package",
        nodeCount=node_count,
        mode="locked",
        packageInternal=True,
    )


def _merge_adapters(
    existing: list[WorkflowAdapterBinding],
    nodes: list[WorkflowProjectNode],
) -> list[WorkflowAdapterBinding]:
    adapter_by_id = {adapter.id: adapter for adapter in existing}
    for node in _walk_nodes(nodes):
        if (
            node.kind == "source"
            and node.adapter
            and node.params.get("sourceMode") == "offline_fixture"
        ):
            adapter_by_id.setdefault(
                node.adapter,
                WorkflowAdapterBinding(
                    id=node.adapter,
                    type="source",
                    provider="native-offline",
                    mode="fixture",
                    config={"channel": "fixture"},
                ),
            )
        if not _is_opencli_multi_source_hda(node) or not node.internals:
            continue
        for internal_node in node.internals.nodes:
            if internal_node.kind != "source" or not internal_node.adapter:
                continue
            adapter_by_id.setdefault(
                internal_node.adapter,
                WorkflowAdapterBinding(
                    id=internal_node.adapter,
                    type="source",
                    provider="opencli",
                    mode="live",
                    config={"channel": "opencli"},
                ),
            )
    return list(adapter_by_id.values())


def _walk_nodes(nodes: list[WorkflowProjectNode]) -> Iterator[WorkflowProjectNode]:
    for node in nodes:
        yield node
        if node.internals:
            yield from _walk_nodes(node.internals.nodes)


def _adapter_id(source: dict[str, Any]) -> str:
    return _read_string(source.get("adapterId")) or f"opencli-{_safe_id(str(source['site']))}"


def _unique_id(value: str, used: set[str]) -> str:
    base = value or "source"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return normalized or "source"


def _read_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
