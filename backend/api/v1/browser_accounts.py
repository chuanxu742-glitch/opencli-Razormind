"""Workspace-scoped browser-account and login-session API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal
import secrets
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
from sqlalchemy import select
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import AsyncSessionLocal, get_db
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
    PortalTicketIssueRequestV1,
    PortalTicketIssuedV1,
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


router = APIRouter(
    prefix="/workspaces/{workspace_id}/browser-accounts",
    tags=["browser-accounts"],
)


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
        body.account_ref.workspace_id != workspace_id
        or body.account_ref.account_id != account_id
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
    return BrowserLoginSessionRead.model_validate(session)


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


@router.post("", response_model=ApiResponse[BrowserAccountRead], status_code=status.HTTP_201_CREATED)
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
    return ApiResponse.ok(_session_read(session))


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


@router.post("/{account_id}/login-sessions/{session_id}/refresh", response_model=ApiResponse[BrowserLoginSessionRead])
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
        else body.expected_view_generation if body else None
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
    reason: Literal["completed", "cancelled", "expired", "error"] = Query(
        default="cancelled"
    ),
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


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin header required")
    expected = _http_origin(request.url.scheme, request.url.netloc)
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
) -> None:
    _ACTIVE_PORTAL_SOCKETS.pop(key, None)
    websocket = state["websocket"]
    try:
        await websocket.close(code=4403, reason=reason)  # type: ignore[union-attr]
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
                    or snapshot.facts.session_revoked
                    or snapshot.account_paused
                    or (
                        snapshot.account_auth_required
                        and snapshot.session_purpose != "login"
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


@router.websocket("/{account_id}/login-sessions/{session_id}/portal")
async def account_portal_websocket(
    websocket: WebSocket,
    workspace_id: str,
    account_id: str,
    session_id: str,
) -> None:
    origin = websocket.headers.get("origin")
    expected = _http_origin(websocket.url.scheme, websocket.url.netloc)
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
    if (
        owner is None
        or session is None
        or account is None
        or _as_utc(owner.expires_at) <= now
        or _as_utc(owner.hard_expires_at) <= now
        or owner.session_revision != session.revision
        or account.paused
        or (account.auth_required and session.purpose != "login")
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
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # A-to-R transient frame relay is intentionally not implicit: until
            # R registers its dedicated PortalOwnerRoute transport, accepting and
            # discarding input would be a credential/pixel sink.
            await websocket.close(code=1011, reason="Portal transport is unavailable")
            return
    except WebSocketDisconnect:
        return
    finally:
        if _ACTIVE_PORTAL_SOCKETS.get(key) is state:
            _ACTIVE_PORTAL_SOCKETS.pop(key, None)


__all__ = ["router"]
