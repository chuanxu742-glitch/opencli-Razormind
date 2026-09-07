from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.models.browser import BrowserAccount
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.source_binding import (
    Source,
    SourceBinding,
    SourceBindingRevision,
    SourceLifecycleStatus,
    SourceRevision,
)
from backend.models.studio import (
    StudioProject,
    StudioWorkflow,
    StudioWorkflowValidationRun,
    StudioWorkflowVersion,
    StudioWorkspace,
)
from backend.models.workflow import Project
from backend.models.workflow_run import WorkflowRun
from backend.schemas.workflow import CompiledWorkflowNode, WorkflowRunStartRequest
from backend.security.identity import RequestIdentity
from backend.workflow import opencli_hda_tracer as tracer
from backend.workflow.runtime_registry import SOURCE_FETCH_BINDING_ID


def _workflow_project(*, account_id: str | None = None, revision_id: str | None = None) -> dict:
    params = {
        "site": "bbc",
        "command": "news",
        "format": "json",
        "args": {"caller_id": "forged-client", "execution_id": "forged-run"},
        "opencliAdapterNodeId": "opencli.adapter.bbc.news",
    }
    if account_id is not None:
        params.update(
            {
                "workspaceId": "actor-workspace",
                "accountId": account_id,
                "sourceBindingId": "actor-binding",
                "sourceBindingRevisionId": revision_id,
                "callerId": "forged-client",
                "executionId": "forged-run",
            }
        )
    return {
        "id": "actor-workflow",
        "name": "Actor workflow",
        "profile": "intelligence",
        "version": 1,
        "nodes": [
            {
                "id": "source-bbc",
                "kind": "source",
                "capability": "fetch",
                "adapter": "opencli-bbc",
                "params": params,
                "ui": {"catalogId": "intelligence.source.opencli-slot"},
            }
        ],
        "edges": [],
        "adapters": [
            {
                "id": "opencli-bbc",
                "type": "source",
                "provider": "opencli",
                "mode": "live",
                "config": {"channel": "opencli"},
            }
        ],
        "agentPermissions": {
            "canFetchNetwork": True,
            "canSendNotifications": False,
            "canWriteInbox": True,
            "allowedDomains": [],
        },
    }


async def _seed_account_binding(db_session):
    workspace = Workspace(id="actor-workspace", name="Actor Workspace", slug="actor-workspace")
    user = User(id="actor-user", subject="actor-subject", disabled=False)
    project = Project(
        id="actor-project",
        workspace_id=workspace.id,
        name="Actor Project",
        slug="actor-project",
        created_by_user_id=user.id,
    )
    source = Source(
        id="actor-source",
        workspace_id=workspace.id,
        name="Actor Source",
        slug="actor-source",
        adapter_type="opencli",
        status=SourceLifecycleStatus.ACTIVE,
        current_revision_number=1,
        created_by_user_id=user.id,
    )
    source_revision = SourceRevision(
        id="actor-source-r1",
        source_id=source.id,
        revision_number=1,
        adapter_config={},
        created_by_user_id=user.id,
    )
    binding = SourceBinding(
        id="actor-binding",
        project_id=project.id,
        source_id=source.id,
        name="Actor Binding",
        slug="actor-binding",
        status=SourceLifecycleStatus.ACTIVE,
        current_revision_number=1,
        created_by_user_id=user.id,
    )
    account = BrowserAccount(
        id="actor-account",
        workspace_id=workspace.id,
        site="bbc",
        label="Actor Account",
        profile_id="actor-profile",
        auth_evidence="valid",
        evidence_source="rule_verified",
        status="dormant",
    )
    revision = SourceBindingRevision(
        id="actor-binding-r1",
        source_binding_id=binding.id,
        revision_number=1,
        pinned_source_revision_id=source_revision.id,
        scope_config={"accountId": account.id},
        workspace_id=workspace.id,
        account_id=account.id,
        created_by_user_id=user.id,
    )
    db_session.add_all(
        [
            workspace,
            user,
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WorkspaceRole.OPERATOR,
            ),
            project,
            source,
            source_revision,
            binding,
            account,
            revision,
        ]
    )
    await db_session.commit()
    return workspace, user, source, binding, account, revision


