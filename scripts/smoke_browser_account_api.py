"""通过真实 HTTP API 验证账号与 portal ticket 的最小业务往返。

脚本只适用于专用测试 workspace；创建的账号不会自动删除，因为账号 API
没有 DELETE 操作。请求错误只输出状态码和结构化错误码，不输出响应正文中的
票据、CSRF 或 Cookie。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

API = os.environ.get("OPENCLI_ADMIN_API", "http://127.0.0.1:8031/api/v1").rstrip("/")
WORKSPACE_ID = os.environ.get("QRAC2_WORKSPACE_ID", "").strip()


def _headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    fleet_token = os.environ.get("OPENCLI_API_TOKEN", "").strip()
    bearer_token = os.environ.get("OPENCLI_BEARER_TOKEN", "").strip()
    if fleet_token:
        headers["X-API-Token"] = fleet_token
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    headers.update(extra or {})
    return headers


def _origin() -> str:
    parsed = urlsplit(API)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("OPENCLI_ADMIN_API must include scheme and host")
    return f"{parsed.scheme}://{parsed.netloc}"


def _error_code(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unstructured_error"
    detail = payload.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    if isinstance(payload.get("code"), str):
        return payload["code"]
    return "api_error"


def _request(
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 200,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{API}{path}", data=data, headers=_headers(headers), method=method)
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            raw = response.read()
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_payload = None
        raise RuntimeError(
            f"{method} {path}: HTTP {exc.code} ({_error_code(error_payload)})"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path}: transport failure ({exc.reason})") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{method} {path}: response is not JSON") from exc
    if status != expected_status:
        raise RuntimeError(f"{method} {path}: HTTP {status} ({_error_code(body)})")
    if not isinstance(body, dict) or body.get("success") is not True:
        raise RuntimeError(f"{method} {path}: {_error_code(body)}")
    if "data" not in body:
        raise RuntimeError(f"{method} {path}: response has no data")
    return body["data"], response_headers


def _route(account_id: str | None = None, suffix: str = "") -> str:
    route = f"/workspaces/{WORKSPACE_ID}/browser-accounts"
    if account_id is not None:
        route += f"/{account_id}"
    return route + suffix


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> dict[str, str]:
    if not WORKSPACE_ID:
        raise RuntimeError("QRAC2_WORKSPACE_ID must identify a dedicated test workspace")
    idempotency = secrets.token_urlsafe(18)
    account, _ = _request(
        "POST",
        _route(),
        {
            "workspace_id": WORKSPACE_ID,
            "site": "fixture.test",
            "label": f"API smoke {idempotency}",
        },
        headers={"Idempotency-Key": idempotency},
        expected_status=201,
    )
    account_id = account["id"]
    _assert(account["workspace_id"] == WORKSPACE_ID, "created account workspace mismatch")
    _assert(
        account["status"] == "dormant" and account["revision"] == 0,
        "created account state mismatch",
    )

    listing, _ = _request("GET", _route())
    _assert(isinstance(listing, dict), "account list is not an object")
    _assert(
        any(row["id"] == account_id for row in listing.get("items", [])),
        "created account is absent from list",
    )
    fetched, _ = _request("GET", _route(account_id))
    _assert(fetched["id"] == account_id, "account detail id mismatch")

    paused, _ = _request(
        "PATCH",
        _route(account_id),
        {"expected_revision": 0, "paused": True},
        headers={"If-Match": "0"},
    )
    _assert(
        paused["paused"] is True and paused["revision"] == 1,
        "pause revision did not advance",
    )
    resumed, _ = _request(
        "POST",
        _route(account_id, "/resume"),
        {
            "account_ref": {"workspace_id": WORKSPACE_ID, "account_id": account_id},
            "expected_revision": 1,
            "operation": "resume",
        },
        headers={"If-Match": "1"},
    )
    _assert(resumed["operation"] == "resume", "resume operation contract mismatch")
    _assert(resumed["account_ref"]["account_id"] == account_id, "resume account mismatch")
    _assert(
        resumed["paused"] is False and resumed["revision"] == 2,
        "resume revision did not advance",
    )

    session, _ = _request(
        "POST",
        _route(account_id, "/login-sessions"),
        {"purpose": "login", "expected_revision": resumed["revision"]},
        headers={"Idempotency-Key": secrets.token_urlsafe(18)},
        expected_status=201,
    )
    session_id = session["id"]
    session_revision = session["revision"]
    session_route = _route(account_id, f"/login-sessions/{session_id}")
    _assert(
        session["status"] == "opening" and session_revision == 0,
        "login session state mismatch",
    )
    sessions, _ = _request("GET", _route(account_id, "/login-sessions"))
    _assert(
        any(row["id"] == session_id for row in sessions),
        "created login session is absent from list",
    )
    viewed, _ = _request("POST", f"{session_route}/view")
    _assert(viewed["id"] == session_id, "login session view mismatch")

    csrf_token = secrets.token_urlsafe(24)
    account_ref = {"workspace_id": WORKSPACE_ID, "account_id": account_id}
    issued, _ = _request(
        "POST",
        f"{session_route}/portal-ticket/issue",
        {
            "contract_version": 1,
            "first_entry": "initial",
            "account_ref": account_ref,
            "session_id": session_id,
            "expected_session_revision": session_revision,
            "csrf_token": csrf_token,
        },
        headers={"Origin": _origin()},
    )
    ticket = issued["ticket"]
    issued_csrf = issued["csrf_token"]
    _assert(
        issued["status"] == "issued" and len(ticket) >= 16,
        "portal ticket issue contract mismatch",
    )
    _assert(issued_csrf == csrf_token, "portal issue did not return the request CSRF token")

    grant, response_headers = _request(
        "POST",
        f"{session_route}/portal-ticket/redeem",
        {
            "contract_version": 1,
            "first_entry": "initial",
            "account_ref": account_ref,
            "session_id": session_id,
            "expected_session_revision": session_revision,
            "ticket_id": issued["ticket_id"],
            "ticket": ticket,
            "csrf_token": issued_csrf,
        },
        headers={"Origin": _origin()},
    )
    _assert(grant["status"] == "granted", "portal ticket redemption was not granted")
    _assert(grant["cookie_name"] == "qrac2_portal", "portal cookie name mismatch")
    _assert(
        "qrac2_portal=" in response_headers.get("set-cookie", ""),
        "portal cookie was not issued",
    )
    _assert("ticket" not in grant and "csrf_token" not in grant, "portal grant leaked a secret")

    closed, _ = _request(
        "POST",
        f"{session_route}/close",
        {"reason": "completed"},
        headers={"If-Match": str(session_revision)},
    )
    _assert(
        closed["id"] == session_id and closed["status"] == "closed",
        "session close contract mismatch",
    )
    return {"workspace_id": WORKSPACE_ID, "account_id": account_id, "session_id": session_id}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=None, help="覆盖 OPENCLI_ADMIN_API")
    parser.add_argument("--workspace-id", default=None, help="覆盖 QRAC2_WORKSPACE_ID")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global API, WORKSPACE_ID
    if args.api:
        API = args.api.rstrip("/")
    if args.workspace_id:
        WORKSPACE_ID = args.workspace_id
    try:
        print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
