"""Workspace-scoped browser-account and login-session API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import UTC, datetime
from typing import Literal

import anyio
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal, commit_session, get_db
from backend.models.browser import BrowserAccount, BrowserAccountStatus, BrowserLoginSession
from backend.models.browser_portal import BrowserPortalOwner
from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountCreate,
    BrowserAccountListV1,
    BrowserAccountOperationRequestV1,
    BrowserAccountOperationResponseV1,
    BrowserAccountRead,
    BrowserAccountRevisionPreconditionV1,
    BrowserAccountUpdate,
    BrowserLoginSessionCreateV1,
    BrowserLoginSessionRead,
    ExternalIdentityV1,
    PortalAuthorizationFactsV1,
    PortalEntryResponseV1,
    PortalTicketIssuedV1,
    PortalTicketIssueRequestV1,
    PortalTicketRedeemRequestV1,
)
from backend.schemas.common import ApiResponse
from backend.security.identity import RequestIdentity, get_request_identity
from backend.security.workspace_rbac import (
    WorkspaceAccess,
    WorkspacePermission,
    get_workspace_access,
    require_permission,
)
from backend.services import browser_account_service
from backend.services.browser_account_login_options import (
    get_login_readiness,
    workspace_login_options,
)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/browser-accounts",
    tags=["browser-accounts"],
)
logger = logging.getLogger(__name__)


class LoginSessionRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_view_generation: int = Field(ge=0)


class LoginSessionConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    expected_view_generation: int = Field(ge=0)
    platform_identity: ExternalIdentityV1 | None = None


class LoginSessionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=0)
    reason: Literal["completed", "cancelled", "expired", "error"] = "cancelled"


class BrowserDesktopGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["granted"] = "granted"
    websocket_path: str
    expires_at: datetime
    hard_expires_at: datetime
    session_id: str
    session_revision: int
    max_session_seconds: Literal[1800] = 1800


class BrowserNativeWindowSupportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    message: str


class BrowserNativeWindowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["opened", "already_open"]
    session_id: str
    message: str


def _operation_response(
    account: BrowserAccount,
    operation: Literal["auth_required", "suspend", "resume", "migrate"],
) -> BrowserAccountOperationResponseV1:
    return BrowserAccountOperationResponseV1(
        account_ref=_account_ref(account.workspace_id, account.id),
        operation=operation,
        revision=account.revision,
        status=BrowserAccountStatus(account.status),
        auth_required=account.auth_required,
        paused=account.paused,
    )


def _validate_operation_ref(
    body: BrowserAccountOperationRequestV1,
    workspace_id: str,
    account_id: str,
    operation: Literal["auth_required", "suspend", "resume", "migrate"],
) -> None:
    if body.operation != operation or (
        body.account_ref.workspace_id != workspace_id or body.account_ref.account_id != account_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Browser account operation target not found")


def _http_error(exc: browser_account_service.BrowserAccountError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _account_read(account: object) -> BrowserAccountRead:
    return BrowserAccountRead.model_validate(account)


def _session_read(session: object) -> BrowserLoginSessionRead:
    parsed = BrowserLoginSessionRead.model_validate(session)
    # Persisted Chrome IDs are strings; expose their canonical numeric wire form.
    return parsed.model_copy(
        update={
            field: int(value)
            for field in ("tab_id", "frame_id")
            if isinstance(value := getattr(parsed, field), str) and value.isdecimal()
        }
    )


def _require_operator_or_manager(access: WorkspaceAccess) -> None:
    role = getattr(access.role, "value", access.role)
    if role not in {"admin", "maintainer", "operator"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Browser account operation permission required",
        )


def _effective_revision(
    if_match: str | None,
    body_revision: int | None,
) -> int | None:
    try:
        return BrowserAccountRevisionPreconditionV1(
            if_match=if_match,
            body_revision=body_revision,
        ).effective_revision
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "revision precondition is invalid") from exc


def _account_ref(workspace_id: str, account_id: str) -> AccountRef:
    return AccountRef(workspace_id=workspace_id, account_id=account_id)


@router.get("", response_model=ApiResponse[BrowserAccountListV1])
async def list_browser_accounts(
    workspace_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    after_id: str | None = Query(default=None, min_length=1, max_length=36),
    cursor: str | None = Query(default=None, min_length=1, max_length=36),
    status_filter: BrowserAccountStatus | None = Query(default=None, alias="status"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        accounts, next_cursor = await browser_account_service.list_browser_accounts(
            db,
            workspace_id,
            limit=limit,
            after_id=after_id or cursor,
            status=status_filter,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    if next_cursor:
        response.headers["X-Next-Cursor"] = next_cursor
    return ApiResponse.ok(
        BrowserAccountListV1(
            items=[_account_read(account) for account in accounts],
            next_cursor=next_cursor,
        )
    )


@router.post(
    "", response_model=ApiResponse[BrowserAccountRead], status_code=status.HTTP_201_CREATED
)
async def create_browser_account(
    workspace_id: str,
    body: BrowserAccountCreate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    try:
        account = await browser_account_service.create_browser_account(db, workspace_id, body)
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_account_read(account))


@router.get("/login-options", response_model=ApiResponse)
async def get_browser_account_login_options(
    workspace_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    return ApiResponse.ok(await workspace_login_options(db))


@router.get(
    "/native-window-support",
    response_model=ApiResponse[BrowserNativeWindowSupportResponse],
)
async def get_browser_native_window_support(
    workspace_id: str,
    request: Request,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """Report whether this API host can launch the configured native viewer."""

    from backend.services.browser_native_window import native_window_manager

    # Same-origin browser GET requests need not carry Origin. This endpoint is
    # read-only and still requires authenticated workspace operator permission.
    if request.headers.get("origin"):
        _require_same_origin(request)
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    support = native_window_manager.support()
    return ApiResponse.ok(BrowserNativeWindowSupportResponse(**support.__dict__))


@router.get("/{account_id}/login-readiness", response_model=ApiResponse)
async def get_browser_account_login_readiness(
    workspace_id: str,
    account_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        readiness = await get_login_readiness(db, workspace_id, account_id)
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(readiness)


@router.get("/{account_id}", response_model=ApiResponse[BrowserAccountRead])
async def get_browser_account(
    workspace_id: str,
    account_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        account = await browser_account_service.get_browser_account(db, workspace_id, account_id)
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_account_read(account))


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_browser_account(
    workspace_id: str,
    account_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    revision = _effective_revision(if_match, None)
    if revision is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "revision precondition is required")
    try:
        await browser_account_service.delete_browser_account(db, workspace_id, account_id, revision)
        await commit_session(db)
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{account_id}", response_model=ApiResponse[BrowserAccountRead])
async def update_browser_account(
    workspace_id: str,
    account_id: str,
    body: BrowserAccountUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    revision = _effective_revision(if_match, body.expected_revision)
    assert revision is not None
    try:
        account = await browser_account_service.update_browser_account(
            db,
            workspace_id,
            account_id,
            body.model_copy(update={"expected_revision": revision}),
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_account_read(account))


@router.post(
    "/{account_id}/auth-required",
    response_model=ApiResponse[BrowserAccountOperationResponseV1],
)
async def set_browser_account_auth_required(
    workspace_id: str,
    account_id: str,
    body: BrowserAccountOperationRequestV1,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    _validate_operation_ref(body, workspace_id, account_id, "auth_required")
    revision = _effective_revision(if_match, body.expected_revision)
    assert revision is not None
    if body.auth_required is not True:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "auth_required may only be set")
    try:
        account = await browser_account_service.mark_auth_required(
            db, workspace_id, account_id, revision
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_operation_response(account, "auth_required"))


@router.post(
    "/{account_id}/suspend",
    response_model=ApiResponse[BrowserAccountOperationResponseV1],
)
async def suspend_browser_account(
    workspace_id: str,
    account_id: str,
    body: BrowserAccountOperationRequestV1,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    _validate_operation_ref(body, workspace_id, account_id, "suspend")
    revision = _effective_revision(if_match, body.expected_revision)
    assert revision is not None
    try:
        account = await browser_account_service.suspend_browser_account(
            db, workspace_id, account_id, revision
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_operation_response(account, "suspend"))


@router.post(
    "/{account_id}/resume",
    response_model=ApiResponse[BrowserAccountOperationResponseV1],
)
async def resume_browser_account(
    workspace_id: str,
    account_id: str,
    body: BrowserAccountOperationRequestV1,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    _validate_operation_ref(body, workspace_id, account_id, "resume")
    revision = _effective_revision(if_match, body.expected_revision)
    assert revision is not None
    try:
        account = await browser_account_service.resume_browser_account(
            db, workspace_id, account_id, revision
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_operation_response(account, "resume"))


@router.post(
    "/{account_id}/migration",
    response_model=ApiResponse[BrowserAccountOperationResponseV1],
)
async def migrate_browser_account(
    workspace_id: str,
    account_id: str,
    body: BrowserAccountOperationRequestV1,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """Queue explicit migration; node failure never silently switches ownership."""

    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    _validate_operation_ref(body, workspace_id, account_id, "migrate")
    revision = _effective_revision(if_match, body.expected_revision)
    assert revision is not None
    assert body.target_node_id is not None
    assert body.isolation_evidence_ref is not None
    try:
        account = await browser_account_service.queue_account_migration(
            db,
            workspace_id,
            account_id,
            target_node_id=body.target_node_id,
            snapshot_ref=body.isolation_evidence_ref,
            expected_revision=revision,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_operation_response(account, "migrate"))


@router.get(
    "/{account_id}/login-sessions",
    response_model=ApiResponse[list[BrowserLoginSessionRead]],
)
async def list_account_login_sessions(
    workspace_id: str,
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        sessions = await browser_account_service.list_login_sessions(
            db, workspace_id, account_id, limit=limit
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok([_session_read(session) for session in sessions])


@router.post(
    "/{account_id}/login-sessions",
    response_model=ApiResponse[BrowserLoginSessionRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_account_login_session(
    workspace_id: str,
    account_id: str,
    body: BrowserLoginSessionCreateV1,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    try:
        if body.purpose in {"login", "browser"}:
            account = await browser_account_service.get_browser_account(
                db, workspace_id, account_id, for_update=True
            )
            active = await db.scalar(
                select(BrowserLoginSession).where(
                    BrowserLoginSession.workspace_id == workspace_id,
                    BrowserLoginSession.account_id == account_id,
                    BrowserLoginSession.status.in_(
                        browser_account_service.ACTIVE_BROWSER_SESSION_STATUSES
                    ),
                )
            )
            # Reusing/promoting a live stack must not depend on a fresh capacity
            # or readiness cache. New stacks still pass the complete matching
            # and runtime-readiness gate for both login and browser purposes.
            if active is None:
                if (
                    not account.node_id
                    and not account.runtime_bundle_id
                    and not (
                        account.profile_id
                        or account.profile_version
                        or account.profile_manifest_id
                    )
                ):
                    from backend.services.browser_account_login_options import (
                        match_new_account_login,
                    )

                    if account.revision != body.expected_revision:
                        raise browser_account_service.BrowserAccountError(
                            "stale_generation", "account revision is stale", 409
                        )
                    matched = await match_new_account_login(
                        db, account.site, require_free_slot=False
                    )
                    if matched and matched.get("node_id"):
                        for key, value in matched.items():
                            setattr(account, key, value)
                        from backend.models.browser import BrowserRuntimeBundle

                        bundle = await db.get(
                            BrowserRuntimeBundle, account.runtime_bundle_id
                        )
                        account.runtime_bundle_version = bundle.version
                        await db.flush()
                readiness = await get_login_readiness(db, workspace_id, account_id)
                if not readiness.ready and readiness.code not in {
                    "capacity_full",
                    "session_queued",
                    "pool_pending",
                }:
                    raise browser_account_service.BrowserAccountError(
                        readiness.code, readiness.message, 409
                    )
        session = await browser_account_service.create_login_session(
            db,
            workspace_id,
            account_id,
            purpose=body.purpose,
            execution_id=body.execution_id,
            expected_revision=body.expected_revision,
            source_binding_revision_id=body.source_binding_revision_id,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    response = ApiResponse.ok(_session_read(session))
    # The next request may use another connection before dependency cleanup runs.
    await commit_session(db)
    return response


@router.get(
    "/{account_id}/login-sessions/{session_id}",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def get_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        session = await browser_account_service.get_login_session(
            db, workspace_id, account_id, session_id
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.post(
    "/{account_id}/login-sessions/{session_id}/view",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def view_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    try:
        session = await browser_account_service.get_login_session(
            db, workspace_id, account_id, session_id
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.post(
    "/{account_id}/login-sessions/{session_id}/takeover",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def takeover_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    expected_revision = _effective_revision(if_match, None)
    try:
        session = await browser_account_service.takeover_login_session(
            db, workspace_id, account_id, session_id, expected_revision=expected_revision
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.post(
    "/{account_id}/login-sessions/{session_id}/refresh",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def refresh_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    body: LoginSessionRefresh,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    try:
        session = await browser_account_service.refresh_login_session(
            db,
            workspace_id,
            account_id,
            session_id,
            expected_view_generation=body.expected_view_generation,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.post(
    "/{account_id}/login-sessions/{session_id}/confirm",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def confirm_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    body: LoginSessionConfirm | None = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    expected_view_generation: int | None = Query(default=None, ge=0),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    revision = _effective_revision(if_match, body.expected_revision if body else None)
    view_generation = (
        expected_view_generation
        if expected_view_generation is not None
        else body.expected_view_generation
        if body
        else None
    )
    try:
        session = await browser_account_service.confirm_login_session(
            db,
            workspace_id,
            account_id,
            session_id,
            confirmed_by=access.user_id,
            expected_revision=revision,
            expected_view_generation=view_generation,
            platform_identity=body.platform_identity if body else None,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.post(
    "/{account_id}/login-sessions/{session_id}/close",
    response_model=ApiResponse[BrowserLoginSessionRead],
)
async def close_account_login_session(
    workspace_id: str,
    account_id: str,
    session_id: str,
    body: LoginSessionClose | None = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    reason: Literal["completed", "cancelled", "expired", "error"] = Query(default="cancelled"),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    expected_revision = _effective_revision(
        if_match,
        body.expected_revision if body else None,
    )
    try:
        session = await browser_account_service.close_login_session(
            db,
            workspace_id,
            account_id,
            session_id,
            reason=body.reason if body else reason,
            expected_revision=expected_revision,
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(_session_read(session))


@router.get(
    "/{account_id}/login-sessions/{session_id}/authorization-facts",
    response_model=ApiResponse[PortalAuthorizationFactsV1],
)
async def get_login_authorization_facts(
    workspace_id: str,
    account_id: str,
    session_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    try:
        facts = await browser_account_service.get_portal_authorization_facts(
            db, workspace_id, account_id, session_id, subject=identity.subject
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    return ApiResponse.ok(facts)


def _http_origin(scheme: str, netloc: str) -> str:
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return f"{scheme}://{netloc}"


def _portal_expected_origin(scheme: str, netloc: str) -> str:
    from urllib.parse import urlsplit

    from backend.config import get_settings

    configured = get_settings().browser_portal_public_origin
    if not configured:
        return _http_origin(scheme, netloc)
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("browser portal public origin must be an exact HTTP origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin header required")
    expected = _portal_expected_origin(request.url.scheme, request.url.netloc)
    if origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin is not allowed")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_ACTIVE_PORTAL_SOCKETS: dict[
    tuple[str, str, str, str, str],
    dict[str, object],
] = {}
_PORTAL_AUTHORIZATION_TASK: asyncio.Task[None] | None = None


async def _close_portal_socket(
    key: tuple[str, str, str, str, str],
    state: dict[str, object],
    *,
    reason: str,
    code: int = 4403,
) -> None:
    _ACTIVE_PORTAL_SOCKETS.pop(key, None)
    websocket = state["websocket"]
    try:
        await websocket.close(code=code, reason=reason)  # type: ignore[union-attr]
    except Exception:
        # The peer may already have disconnected; revocation remains fail-closed.
        pass


async def _portal_authorization_monitor() -> None:
    """Batch fresh permission reads and close revoked portals before one second."""

    global _PORTAL_AUTHORIZATION_TASK
    try:
        while _ACTIVE_PORTAL_SOCKETS:
            await asyncio.sleep(0.1)
            entries = list(_ACTIVE_PORTAL_SOCKETS.items())
            requests = [
                (
                    key[0],
                    key[1],
                    key[2],
                    str(state["subject"]),
                    str(state["owner_digest"]),
                )
                for key, state in entries
            ]
            try:
                async with AsyncSessionLocal() as db:
                    snapshots = await browser_account_service.get_portal_authorization_batch(
                        db, requests
                    )
            except Exception:
                # A timed-out or unavailable authorization read is not permission.
                for key, state in entries:
                    await _close_portal_socket(
                        key, state, reason="Portal authorization refresh failed"
                    )
                continue
            checked_at = datetime.now(UTC)
            for key, state in entries:
                if _ACTIVE_PORTAL_SOCKETS.get(key) is not state:
                    continue
                snapshot = snapshots.get(
                    (
                        key[0],
                        key[1],
                        key[2],
                        str(state["subject"]),
                        str(state["owner_digest"]),
                    )
                )
                owner_expires_at = state["owner_expires_at"]
                owner_hard_expires_at = state["owner_hard_expires_at"]
                if (
                    snapshot is None
                    or checked_at >= snapshot.facts.freshness_deadline
                    or not snapshot.facts.membership_exists
                    or not snapshot.owner_active
                    or snapshot.facts.user_disabled
                    or not snapshot.facts.workspace_active
                    or snapshot.facts.role not in {"admin", "maintainer", "operator"}
                    or snapshot.facts.session_revoked
                    or snapshot.account_paused
                    or (
                        snapshot.account_auth_required
                        and snapshot.session_purpose == "execution"
                    )
                    or snapshot.facts.session_revision != state["session_revision"]
                    or checked_at >= owner_expires_at
                    or checked_at >= owner_hard_expires_at
                ):
                    await _close_portal_socket(
                        key, state, reason="Portal authorization has been revoked"
                    )
    finally:
        _PORTAL_AUTHORIZATION_TASK = None


def _ensure_portal_authorization_monitor() -> None:
    global _PORTAL_AUTHORIZATION_TASK
    if _PORTAL_AUTHORIZATION_TASK is None or _PORTAL_AUTHORIZATION_TASK.done():
        _PORTAL_AUTHORIZATION_TASK = asyncio.create_task(_portal_authorization_monitor())


@router.post(
    "/{account_id}/login-sessions/{session_id}/portal-ticket/issue",
    response_model=ApiResponse[PortalTicketIssuedV1],
)
async def issue_account_portal_ticket(
    workspace_id: str,
    account_id: str,
    session_id: str,
    body: PortalTicketIssueRequestV1,
    request: Request,
    response: Response,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_same_origin(request)
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    if body.account_ref != _account_ref(workspace_id, account_id) or body.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Portal session not found")
    try:
        issued = await browser_account_service.issue_portal_ticket(
            db, body, subject=identity.subject
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    payload = issued.model_dump(mode="json")
    payload["ticket"] = issued.ticket.get_secret_value()
    payload["csrf_token"] = issued.csrf_token.get_secret_value()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": payload, "error": None, "meta": None},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post(
    "/{account_id}/login-sessions/{session_id}/portal-ticket/redeem",
    response_model=ApiResponse[PortalEntryResponseV1],
)
async def redeem_account_portal_ticket(
    workspace_id: str,
    account_id: str,
    session_id: str,
    body: PortalTicketRedeemRequestV1,
    request: Request,
    response: Response,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    _require_same_origin(request)
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    if body.account_ref != _account_ref(workspace_id, account_id) or body.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Portal session not found")
    owner_token = secrets.token_urlsafe(32)
    try:
        outcome = await browser_account_service.redeem_portal_ticket(
            db, body, subject=identity.subject, owner_token=owner_token
        )
    except browser_account_service.BrowserAccountError as exc:
        raise _http_error(exc) from exc
    if getattr(outcome, "status", None) == "granted":
        response.set_cookie(
            key=outcome.cookie_name,
            value=owner_token,
            max_age=max(1, int((outcome.expires_at - datetime.now(UTC)).total_seconds())),
            httponly=True,
            secure=True,
            samesite=outcome.same_site,
            path="/",
        )
    return ApiResponse.ok(outcome)


@router.post(
    "/{account_id}/login-sessions/{session_id}/native-window",
    response_model=ApiResponse[BrowserNativeWindowResponse],
)
async def open_account_browser_native_window(
    workspace_id: str,
    account_id: str,
    session_id: str,
    request: Request,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """Launch the configured local viewer for one authorized browser session."""

    from backend.services.browser_desktop_service import authorize_browser_desktop
    from backend.services.browser_native_window import (
        BrowserNativeWindowError,
        native_window_manager,
    )

    _require_same_origin(request)
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    support = native_window_manager.support()
    if not support.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, support.message)
    authorization = await authorize_browser_desktop(
        db,
        workspace_id,
        account_id,
        session_id,
        subject=identity.subject,
    )
    if authorization is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "隔离浏览器尚未就绪或授权已失效"
        )
    try:
        account = await browser_account_service.get_browser_account(
            db, workspace_id, account_id
        )
        result = await native_window_manager.open(
            authorization,
            subject=identity.subject,
            account_label=account.label,
        )
    except BrowserNativeWindowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return ApiResponse.ok(BrowserNativeWindowResponse(**result.__dict__))


@router.post(
    "/{account_id}/login-sessions/{session_id}/browser-grant",
    response_model=ApiResponse[BrowserDesktopGrantResponse],
)
async def issue_browser_desktop_grant(
    workspace_id: str,
    account_id: str,
    session_id: str,
    request: Request,
    response: Response,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """Exchange current API authorization for a short-lived HttpOnly desktop cookie."""

    from backend.services.browser_desktop_service import (
        DESKTOP_COOKIE_NAME,
        authorize_browser_desktop,
        grant_store,
    )

    _require_same_origin(request)
    access = await get_workspace_access(db, workspace_id, identity)
    _require_operator_or_manager(access)
    authorization = await authorize_browser_desktop(
        db, workspace_id, account_id, session_id, subject=identity.subject
    )
    if authorization is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "隔离浏览器尚未就绪或授权已失效")
    token, grant = grant_store.issue(authorization, subject=identity.subject)
    websocket_path = (
        f"/api/v1/workspaces/{workspace_id}/browser-accounts/{account_id}"
        f"/login-sessions/{session_id}/browser"
    )
    response.set_cookie(
        key=DESKTOP_COOKIE_NAME,
        value=token,
        max_age=max(1, int((grant.expires_at - datetime.now(UTC)).total_seconds())),
        httponly=True,
        secure=True,
        samesite="strict",
        path=websocket_path,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return ApiResponse.ok(
        BrowserDesktopGrantResponse(
            websocket_path=websocket_path,
            expires_at=grant.expires_at,
            hard_expires_at=grant.hard_expires_at,
            session_id=session_id,
            session_revision=authorization.session_revision,
        )
    )


@router.websocket("/{account_id}/login-sessions/{session_id}/browser")
async def account_browser_desktop_websocket(
    websocket: WebSocket,
    workspace_id: str,
    account_id: str,
    session_id: str,
) -> None:
    """Relay a raw noVNC/RFB stream without exposing the node VNC endpoint."""

    from backend import ws_agent_manager
    from backend.browser_desktop_protocol import DESKTOP_MAX_PAYLOAD_BYTES
    from backend.services.browser_desktop_service import (
        DESKTOP_COOKIE_NAME,
        authorize_browser_desktop,
        grant_matches_authorization,
        grant_store,
    )

    origin = websocket.headers.get("origin")
    expected_origin = _portal_expected_origin(websocket.url.scheme, websocket.url.netloc)
    cookie = websocket.cookies.get(DESKTOP_COOKIE_NAME)
    grant = grant_store.resolve(cookie) if cookie else None
    if (
        grant is None
        or not origin
        or origin.rstrip("/") != expected_origin.rstrip("/")
        or (grant.workspace_id, grant.account_id, grant.session_id)
        != (workspace_id, account_id, session_id)
    ):
        await websocket.close(code=4403, reason="Desktop origin or cookie authentication failed")
        return
    async with AsyncSessionLocal() as db:
        authorization = await authorize_browser_desktop(
            db, workspace_id, account_id, session_id, subject=grant.subject
        )
    if authorization is None or not grant_matches_authorization(grant, authorization):
        grant_store.revoke(grant.digest)
        await websocket.close(code=4403, reason="Desktop authorization is no longer current")
        return

    await websocket.accept()
    transport = None
    stop = asyncio.Event()
    relay_tasks: list[asyncio.Task[None]] = []

    async def monitor_authorization() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
                return
            except TimeoutError:
                pass
            current = grant_store.resolve(cookie)
            if current is None or datetime.now(UTC) >= grant.hard_expires_at:
                break
            try:
                async with asyncio.timeout(0.5):
                    async with AsyncSessionLocal() as monitor_db:
                        snapshot = await authorize_browser_desktop(
                            monitor_db,
                            workspace_id,
                            account_id,
                            session_id,
                            subject=grant.subject,
                        )
            except Exception:
                snapshot = None
            if snapshot is None or not grant_matches_authorization(grant, snapshot):
                break
        stop.set()
        try:
            await websocket.close(code=4403, reason="Desktop authorization was revoked")
        except Exception:
            pass

    try:
        monitor_task = asyncio.create_task(monitor_authorization())
        relay_tasks.append(monitor_task)
        transport = await ws_agent_manager.open_browser_desktop_route(
            authorization.endpoint,
            authorization.envelope,
            expires_at=grant.hard_expires_at,
        )
        if stop.is_set() or grant_store.resolve(cookie) is None:
            raise RuntimeError("desktop authorization ended while opening the node route")

        async def relay_input() -> None:
            while not stop.is_set():
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload = message.get("bytes")
                if payload is None:
                    raise ValueError("desktop transport accepts binary RFB frames only")
                if not payload or len(payload) > DESKTOP_MAX_PAYLOAD_BYTES:
                    raise ValueError("desktop RFB frame exceeds the transport limit")
                await transport.send(payload)

        async def relay_output() -> None:
            while not stop.is_set():
                payload = await transport.receive()
                if payload is None:
                    return
                await websocket.send_bytes(payload)

        relay_tasks.extend([
            asyncio.create_task(relay_input()),
            asyncio.create_task(relay_output()),
        ])
        done, pending = await asyncio.wait(
            relay_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    except Exception:
        logger.info("Browser desktop connection ended", exc_info=True)
    finally:
        stop.set()
        for task in relay_tasks:
            if not task.done():
                task.cancel()
        if relay_tasks:
            await asyncio.gather(*relay_tasks, return_exceptions=True)
        if transport is not None:
            await asyncio.shield(transport.close(reason="viewer_disconnected"))
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/{account_id}/login-sessions/{session_id}/portal")
async def account_portal_websocket(
    websocket: WebSocket,
    workspace_id: str,
    account_id: str,
    session_id: str,
) -> None:
    origin = websocket.headers.get("origin")
    expected = _portal_expected_origin(websocket.url.scheme, websocket.url.netloc)
    cookie = websocket.cookies.get("qrac2_portal")
    if not cookie or not origin or origin.rstrip("/") != expected.rstrip("/"):
        await websocket.close(code=4403, reason="Portal origin or cookie authentication failed")
        return
    digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        owner = await db.scalar(
            select(BrowserPortalOwner).where(
                BrowserPortalOwner.owner_digest == digest,
                BrowserPortalOwner.workspace_id == workspace_id,
                BrowserPortalOwner.account_id == account_id,
                BrowserPortalOwner.session_id == session_id,
                BrowserPortalOwner.active.is_(True),
                BrowserPortalOwner.revoked_at.is_(None),
            )
        )
        session = await db.scalar(
            select(BrowserLoginSession).where(
                BrowserLoginSession.workspace_id == workspace_id,
                BrowserLoginSession.account_id == account_id,
                BrowserLoginSession.id == session_id,
            )
        )
        account = await db.scalar(
            select(BrowserAccount).where(
                BrowserAccount.workspace_id == workspace_id,
                BrowserAccount.id == account_id,
            )
        )
        authorization = (
            await browser_account_service.get_portal_authorization_batch(
                db, [(workspace_id, account_id, session_id, owner.subject, digest)]
            )
            if owner is not None
            else {}
        )
        authorized = (
            authorization.get((workspace_id, account_id, session_id, owner.subject, digest))
            if owner is not None
            else None
        )
    if (
        owner is None
        or session is None
        or account is None
        or authorized is None
        or not authorized.owner_active
        or authorized.facts.user_disabled
        or not authorized.facts.workspace_active
        or authorized.facts.role not in {"admin", "maintainer", "operator"}
        or authorized.facts.session_revoked
        or _as_utc(owner.expires_at) <= now
        or _as_utc(owner.hard_expires_at) <= now
        or owner.session_revision != session.revision
        or account.paused
        or (account.auth_required and session.purpose == "execution")
        or session.status in {"closed", "expired", "error"}
    ):
        await websocket.close(code=4403, reason="Portal owner credential is invalid")
        return
    await websocket.accept()
    key = (workspace_id, account_id, session_id, owner.subject, secrets.token_urlsafe(12))
    state: dict[str, object] = {
        "websocket": websocket,
        "subject": owner.subject,
        "owner_digest": digest,
        "owner_expires_at": _as_utc(owner.expires_at),
        "owner_hard_expires_at": _as_utc(owner.hard_expires_at),
        "session_revision": session.revision,
    }
    _ACTIVE_PORTAL_SOCKETS[key] = state
    _ensure_portal_authorization_monitor()
    transport = None
    relays: list[asyncio.Task[None]] = []
    portal_stage = "session_envelope"
    try:
        from backend import ws_agent_manager
        from backend.services.browser_portal_contract import (
            decode_portal_wire_frame,
            encode_portal_wire_frame,
            portal_wire_metadata_length,
            validate_portal_frame_binding,
        )

        async with AsyncSessionLocal() as db:
            (
                endpoint,
                envelope,
                revision,
            ) = await browser_account_service.get_portal_session_envelope(
                db, workspace_id, account_id, session_id
            )
        portal_stage = "prepare_route"
        route = await ws_agent_manager.prepare_portal_route(
            endpoint, envelope, session_revision=revision, timeout=15
        )
        portal_stage = "validate_route"
        binding = route.binding
        if (
            binding.account_ref.workspace_id != workspace_id
            or binding.account_ref.account_id != account_id
            or binding.session_id != session_id
            or binding.epoch != envelope.epoch
            or binding.view_generation != envelope.view_generation
            or (envelope.target.is_complete() and binding.target != envelope.target)
            or route.node_identity.node_id != envelope.node_id
            or route.node_identity.boot_id != envelope.node_boot_id
            or route.session_revision != revision
            or revision != owner.session_revision
            or route.route_expires_at > envelope.lease_expires_at
            or route.route_expires_at <= datetime.now(UTC)
            or _ACTIVE_PORTAL_SOCKETS.get(key) is not state
        ):
            raise ValueError("portal route no longer matches its authorized session")
        portal_stage = "open_route"
        transport = await ws_agent_manager.open_portal_route(endpoint, route)
        portal_stage = "relay"

        async def relay_input() -> None:
            sequence = -1
            while _ACTIVE_PORTAL_SOCKETS.get(key) is state:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if datetime.now(UTC) >= route.route_expires_at:
                    return
                wire = message.get("bytes")
                if wire is None:
                    raise ValueError("portal controls require binary framing")
                frame = decode_portal_wire_frame(wire)
                if frame.encoding != "control-json" or frame.sequence <= sequence:
                    raise ValueError("portal control framing or sequence is invalid")
                validate_portal_frame_binding(route, frame)
                sequence = frame.sequence
                # JS omits optional defaults which Pydantic materializes. The
                # received header was validated by the decoder; forwarding uses
                # the canonical model's byte length, not the original JSON size.
                frame = frame.model_copy(
                    update={
                        "layout": frame.layout.model_copy(
                            update={"metadata_bytes": portal_wire_metadata_length(frame)}
                        )
                    }
                )
                await transport.send(frame)

        async def relay_pixels() -> None:
            sequence = -1
            while _ACTIVE_PORTAL_SOCKETS.get(key) is state:
                remaining = (route.route_expires_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    return
                try:
                    frame = await transport.receive(timeout=min(remaining, 1.0))
                except TimeoutError:
                    continue
                if frame is None:
                    return
                if datetime.now(UTC) >= route.route_expires_at:
                    return
                if frame.encoding != "pixel-binary" or frame.sequence <= sequence:
                    raise ValueError("portal pixel framing or sequence is invalid")
                validate_portal_frame_binding(route, frame)
                sequence = frame.sequence
                if _ACTIVE_PORTAL_SOCKETS.get(key) is not state:
                    return
                await websocket.send_bytes(encode_portal_wire_frame(frame))

        relays = [asyncio.create_task(relay_input()), asyncio.create_task(relay_pixels())]
        finished, _ = await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        for relay in finished:
            relay.result()
        if _ACTIVE_PORTAL_SOCKETS.get(key) is state and datetime.now(UTC) >= route.route_expires_at:
            await _close_portal_socket(key, state, reason="Portal route expired", code=1000)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        # Transient controls and pixels must never enter exception logs.
        logging.getLogger(__name__).warning(
            "Portal connection ended stage=%s error_type=%s", portal_stage, type(exc).__name__
        )
        await _close_portal_socket(key, state, reason="Portal connection ended")
    finally:
        # ASGI cancellation must not interrupt transient-buffer/route release.
        with anyio.CancelScope(shield=True):
            for relay in relays:
                relay.cancel()
            if relays:
                await asyncio.gather(*relays, return_exceptions=True)
            if transport is not None:
                with anyio.move_on_after(2):
                    await transport.close(reason="portal_closed")
            if _ACTIVE_PORTAL_SOCKETS.get(key) is state:
                await _close_portal_socket(key, state, reason="Portal connection ended")


__all__ = ["router"]