async def _seed_studio_published_workflow(db_session, *, user_id: str) -> str:
    graph = _workflow_project(
        account_id="actor-account",
        revision_id="actor-binding-r1",
    )
    graph["id"] = "actor-studio-workflow"
    workspace = StudioWorkspace(
        id="actor-workspace",
        name="Actor Studio Workspace",
        slug="actor-studio-workspace",
    )
    project = StudioProject(
        id="actor-studio-project",
        workspace_id=workspace.id,
        name="Actor Studio Project",
        slug="actor-studio-project",
        created_by_user_id=user_id,
    )
    db_session.add_all([workspace, project])
    await db_session.flush()
    workflow = StudioWorkflow(
        id="actor-studio-workflow",
        project_id=project.id,
        name="Actor Studio Workflow",
        current_published_version=1,
    )
    db_session.add(workflow)
    await db_session.flush()
    project.primary_workflow_id = workflow.id
    validation = StudioWorkflowValidationRun(
        id="actor-studio-validation",
        workflow_id=workflow.id,
        draft_revision=1,
        status="completed",
        valid=True,
        errors=[],
        warnings=[],
        compile_version="workflow-compile.v1",
        resolved_graph=graph,
    )
    db_session.add(validation)
    await db_session.flush()
    db_session.add(
        StudioWorkflowVersion(
            id="actor-studio-version",
            workflow_id=workflow.id,
            version=1,
            draft_revision=1,
            graph=graph,
            compile_version="workflow-compile.v1",
            validation_run_id=validation.id,
            published_by_user_id=user_id,
            reason="Actor regression",
        )
    )
    await db_session.commit()
    return (
        "/api/v1/workspaces/actor-workspace/projects/actor-studio-project"
        "/workflows/actor-studio-workflow"
    )


def _catalog():
    return (
        {
            "site": "bbc",
            "name": "news",
            "description": "BBC news",
            "access": "read",
            "browser": False,
            "strategy": "public",
            "args": [],
            "columns": ["title", "url"],
        },
    )


@pytest.mark.asyncio
async def test_collector_overwrites_client_execution_identity(monkeypatch):
    observed_sources: list[dict] = []

    async def collect(source, _source_type):
        observed_sources.append(source)
        return [{"title": "ok"}]

    monkeypatch.setattr(tracer, "_collect_source_once", collect)
    node = CompiledWorkflowNode(
        id="collector",
        kind="source",
        capability="fetch",
        params={},
        runtime={
            "binding": {
                "binding_id": SOURCE_FETCH_BINDING_ID,
                "input": {
                    "collectorType": "api",
                    "sources": [
                        {
                            "sourceId": "bound-source",
                            "kind": "api",
                            "accountId": "account-1",
                            "sourceBindingRevisionId": "revision-1",
                            "caller_id": "forged-caller",
                            "execution_id": "forged-execution",
                        }
                    ],
                },
            }
        },
    )

    items, results = await tracer._execute_collector_source_node(
        node,
        actor_user_id="trusted-actor",
        execution_id="trusted-execution",
    )

    assert items[0]["data"]["title"] == "ok"
    assert results[0]["status"] == "completed"
    assert observed_sources[0]["caller_id"] == "trusted-actor"
    assert observed_sources[0]["execution_id"] == "trusted-execution"


