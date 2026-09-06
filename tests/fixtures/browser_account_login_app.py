"""用于账号登录协议测试的受控本地站点。

这是一个真实的 HTTP 页面，不代表任何真实平台，也不实现业务账号服务。每个浏览器
会话由独立的 HttpOnly cookie 绑定；场景和事件通过显式控制接口驱动。表单请求体会
被读取并丢弃，永不记录。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

import qrcode
from qrcode.image.svg import SvgImage

_LOOPBACK_HOST = "127.0.0.1"
_COOKIE_NAME = "fixture_session"
_SCENARIOS = {
    "qr",
    "form",
    "expired",
    "challenge",
    "wrong_identity",
    "navigation",
    "multi_origin",
    "sensitive",
    "password_to_text",
    "otp_token",
}
_EVENTS = {
    "expire_qr",
    "refresh_qr",
    "navigate",
    "challenge",
    "resolve_challenge",
    "wrong_identity",
    "set_identity",
    "authenticate",
    "sensitive",
    "password_to_text",
    "otp_token",
    "reset",
}
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")


@dataclass
class _LoginState:
    """仅保存可公开断言的登录状态，不保存任何表单内容。"""

    scenario: str = "qr"
    flow_state: str = "presenting"
    expected_identity: str = "fixture-account-alice"
    presented_identity: str = "fixture-account-alice"
    authenticated: bool = False
    identity_status: str = "unknown"
    qr_status: str = "presenting"
    qr_generation: int = 1
    qr_challenge: str | None = None
    qr_expires_at: float | None = None
    document_generation: int = 1
    view_generation: int = 1
    frame_id: str = "frame-label-1"
    challenge_required: bool = False
    sensitive_mode: bool = False
    password_input_type: str = "password"
    otp_visible: bool = False
    token_visible: bool = False
    submitted_count: int = 0
    events: list[str] = field(default_factory=list)


class ControlledLoginSite:
    """可在测试中启动和控制的本地登录站点。"""

    def __init__(self, *, host: str = _LOOPBACK_HOST, port: int = 0) -> None:
        if host != _LOOPBACK_HOST:
            raise ValueError("受控登录站点只允许绑定 127.0.0.1")
        if port < 0 or port > 65535:
            raise ValueError("端口必须在 0 到 65535 之间")
        self.host = host
        self.requested_port = port
        self._lock = threading.RLock()
        self._sessions: dict[str, _LoginState] = {}
        self._challenges: dict[str, tuple[str, int, float]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server_address(self) -> tuple[str, int] | None:
        """返回实际监听地址；未启动时返回 ``None``。"""
        if self._server is None:
            return None
        address = self._server.server_address
        return str(address[0]), int(address[1])

    @property
    def base_url(self) -> str:
        address = self.server_address
        if address is None:
            raise RuntimeError("站点尚未启动")
        return f"http://{address[0]}:{address[1]}"

    @property
    def alternate_origin(self) -> str:
        """返回同端口的另一回环 origin，用于跨 origin 测试。"""
        address = self.server_address
        if address is None:
            raise RuntimeError("站点尚未启动")
        return f"http://localhost:{address[1]}"

    @property
    def session_ids(self) -> tuple[str, ...]:
        """返回已由浏览器创建的会话 ID，供测试控制指定会话。"""
        with self._lock:
            return tuple(self._sessions)

    def start(self) -> ControlledLoginSite:
        if self._server is not None:
            return self
        self._server = self._build_server()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="controlled-login-fixture",
            daemon=True,
        )
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """以单一 serving loop 启动命令行站点。"""
        if self._server is not None:
            raise RuntimeError("站点已经启动")
        self._server = self._build_server()
        print(f"LOGIN_FIXTURE_URL={self.base_url}", flush=True)
        try:
            self._server.serve_forever()
        finally:
            self.stop()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def __enter__(self) -> ControlledLoginSite:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _build_server(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((self.host, self.requested_port), self._make_handler())
        server.daemon_threads = True
        return server

    def _new_session_locked(self) -> tuple[str, _LoginState]:
        session_id = secrets.token_urlsafe(18)
        state = _LoginState()
        self._sessions[session_id] = state
        self._rotate_qr_locked(session_id, state)
        return session_id, state

    def _state_locked(self, session_id: str) -> _LoginState:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError("unknown fixture session") from exc

    def _rotate_qr_locked(self, session_id: str, state: _LoginState) -> None:
        if state.qr_challenge is not None:
            self._challenges.pop(state.qr_challenge, None)
        challenge = secrets.token_urlsafe(18)
        expires_at = time.time() + 30
        state.qr_challenge = challenge
        state.qr_expires_at = expires_at
        self._challenges[challenge] = (session_id, state.qr_generation, expires_at)

    def _reset_state_locked(self, session_id: str) -> _LoginState:
        previous = self._state_locked(session_id)
        if previous.qr_challenge is not None:
            self._challenges.pop(previous.qr_challenge, None)
        state = _LoginState()
        self._sessions[session_id] = state
        self._rotate_qr_locked(session_id, state)
        return state

    def state(self, session_id: str) -> dict[str, Any]:
        """返回指定 cookie 会话的无秘密状态快照。"""
        with self._lock:
            state = self._state_locked(session_id)
            trusted = state.authenticated and state.identity_status == "valid" and not state.challenge_required
            return {
                "session_id": session_id,
                "scenario": state.scenario,
                "flow_state": state.flow_state,
                "expected_identity": state.expected_identity,
                "presented_identity": state.presented_identity,
                "authenticated": state.authenticated,
                "trusted": trusted,
                "identity_status": state.identity_status,
                "qr_status": state.qr_status,
                "qr_generation": state.qr_generation,
                "document_generation": state.document_generation,
                "view_generation": state.view_generation,
                "frame_label": state.frame_id,
                "challenge_required": state.challenge_required,
                "sensitive_mode": state.sensitive_mode,
                "password_input_type": state.password_input_type,
                "otp_visible": state.otp_visible,
                "token_visible": state.token_visible,
                "submitted_count": state.submitted_count,
                "events": list(state.events),
            }

    def set_scenario(
        self,
        scenario: str,
        *,
        session_id: str,
        expected_identity: str | None = None,
    ) -> dict[str, Any]:
        """为一个已创建的 cookie 会话选择固定场景并重置其状态。"""
        if scenario not in _SCENARIOS:
            raise ValueError(f"未知登录场景: {scenario}")
        if expected_identity is not None and not _SAFE_IDENTITY.fullmatch(expected_identity):
            raise ValueError("expected_identity 只能包含有限 ASCII 标识符")
        with self._lock:
            previous = self._state_locked(session_id)
            if previous.qr_challenge is not None:
                self._challenges.pop(previous.qr_challenge, None)
            state = _LoginState(
                scenario=scenario,
                expected_identity=expected_identity or previous.expected_identity,
            )
            state.presented_identity = state.expected_identity
            state.sensitive_mode = scenario in {"sensitive", "password_to_text", "otp_token"}
            state.password_input_type = "text" if scenario == "password_to_text" else "password"
            state.otp_visible = scenario == "otp_token"
            state.token_visible = scenario == "otp_token"
            if scenario == "expired":
                state.flow_state = "expired"
                state.qr_status = "expired"
            elif scenario == "challenge":
                state.flow_state = "challenge"
                state.qr_status = "hidden"
                state.challenge_required = True
            elif scenario == "wrong_identity":
                state.flow_state = "verifying"
                state.qr_status = "hidden"
                state.authenticated = True
                state.presented_identity = "fixture-account-bob"
                state.identity_status = "mismatch"
            elif scenario in {"form", "sensitive", "password_to_text", "otp_token"}:
                state.qr_status = "hidden"
            self._sessions[session_id] = state
            if state.qr_status == "presenting":
                self._rotate_qr_locked(session_id, state)
            self._record_event_locked(state, f"scenario:{scenario}")
            return self.state(session_id)

    def advance(
        self,
        event: str,
        *,
        session_id: str,
        identity: str | None = None,
    ) -> dict[str, Any]:
        """推进指定 cookie 会话的一个固定事件；事件参数不接收秘密。"""
        if event not in _EVENTS:
            raise ValueError(f"未知控制事件: {event}")
        with self._lock:
            state = self._state_locked(session_id)
            if event == "expire_qr":
                state.qr_status = "expired"
                state.flow_state = "expired"
                state.authenticated = False
            elif event == "refresh_qr":
                state.qr_generation += 1
                state.view_generation += 1
                state.qr_status = "presenting"
                state.flow_state = "presenting"
                state.authenticated = False
                state.identity_status = "unknown"
                self._rotate_qr_locked(session_id, state)
            elif event == "navigate":
                state.document_generation += 1
                state.view_generation += 1
                state.frame_id = f"frame-label-{state.document_generation}"
                state.flow_state = "verifying"
                state.identity_status = "valid" if state.authenticated else "unknown"
            elif event == "challenge":
                state.challenge_required = True
                state.flow_state = "challenge"
                state.qr_status = "hidden"
            elif event == "resolve_challenge":
                state.challenge_required = False
                state.flow_state = "verifying"
                state.identity_status = "valid" if state.authenticated else "unknown"
            elif event == "wrong_identity":
                state.presented_identity = "fixture-account-bob"
                state.authenticated = True
                state.identity_status = "mismatch"
                state.flow_state = "verifying"
                state.qr_status = "hidden"
            elif event == "set_identity":
                if identity is None or not _SAFE_IDENTITY.fullmatch(identity):
                    raise ValueError("identity 只能包含有限 ASCII 标识符")
                state.presented_identity = identity
                state.identity_status = "valid" if identity == state.expected_identity else "mismatch"
            elif event == "authenticate":
                state.qr_status = "hidden"
                state.authenticated = True
                state.flow_state = "authenticated"
                state.identity_status = "valid" if state.presented_identity == state.expected_identity else "mismatch"
            elif event == "sensitive":
                state.sensitive_mode = True
                state.flow_state = "presenting"
            elif event == "password_to_text":
                state.sensitive_mode = True
                state.password_input_type = "text"
            elif event == "otp_token":
                state.sensitive_mode = True
                state.otp_visible = True
                state.token_visible = True
            elif event == "reset":
                state = self._reset_state_locked(session_id)
            self._record_event_locked(state, event)
            return self.state(session_id)

    def _confirm_qr(self, challenge: str, generation: int) -> tuple[int, dict[str, Any]]:
        with self._lock:
            record = self._challenges.pop(challenge, None)
            if record is None:
                return 410, {"error": "qr_expired"}
            session_id, current_generation, expires_at = record
            state = self._state_locked(session_id)
            if (
                generation != current_generation
                or state.qr_generation != current_generation
                or state.qr_status != "presenting"
                or time.time() >= expires_at
            ):
                return 410, {"error": "qr_expired", "generation": generation}
            state.authenticated = True
            state.flow_state = "authenticated"
            state.qr_status = "hidden"
            state.presented_identity = state.expected_identity
            state.identity_status = "valid"
            self._record_event_locked(state, "qr_confirmed")
            return 200, {
                "confirmed": True,
                "session_id": session_id,
                "generation": current_generation,
            }

    def _record_event_locked(self, state: _LoginState, event: str) -> None:
        state.events.append(event)
        del state.events[:-50]

    def _qr_payload(self, state: _LoginState) -> str:
        if state.qr_challenge is None:
            raise RuntimeError("二维码挑战尚未创建")
        return (
            f"{self.base_url}/__confirm__/qr?challenge={state.qr_challenge}"
            f"&generation={state.qr_generation}"
        )

    @staticmethod
    def _encode_qr(payload: str) -> bytes:
        code = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        code.add_data(payload)
        code.make(fit=True)
        image = code.make_image(image_factory=SvgImage)
        output = BytesIO()
        image.save(output)
        return output.getvalue()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            _new_session_id: str | None = None

            def log_message(self, _format: str, *_args: object) -> None:
                # 禁止标准库把 URL、表单或控制请求写入 stderr。
                return

            def _send(
                self,
                body: bytes,
                *,
                status: int = 200,
                content_type: str = "text/html",
                set_session_cookie: str | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                if set_session_cookie is not None:
                    self.send_header(
                        "Set-Cookie",
                        f"{_COOKIE_NAME}={set_session_cookie}; Path=/; HttpOnly; SameSite=Lax",
                    )
                self.end_headers()
                self.wfile.write(body)

            def _json(self, value: Any, *, status: int = 200, set_session_cookie: str | None = None) -> None:
                self._send(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    status=status,
                    content_type="application/json",
                    set_session_cookie=set_session_cookie,
                )

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("控制请求必须是 JSON 对象")
                return value

            def _browser_session(self) -> tuple[str, _LoginState, str | None]:
                cookie_header = self.headers.get("Cookie", "")
                session_id = next(
                    (part.split("=", 1)[1] for part in cookie_header.split("; ") if part.startswith(f"{_COOKIE_NAME}=")),
                    None,
                )
                with site._lock:
                    if session_id not in site._sessions:
                        session_id, state = site._new_session_locked()
                        return session_id, state, session_id
                    return session_id, site._sessions[session_id], None

            def _control_session(self, payload: dict[str, Any]) -> str:
                session_id = payload.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("控制请求必须指定 session_id")
                with site._lock:
                    if session_id not in site._sessions:
                        raise ValueError("unknown fixture session")
                return session_id

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = parsed.path
                try:
                    if path == "/__confirm__/qr":
                        query = parse_qs(parsed.query)
                        challenge = query.get("challenge", [""])[0]
                        generation = int(query.get("generation", ["-1"])[0])
                        status, body = site._confirm_qr(challenge, generation)
                        self._json(body, status=status)
                        return
                    if (
                        path in {"/__control__/state", "/identity", "/auth-status", "/qr/current", "/", "/login", "/auth/callback", "/sensitive"}
                        or path.startswith("/frames/")
                        or path == "/qr/foreign.svg"
                    ):
                        session_id, state, new_cookie = self._browser_session()
                        if path == "/__control__/state":
                            self._json(site.state(session_id), set_session_cookie=new_cookie)
                        elif path == "/identity":
                            snapshot = site.state(session_id)
                            self._json(
                                {
                                    "identity": snapshot["presented_identity"],
                                    "identity_status": snapshot["identity_status"],
                                    "authenticated": snapshot["authenticated"],
                                    "document_generation": snapshot["document_generation"],
                                    "view_generation": snapshot["view_generation"],
                                },
                                set_session_cookie=new_cookie,
                            )
                        elif path == "/auth-status":
                            self._json(site._auth_status(session_id), set_session_cookie=new_cookie)
                        elif path == "/qr/current":
                            self._json(site._qr_status(session_id), set_session_cookie=new_cookie)
                        elif path.startswith("/frames/"):
                            self._send(site._render_frame(path, site.state(session_id)).encode("utf-8"), set_session_cookie=new_cookie)
                        elif path == "/qr/foreign.svg":
                            payload = f"{site.alternate_origin}/__confirm__/qr?challenge=foreign&generation={state.qr_generation}"
                            self._send(site._encode_qr(payload), content_type="image/svg+xml", set_session_cookie=new_cookie)
                        else:
                            self._send(site._render_page(path, site.state(session_id)).encode("utf-8"), set_session_cookie=new_cookie)
                        return
                    qr_match = re.fullmatch(r"/qr/(\d+)\.svg", path)
                    if qr_match:
                        session_id, state, new_cookie = self._browser_session()
                        generation = int(qr_match.group(1))
                        snapshot = site.state(session_id)
                        if generation != snapshot["qr_generation"] or snapshot["qr_status"] != "presenting":
                            self._json({"error": "qr_expired", "generation": generation}, status=410)
                        else:
                            with site._lock:
                                payload = site._qr_payload(state)
                            self._send(site._encode_qr(payload), content_type="image/svg+xml", set_session_cookie=new_cookie)
                        return
                    self._json({"error": "not_found"}, status=404)
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json({"error": "invalid_request"}, status=400)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = parsed.path
                try:
                    if path == "/__control__/scenario":
                        payload = self._read_json()
                        session_id = self._control_session(payload)
                        self._json(
                            site.set_scenario(
                                str(payload.get("scenario", "")),
                                session_id=session_id,
                                expected_identity=payload.get("expected_identity"),
                            )
                        )
                        return
                    if path == "/__control__/advance":
                        payload = self._read_json()
                        session_id = self._control_session(payload)
                        self._json(
                            site.advance(
                                str(payload.get("event", "")),
                                session_id=session_id,
                                identity=payload.get("identity"),
                            )
                        )
                        return
                    if path in {"/login/submit", "/form/submit"}:
                        session_id, _state, new_cookie = self._browser_session()
                        # 读取并丢弃请求体，不解析、不保存、不回显任何字段。
                        length = int(self.headers.get("Content-Length", "0"))
                        if length:
                            self.rfile.read(length)
                        with site._lock:
                            state = site._state_locked(session_id)
                            state.submitted_count += 1
                            site._record_event_locked(state, "form_submitted")
                        self._send(site._render_page("/login", site.state(session_id)).encode("utf-8"), set_session_cookie=new_cookie)
                        return
                    self._json({"error": "not_found"}, status=404)
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json({"error": "invalid_request"}, status=400)

        return Handler

    def _auth_status(self, session_id: str) -> dict[str, Any]:
        snapshot = self.state(session_id)
        return {
            "authenticated": snapshot["authenticated"],
            "trusted": snapshot["trusted"],
            "identity": snapshot["presented_identity"],
            "identity_status": snapshot["identity_status"],
            "evidence": {
                "identity_endpoint": "/identity",
                "state_endpoint": "/auth-status",
                "document_generation": snapshot["document_generation"],
                "view_generation": snapshot["view_generation"],
                "frame_label": snapshot["frame_label"],
            },
        }

    def _qr_status(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._state_locked(session_id)
            current = state.qr_status == "presenting" and state.qr_challenge is not None
            return {
                "status": state.qr_status,
                "generation": state.qr_generation,
                "origin": self.base_url,
                "image_url": f"{self.base_url}/qr/{state.qr_generation}.svg" if current else None,
                "confirmation_url": self._qr_payload(state) if current else None,
            }

    def _render_frame(self, path: str, state: dict[str, Any]) -> str:
        cross = path == "/frames/cross-origin"
        return f'''<!doctype html>
<html lang="zh-CN" data-frame-kind="{"cross-origin" if cross else "same-origin"}"
 data-document-generation="{state["document_generation"]}" data-view-generation="{state["view_generation"]}"
 data-frame-label="{escape(state["frame_label"])}">
<head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>受控原生 frame</title></head>
<body><article id="frame-content"><h2>{"跨 origin" if cross else "同 origin"}原生文档</h2>
<p>fixture generation label: document {state["document_generation"]}, view {state["view_generation"]}</p>
</article></body></html>'''

    def _render_page(self, path: str, state: dict[str, Any]) -> str:
        qr = ""
        if state["qr_status"] == "presenting":
            generation = state["qr_generation"]
            qr = (
                f'<section id="qr-region" data-qr-origin="{escape(self.base_url)}" '
                f'data-qr-generation="{generation}"><h2>扫码登录</h2>'
                f'<img id="login-qr" src="/qr/{generation}.svg" alt="受控二维码" '
                f'data-qr-generation="{generation}" /></section>'
            )
            if state["scenario"] == "multi_origin":
                qr += (
                    f'<img id="foreign-login-qr" src="{escape(self.alternate_origin)}/qr/foreign.svg" '
                    f'alt="未批准来源二维码" data-qr-origin="{escape(self.alternate_origin)}" />'
                )
        elif state["qr_status"] == "expired":
            qr = '<section id="qr-region" data-qr-status="expired"><p>二维码已过期，请刷新。</p></section>'
        sensitive = "true" if state["sensitive_mode"] else "false"
        otp = (
            '<label>一次性验证码<input id="otp" name="otp" type="text" '
            'data-sensitive-field="otp" autocomplete="off"></label>'
            if state["otp_visible"]
            else ""
        )
        token = (
            '<label>访问令牌<input id="token" name="token" type="text" '
            'data-sensitive-field="token" autocomplete="off"></label>'
            if state["token_visible"]
            else ""
        )
        challenge = (
            '<section id="challenge" data-challenge-state="required"><h2>需要人工挑战</h2>'
            '<p>请在同一受控会话中完成挑战。</p>'
            '<input id="challenge-code" name="challenge_code" type="text" '
            'data-sensitive-field="challenge" autocomplete="off"></section>'
            if state["challenge_required"]
            else ""
        )
        auth = (
            f'<section id="authenticated" data-authenticated="true" '
            f'data-authenticated-identity="{escape(state["presented_identity"])}">'
            '<h2>认证状态</h2><p>受控认证成功证据已出现。</p></section>'
            if state["authenticated"]
            else ""
        )
        frames = ""
        if state["scenario"] in {"navigation", "multi_origin"}:
            document = state["document_generation"]
            view = state["view_generation"]
            frames = (
                f'<section id="frame-region"><iframe id="same-origin-frame" '
                f'src="/frames/login?document={document}&view={view}" title="同 origin frame"></iframe>'
                f'<iframe id="cross-origin-frame" src="{escape(self.alternate_origin)}/frames/cross-origin?document={document}&view={view}" '
                'title="跨 origin frame"></iframe></section>'
            )
        return f'''<!doctype html>
<html lang="zh-CN" data-scenario="{escape(state["scenario"])}"
 data-flow-state="{escape(state["flow_state"])}" data-sensitive-mode="{sensitive}"
 data-document-generation="{state["document_generation"]}"
 data-view-generation="{state["view_generation"]}" data-frame-label="{escape(state["frame_label"])}">
<head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>受控账号登录站点</title></head>
<body><main id="login-page"><h1>受控登录测试站点</h1>
<p id="identity-evidence" data-identity="{escape(state["presented_identity"])}"
 data-identity-status="{escape(state["identity_status"])}">身份证据：{escape(state["presented_identity"])}</p>
{qr}{challenge}{frames}
<form id="login-form" method="post" action="/login/submit" data-sensitive-form="{sensitive}">
<label>账号<input id="username" name="username" type="text" autocomplete="off"
 data-identity-field="true"></label>
<label>密码<input id="password" name="password" type="{state["password_input_type"]}"
 autocomplete="off" data-sensitive-field="password"></label>{otp}{token}
<button id="submit-login" type="submit">提交到原生表单</button></form>
{auth}
<p id="generation-evidence" data-document-generation="{state["document_generation"]}"
 data-view-generation="{state["view_generation"]}"
 data-frame-label="{escape(state["frame_label"])}">文档代际标签 {state["document_generation"]}，视图代际标签 {state["view_generation"]}</p>
</main></body></html>'''


def create_login_site(*, host: str = _LOOPBACK_HOST, port: int = 0) -> ControlledLoginSite:
    """创建尚未启动的受控登录站点。"""
    return ControlledLoginSite(host=host, port=port)


@contextmanager
def running_login_site(*, host: str = _LOOPBACK_HOST, port: int = 0) -> Iterator[ControlledLoginSite]:
    """启动站点并在退出时可靠关闭。"""
    site = create_login_site(host=host, port=port)
    with site:
        yield site


def _main() -> int:
    parser = argparse.ArgumentParser(description="启动受控账号登录测试站点")
    parser.add_argument("--port", default=0, type=int)
    args = parser.parse_args()
    site = create_login_site(port=args.port)
    try:
        site.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
