from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.source_binding import (
    Source,
    SourceBinding,
    SourceBindingRevision,
    SourceRevision,
)
from backend.models.browser import BrowserAccount
from backend.models.workflow import Project
from backend.schemas.common import ApiResponse
from backend.schemas.source_binding import (
    SourceBindingCreate,
    SourceBindingRead,
    SourceBindingRevisionCreate,
    SourceBindingRevisionRead,
    SourceBindingUpdate,
)
from backend.security.identity import RequestIdentity, get_request_identity
from backend.security.workspace_rbac import (
    WorkspacePermission,
    get_workspace_access,
    require_permission,
)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/projects/{project_id}/source-bindings",
    tags=["source-bindings"],
)


async def _get_project(db: AsyncSession, workspace_id: str, project_id: str) -> Project:
    project = await db.scalar(
        select(Project).where(Project.workspace_id == workspace_id, Project.id == project_id)
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


async def _account_for_binding(
    db: AsyncSession, workspace_id: str, account_id: str | None
) -> BrowserAccount | None:
    """Validate account selection against the project workspace."""
    if account_id is None:
        return None
    account = await db.scalar(
        select(BrowserAccount).where(
            BrowserAccount.workspace_id == workspace_id,
            BrowserAccount.id == account_id,
        )
    )
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Browser account not found")
    return account


async def _get_source_in_workspace(db: AsyncSession, workspace_id: str, source_id: str) -> Source:
    # Scoping the lookup to workspace_id (taken from THIS project's own path,
    # never from the caller-supplied source_id) is the cross-workspace guard
    # from ADR-0041: a Project can only bind Sources owned by its own Workspace.
    source = await db.scalar(
        select(Source).where(Source.workspace_id == workspace_id, Source.id == source_id)
    )
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


async def _get_source_revision(
    db: AsyncSession,
    source_id: str,
    revision_number: int,
) -> SourceRevision:
    revision = await db.scalar(
        select(SourceRevision).where(
            SourceRevision.source_id == source_id,
            SourceRevision.revision_number == revision_number,
        )
    )
    if revision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source revision not found")
    return revision


async def _get_binding(
    db: AsyncSession, project_id: str, binding_id: str, for_update: bool = False
) -> SourceBinding:
    query = select(SourceBinding).where(
        SourceBinding.project_id == project_id, SourceBinding.id == binding_id
    )
    if for_update:
        query = query.with_for_update()
    binding = await db.scalar(query)
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source binding not found")
    return binding


@router.get("", response_model=ApiResponse[list[SourceBindingRead]])
async def list_source_bindings(
    workspace_id: str,
    project_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    await _get_project(db, workspace_id, project_id)
    rows = (await db.execute(
        select(SourceBinding)
        .where(SourceBinding.project_id == project_id)
        .order_by(SourceBinding.created_at)
    )).scalars().all()
    return ApiResponse.ok([SourceBindingRead.model_validate(row) for row in rows])


@router.post("", response_model=ApiResponse[SourceBindingRead], status_code=201)
async def create_source_binding(
    workspace_id: str,
    project_id: str,
    body: SourceBindingCreate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    await _get_project(db, workspace_id, project_id)
    source = await _get_source_in_workspace(db, workspace_id, body.source_id)
    pinned_revision = await _get_source_revision(db, source.id, body.source_revision_number)
    account_id = body.scope_config.get("account_id") or body.scope_config.get("accountId")
    if account_id is not None and not isinstance(account_id, str):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "account_id must be a string")
    await _account_for_binding(db, workspace_id, account_id)

    binding = SourceBinding(
        project_id=project_id,
        source_id=source.id,
        name=body.name,
        slug=body.slug,
        current_revision_number=1,
        created_by_user_id=access.user_id,
    )
    db.add(binding)
    await db.flush()
    db.add(
        SourceBindingRevision(
            source_binding_id=binding.id,
            revision_number=1,
            pinned_source_revision_id=pinned_revision.id,
            scope_config=body.scope_config,
            workspace_id=workspace_id if account_id is not None else None,
            account_id=account_id,
            created_by_user_id=access.user_id,
        )
    )
    await db.flush()
    return ApiResponse.ok(SourceBindingRead.model_validate(binding))


@router.get("/{binding_id}", response_model=ApiResponse[SourceBindingRead])
async def get_source_binding(
    workspace_id: str,
    project_id: str,
    binding_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    await _get_project(db, workspace_id, project_id)
    binding = await _get_binding(db, project_id, binding_id)
    return ApiResponse.ok(SourceBindingRead.model_validate(binding))


@router.patch("/{binding_id}", response_model=ApiResponse[SourceBindingRead])
async def update_source_binding(
    workspace_id: str,
    project_id: str,
    binding_id: str,
    body: SourceBindingUpdate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    await _get_project(db, workspace_id, project_id)
    binding = await _get_binding(db, project_id, binding_id, for_update=True)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(binding, field, value)
    await db.flush()
    return ApiResponse.ok(SourceBindingRead.model_validate(binding))


@router.get(
    "/{binding_id}/revisions", response_model=ApiResponse[list[SourceBindingRevisionRead]]
)
async def list_source_binding_revisions(
    workspace_id: str,
    project_id: str,
    binding_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    await _get_project(db, workspace_id, project_id)
    await _get_binding(db, project_id, binding_id)
    rows = (await db.execute(
        select(SourceBindingRevision)
        .where(SourceBindingRevision.source_binding_id == binding_id)
        .order_by(SourceBindingRevision.revision_number)
    )).scalars().all()
    return ApiResponse.ok([SourceBindingRevisionRead.model_validate(row) for row in rows])


@router.post(
    "/{binding_id}/revisions",
    response_model=ApiResponse[SourceBindingRevisionRead],
    status_code=201,
)
async def create_source_binding_revision(
    workspace_id: str,
    project_id: str,
    binding_id: str,
    body: SourceBindingRevisionCreate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    await _get_project(db, workspace_id, project_id)
    binding = await _get_binding(db, project_id, binding_id, for_update=True)
    pinned_revision = await _get_source_revision(db, binding.source_id, body.source_revision_number)
    await _account_for_binding(db, workspace_id, body.account_id)

    next_revision = binding.current_revision_number + 1
    revision = SourceBindingRevision(
        source_binding_id=binding.id,
        revision_number=next_revision,
        pinned_source_revision_id=pinned_revision.id,
        scope_config=body.scope_config,
        workspace_id=workspace_id if body.account_id is not None else None,
        account_id=body.account_id,
        created_by_user_id=access.user_id,
    )
    db.add(revision)
    binding.current_revision_number = next_revision
    await db.flush()
    return ApiResponse.ok(SourceBindingRevisionRead.model_validate(revision))