@pytest.mark.asyncio
async def test_workflow_start_api_captures_server_identity(client, db_session, monkeypatch):
    workspace, user, _source, _binding, account, revision = await _seed_account_binding(db_session)
    seen_actors: list[str | None] = []

    async def resolve_identity(_request):
        return RequestIdentity(subject=user.subject)

    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        assert node.id == "source-bbc"
        seen_actors.append(actor_user_id)
        return [{"title": "ok"}], {"success": True, "protocol": "account"}

    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    monkeypatch.setattr(
        "backend.api.v1.workflows.get_request_identity",
        resolve_identity,
    )
    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    response = await client.post(
        "/api/v1/workflows/runs",
        json={
            "project": _workflow_project(
                account_id=account.id,
                revision_id=revision.id,
            ),
            "runId": "actor-api-run",
        },
    )

    assert response.status_code == 202, response.text
    row = await db_session.get(WorkflowRun, "actor-api-run")
    assert row is not None
    assert row.requested_by_user_id == user.id
    assert seen_actors == [user.id]
    valid_read = await client.get("/api/v1/workflows/runs/actor-api-run")
    assert valid_read.status_code == 200, valid_read.text
    valid_resume = await client.post(
        "/api/v1/workflows/runs/actor-api-run/source-outputs",
        json={"sourceOutputs": {"source-bbc": [{"title": "continued"}]}},
    )
    assert valid_resume.status_code == 202, valid_resume.text
    await db_session.refresh(row)
    assert row.requested_by_user_id == user.id
    assert seen_actors == [user.id]
    attacker = User(
        id="user-attacker",
        subject="oidc|attacker",
        email="attacker@example.com",
        display_name="Attacker",
    )
    db_session.add_all(
        [
            attacker,
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=attacker.id,
                role=WorkspaceRole.OPERATOR.value,
            ),
        ]
    )
    await db_session.flush()

    async def resolve_attacker_identity(_request):
        return RequestIdentity(subject=attacker.subject)

    monkeypatch.setattr(
        "backend.api.v1.workflows.get_request_identity",
        resolve_attacker_identity,
    )
    forged_resume = await client.post(
        "/api/v1/workflows/runs/actor-api-run/source-outputs",
        json={"sourceOutputs": {"source-bbc": [{"title": "forged"}]}},
    )
    assert forged_resume.status_code == 403
    forged_read = await client.get("/api/v1/workflows/runs/actor-api-run")
    assert forged_read.status_code == 403
    forged_restart = await client.post(
        "/api/v1/workflows/runs",
        json={
            "project": _workflow_project(
                account_id=account.id,
                revision_id=revision.id,
            ),
            "runId": "actor-api-run",
        },
    )
    assert forged_restart.status_code == 403
    assert seen_actors == [user.id]


@pytest.mark.asyncio
async def test_account_workflow_persists_authenticated_actor_and_ignores_forged_fields(
    db_session, monkeypatch
):
    _workspace, user, _source, _binding, account, revision = await _seed_account_binding(db_session)
    seen_actors: list[str | None] = []

    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        assert node.id == "source-bbc"
        seen_actors.append(actor_user_id)
        return [{"title": "ok"}], {"success": True, "protocol": "account"}

    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    request = WorkflowRunStartRequest.model_validate(
        {
            "project": _workflow_project(
                account_id=account.id,
                revision_id=revision.id,
            ),
            "runId": "actor-run",
        }
    )

    await tracer.start_workflow_run(
        request,
        session=db_session,
        request_identity=RequestIdentity(subject=user.subject),
    )

    row = await db_session.get(WorkflowRun, "actor-run")
    assert row is not None
    assert row.requested_by_user_id == user.id
    assert seen_actors == [user.id]


@pytest.mark.asyncio
async def test_account_workflow_rejects_missing_revoked_and_disabled_actors(
    db_session, monkeypatch
):
    workspace, user, _source, _binding, account, revision = await _seed_account_binding(db_session)
    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    request = WorkflowRunStartRequest.model_validate(
        {
            "project": _workflow_project(
                account_id=account.id,
                revision_id=revision.id,
            ),
            "runId": "actor-denied-run",
        }
    )

    with pytest.raises(HTTPException) as missing:
        await tracer.start_workflow_run(request, session=db_session)
    assert missing.value.status_code == 401

    membership = await db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    await db_session.delete(membership)
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await tracer.start_workflow_run(
            request,
            session=db_session,
            requested_by_user_id=user.id,
        )
    assert revoked.value.status_code == 403

    db_session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OPERATOR,
        )
    )
    user.disabled = True
    await db_session.commit()
    with pytest.raises(HTTPException) as disabled:
        await tracer.start_workflow_run(
            request,
            session=db_session,
            requested_by_user_id=user.id,
        )
    assert disabled.value.status_code == 403


