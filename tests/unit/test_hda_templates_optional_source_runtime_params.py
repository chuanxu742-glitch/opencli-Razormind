"""Regression coverage for optional OpenCLI source runtime parameters."""

from backend.schemas.workflow import WorkflowProject
from backend.workflow.hda_templates import materialize_hda_templates


def test_materialize_hda_sources_omits_empty_optionals_and_preserves_runtime_refs() -> None:
    project = WorkflowProject(
        id="workflow-optional-source-runtime-params",
        name="Optional source runtime params",
        profile="intelligence",
        nodes=[
            {
                "id": "opencli-package",
                "kind": "agent",
                "capability": "normalize",
                "params": {
                    "template": "opencli-multi-source",
                    "sources": [
                        {
                            "id": "minimal-source",
                            "site": "sina",
                            "command": "quote",
                            "args": {},
                            "sourceGroup": "market",
                        },
                        {
                            "id": "bound-source",
                            "site": "bilibili",
                            "command": "search",
                            "args": {"keyword": "A股"},
                            "sourceGroup": "media",
                            "mode": "live",
                            "positionalArgs": ["A股"],
                            "source_binding_id": "binding-1",
                            "sourceBindingRevisionNumber": 3,
                            "sourceBindingRevisionId": " ",
                            "account_id": "account-1",
                            "workspaceId": "workspace-1",
                            "execution_id": "execution-1",
                            "callerId": "caller-1",
                        },
                    ],
                },
            }
        ],
    )

    materialized = materialize_hda_templates(project)
    assert materialized.nodes[0].internals is not None
    source_nodes = {
        node.id: node
        for node in materialized.nodes[0].internals.nodes
        if node.kind == "source"
    }

    assert source_nodes["source-minimal-source"].params == {
        "site": "sina",
        "command": "quote",
        "args": {},
        "sourceGroup": "market",
        "format": "json",
    }
    assert source_nodes["source-bound-source"].params == {
        "site": "bilibili",
        "command": "search",
        "args": {"keyword": "A股"},
        "sourceGroup": "media",
        "format": "json",
        "mode": "live",
        "positionalArgs": ["A股"],
        "source_binding_id": "binding-1",
        "sourceBindingRevisionNumber": 3,
        "account_id": "account-1",
        "workspaceId": "workspace-1",
        "execution_id": "execution-1",
        "callerId": "caller-1",
    }