@pytest.mark.asyncio
async def test_account_workflow_rejects_cross_workspace_binding_and_disabled_source(
    db_session, monkeypatch
):
    _workspace, user, source, _binding, account, revision = await _seed_account_binding(db_session)
    other_workspace = Workspace(
        id="other-workspace",
        name="Other Workspace",
        slug="other-workspace",
    )
    db_session.add(other_workspace)
    project = await db_session.get(Project, "actor-project")
    assert project is not None
    project.workspace_id = other_workspace.id
    await db_session.commit()
    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    request = WorkflowRunStartRequest.model_validate(
        {
            "project": _workflow_project(
                account_id=account.id,
                revision_id=revision.id,
            ),
            "runId": "cross-workspace-run",
        }
    )

    with pytest.raises(HTTPException) as cross_workspace:
        await tracer.start_workflow_run(
            request,
            session=db_session,
            request_identity=RequestIdentity(subject=user.subject),
        )
    assert cross_workspace.value.status_code == 403

    project.workspace_id = "actor-workspace"
    source.status = SourceLifecycleStatus.DISABLED
    await db_session.commit()
    with pytest.raises(HTTPException) as disabled_source:
        await tracer.start_workflow_run(
            request.model_copy(update={"runId": "disabled-source-run"}),
            session=db_session,
            request_identity=RequestIdentity(subject=user.subject),
        )
    assert disabled_source.value.status_code == 403


@pytest.mark.asyncio
async def test_account_workflow_authorizes_before_invalid_compile_and_rejects_graph_reuse(
    db_session, monkeypatch
):
    _workspace, user, _source, _binding, account, revision = await _seed_account_binding(db_session)
    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    invalid_project = _workflow_project(account_id=account.id, revision_id=revision.id)
    invalid_project["adapters"] = []
    invalid_request = WorkflowRunStartRequest.model_validate(
        {"project": invalid_project, "runId": "invalid-account-run"}
    )

    with pytest.raises(HTTPException) as unauthorized:
        await tracer.start_workflow_run(invalid_request, session=db_session)
    assert unauthorized.value.status_code == 401
    assert await db_session.get(WorkflowRun, "invalid-account-run") is None

    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        return [{"title": node.id}], {"success": True, "actor": actor_user_id}

    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    original_request = WorkflowRunStartRequest.model_validate(
        {
            "project": _workflow_project(account_id=account.id, revision_id=revision.id),
            "runId": "actor-graph-run",
        }
    )
    identity = RequestIdentity(subject=user.subject)
    await tracer.start_workflow_run(
        original_request,
        session=db_session,
        request_identity=identity,
    )
    replacement = original_request.model_copy(deep=True)
    replacement.project.name = "Caller replacement graph"

    with pytest.raises(HTTPException) as collision:
        await tracer.start_workflow_run(
            replacement,
            session=db_session,
            request_identity=identity,
        )
    assert collision.value.status_code == 409
    row = await db_session.get(WorkflowRun, "actor-graph-run")
    assert row is not None
    assert row.request["project"]["name"] == "Actor workflow"


@pytest.mark.asyncio
async def test_account_run_reads_revalidate_role_source_and_legacy_actor(
    client, db_session, monkeypatch
):
    _workspace, user, source, _binding, account, revision = await _seed_account_binding(db_session)

    async def resolve_identity(_request):
        return RequestIdentity(subject=user.subject)

    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        return [{"title": node.id}], {"success": True, "actor": actor_user_id}

    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    monkeypatch.setattr("backend.api.v1.workflows.get_request_identity", resolve_identity)
    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    response = await client.post(
        "/api/v1/workflows/runs",
        json={
            "project": _workflow_project(account_id=account.id, revision_id=revision.id),
            "runId": "actor-read-run",
        },
    )
    assert response.status_code == 202, response.text

    membership = await db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == "actor-workspace",
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    membership.role = WorkspaceRole.VIEWER
    await db_session.commit()
    denied_role = await client.get("/api/v1/workflows/runs/actor-read-run")
    assert denied_role.status_code == 403

    membership.role = WorkspaceRole.OPERATOR
    source.status = SourceLifecycleStatus.DISABLED
    await db_session.commit()
    denied_source = await client.get("/api/v1/workflows/runs/actor-read-run")
    assert denied_source.status_code == 403

    source.status = SourceLifecycleStatus.ACTIVE
    row = await db_session.get(WorkflowRun, "actor-read-run")
    assert row is not None
    row.requested_by_user_id = None
    await db_session.commit()
    denied_legacy = await client.get("/api/v1/workflows/runs/actor-read-run")
    assert denied_legacy.status_code == 403


@pytest.mark.asyncio
async def test_studio_account_runs_revalidate_before_idempotent_reads(
    client, db_session, monkeypatch
):
    _workspace, user, source, _binding, _account, _revision = await _seed_account_binding(
        db_session
    )
    base_url = await _seed_studio_published_workflow(db_session, user_id=user.id)

    async def resolve_identity(_request):
        return RequestIdentity(subject=user.subject)

    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        return [{"title": node.id}], {"success": True, "actor": actor_user_id}

    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    monkeypatch.setattr("backend.api.v1.workflows.get_request_identity", resolve_identity)
    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    headers = {"Idempotency-Key": "studio-actor-idempotency"}
    payload = {"inputs": {"topic": "actor"}, "user": "server-worker"}
    started = await client.post(f"{base_url}/runs", json=payload, headers=headers)
    assert started.status_code == 202, started.text
    run_id = started.json()["data"]["runId"]
    row = await db_session.get(WorkflowRun, run_id)
    assert row is not None
    assert row.requested_by_user_id == user.id

    source.status = SourceLifecycleStatus.DISABLED
    await db_session.commit()
    denied_replay = await client.post(f"{base_url}/runs", json=payload, headers=headers)
    assert denied_replay.status_code == 403

    source.status = SourceLifecycleStatus.ACTIVE
    await db_session.commit()
    visible = await client.get(f"{base_url}/runs/{run_id}")
    assert visible.status_code == 200, visible.text

    membership = await db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == "actor-workspace",
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    membership.role = WorkspaceRole.VIEWER
    await db_session.commit()
    denied_read = await client.get(f"{base_url}/runs/{run_id}")
    denied_summary = await client.get(
        "/api/v1/workspaces/actor-workspace/projects/actor-studio-project/runtime-summary"
    )
    assert denied_read.status_code == 403
    assert denied_summary.status_code == 403


@pytest.mark.asyncio
async def test_accountless_workflow_keeps_legacy_anonymous_execution(db_session, monkeypatch):
    async def fake_dispatch(_dispatch, _match, *, node, actor_user_id):
        assert node.id == "source-bbc"
        assert actor_user_id is None
        return [{"title": "ok"}], {"success": True, "protocol": "local"}

    monkeypatch.setattr("backend.workflow.opencli_adapter_nodes._load_opencli_catalog", _catalog)
    monkeypatch.setattr(tracer, "_dispatch_opencli_source_to_fleet", fake_dispatch)
    request = WorkflowRunStartRequest.model_validate(
        {"project": deepcopy(_workflow_project()), "runId": "anonymous-workflow-run"}
    )

    await tracer.start_workflow_run(request, session=db_session)

    row = await db_session.get(WorkflowRun, "anonymous-workflow-run")
    assert row is not None
    assert row.requested_by_user_id is None
