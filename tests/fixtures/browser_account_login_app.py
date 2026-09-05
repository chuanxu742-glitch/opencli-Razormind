"""用于账号登录协议测试的受控本地站点。

这是一个真实的 HTTP 页面，不代表任何真实平台，也不实现业务账号服务。站点只绑定
回环地址；场景和事件通过显式控制接口驱动，表单请求体会被读取并丢弃，永不记录。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SCENARIO_ALIASES = {
    "qr_login": "qr",
    "qr_refresh": "qr",
    "qr_expired": "expired",
    "password": "form",
    "password_form": "form",
    "wrong-account": "wrong_identity",
    "wrong-account-identity": "wrong_identity",
    "password-to-text": "password_to_text",
    "otp-token": "otp_token",
    "token": "otp_token",
    "frame_document": "navigation",
    "cross_origin": "multi_origin",
    "multi_qr": "multi_origin",
}
_ALLOWED_SCENARIOS = {
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
    document_generation: int = 1
    view_generation: int = 1
    frame_id: str = "login-frame-1"
    challenge_required: bool = False
    sensitive_mode: bool = False
    password_input_type: str = "password"
    otp_visible: bool = False
    token_visible: bool = False
    submitted_count: int = 0
    events: list[str] = field(default_factory=list)


class ControlledLoginSite:
    """可在测试中启动和控制的本地登录站点。"""

    expected_identity = "fixture-account-alice"

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("受控登录站点只允许绑定回环地址")
        if port < 0 or port > 65535:
            raise ValueError("端口必须在 0 到 65535 之间")
        self.host = host
        self.requested_port = port
        self._lock = threading.RLock()
        self._state = _LoginState(expected_identity=self.expected_identity)
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
        host, port = address
        url_host = f"[{host}]" if ":" in host else host
        return f"http://{url_host}:{port}"

    @property
    def alternate_origin(self) -> str:
        """返回同端口的另一回环 origin，用于跨 origin 候选测试。"""
        address = self.server_address
        if address is None:
            raise RuntimeError("站点尚未启动")
        host, port = address
        alternate = "localhost" if host == "127.0.0.1" else "127.0.0.1"
        return f"http://{alternate}:{port}"

    @property
    def url(self) -> str:
        """兼容测试中常用的站点 URL 属性。"""
        return self.base_url

    def start(self) -> ControlledLoginSite:
        if self._server is not None:
            return self
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.requested_port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="controlled-login-fixture",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    close = stop

    def __enter__(self) -> ControlledLoginSite:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            trusted = (
                state.authenticated
                and state.identity_status == "valid"
                and not state.challenge_required
            )
            return {
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
                "frame_id": state.frame_id,
                "challenge_required": state.challenge_required,
                "sensitive_mode": state.sensitive_mode,
                "password_input_type": state.password_input_type,
                "otp_visible": state.otp_visible,
                "token_visible": state.token_visible,
                "submitted_count": state.submitted_count,
                "events": list(state.events),
            }

    def set_scenario(self, scenario: str) -> dict[str, Any]:
        """选择场景并重置其代际；输入只接受固定场景名。"""
        normalized = _SCENARIO_ALIASES.get(scenario, scenario)
        if normalized not in _ALLOWED_SCENARIOS:
            raise ValueError(f"未知登录场景: {scenario}")
        with self._lock:
            state = _LoginState(scenario=normalized, expected_identity=self.expected_identity)
            state.sensitive_mode = normalized in {"sensitive", "password_to_text", "otp_token"}
            state.password_input_type = "text" if normalized == "password_to_text" else "password"
            state.otp_visible = normalized == "otp_token"
            state.token_visible = normalized == "otp_token"
            if normalized == "expired":
                state.flow_state = "expired"
                state.qr_status = "expired"
            elif normalized == "challenge":
                state.flow_state = "challenge"
                state.qr_status = "hidden"
                state.challenge_required = True
            elif normalized == "wrong_identity":
                state.flow_state = "verifying"
                state.qr_status = "hidden"
                state.authenticated = True
                state.presented_identity = "fixture-account-bob"
                state.identity_status = "mismatch"
            elif normalized in {"form", "sensitive", "password_to_text", "otp_token"}:
                state.qr_status = "hidden"
            self._state = state
            self._record_event_locked(f"scenario:{normalized}")
            return self.state()

    def advance(self, event: str, *, identity: str | None = None) -> dict[str, Any]:
        """推进一个白名单事件；事件参数不接收秘密。"""
        event = event.strip().lower().replace("-", "_")
        with self._lock:
            state = self._state
            if event in {"expire", "expire_qr", "qr_expire"}:
                state.qr_status = "expired"
                state.flow_state = "expired"
                state.authenticated = False
            elif event in {"refresh", "refresh_qr", "new_qr"}:
                state.qr_generation += 1
                state.view_generation += 1
                state.qr_status = "presenting"
                state.flow_state = "presenting"
                state.authenticated = False
                state.identity_status = "unknown"
            elif event in {"navigate", "new_document", "document_change", "new_frame"}:
                state.document_generation += 1
                state.view_generation += 1
                state.frame_id = f"login-frame-{state.document_generation}"
                state.flow_state = "verifying"
                state.identity_status = "unknown"
            elif event in {"challenge", "require_challenge"}:
                state.challenge_required = True
                state.flow_state = "challenge"
                state.qr_status = "hidden"
            elif event in {"resolve_challenge", "challenge_resolved"}:
                state.challenge_required = False
                state.flow_state = "verifying"
                state.identity_status = "unknown"
            elif event in {"wrong_identity", "identity_mismatch", "fake_success"}:
                state.presented_identity = "fixture-account-bob"
                state.authenticated = True
                state.identity_status = "mismatch"
                state.flow_state = "verifying"
                state.qr_status = "hidden"
            elif event in {"set_identity", "identity"}:
                if identity is None or not _SAFE_IDENTITY.fullmatch(identity):
                    raise ValueError("identity 只能包含有限 ASCII 标识符")
                state.presented_identity = identity
                state.identity_status = (
                    "valid" if identity == state.expected_identity else "mismatch"
                )
            elif event in {"authenticate", "authenticated", "success"}:
                state.qr_status = "hidden"
                state.authenticated = True
                state.flow_state = "authenticated"
                state.identity_status = (
                    "valid" if state.presented_identity == state.expected_identity else "mismatch"
                )
            elif event in {"sensitive", "enter_sensitive_mode"}:
                state.sensitive_mode = True
                state.flow_state = "presenting"
            elif event in {"password_to_text", "reveal_password_type"}:
                state.sensitive_mode = True
                state.password_input_type = "text"
            elif event in {"otp", "token", "otp_token"}:
                state.sensitive_mode = True
                state.otp_visible = True
                state.token_visible = True
            elif event in {"reset"}:
                self._state = _LoginState(expected_identity=self.expected_identity)
                state = self._state
            else:
                raise ValueError(f"未知控制事件: {event}")
            self._record_event_locked(event)
            return self.state()

    def _record_event_locked(self, event: str) -> None:
        self._state.events.append(event)
        del self._state.events[:-50]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                # 禁止标准库把 URL、表单或控制请求写入 stderr。
                return

            def _send(self, body: bytes, *, status: int = 200, content_type: str = "text/html") -> None:
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, value: Any, *, status: int = 200) -> None:
                self._send(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    status=status,
                    content_type="application/json",
                )

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("控制请求必须是 JSON 对象")
                return value

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == "/__control__/state":
                    self._json(site.state())
                    return
                if path == "/__control__/scenario":
                    name = parse_qs(parsed.query).get("name", [""])[0]
                    try:
                        self._json(site.set_scenario(name))
                    except ValueError as exc:
                        self._json({"error": str(exc)}, status=400)
                    return
                if path == "/identity":
                    state = site.state()
                    self._json(
                        {
                            "identity": state["presented_identity"],
                            "identity_status": state["identity_status"],
                            "authenticated": state["authenticated"],
                            "document_generation": state["document_generation"],
                            "view_generation": state["view_generation"],
                        }
                    )
                    return
                if path == "/auth-status":
                    state = site.state()
                    self._json(
                        {
                            "authenticated": state["authenticated"],
                            "trusted": state["trusted"],
                            "identity": state["presented_identity"],
                            "identity_status": state["identity_status"],
                            "evidence": {
                                "identity_endpoint": "/identity",
                                "state_endpoint": "/auth-status",
                                "document_generation": state["document_generation"],
                                "view_generation": state["view_generation"],
                                "frame_id": state["frame_id"],
                            },
                        }
                    )
                    return
                if path == "/qr/current":
                    state = site.state()
                    current = state["qr_status"] == "presenting"
                    self._json(
                        {
                            "status": state["qr_status"],
                            "generation": state["qr_generation"],
                            "origin": site.base_url,
                            "image_url": (
                                f"{site.base_url}/qr/{state['qr_generation']}.svg" if current else None
                            ),
                        }
                    )
                    return
                qr_match = re.fullmatch(r"/qr/(\d+)\.svg", path)
                if qr_match:
                    generation = int(qr_match.group(1))
                    state = site.state()
                    if generation != state["qr_generation"] or state["qr_status"] != "presenting":
                        self._json({"error": "qr_expired", "generation": generation}, status=410)
                    else:
                        self._send(site._qr_svg(generation), content_type="image/svg+xml")
                    return
                if path == "/foreign/qr.svg":
                    # 多 origin 候选用于验证规则不会接受未批准来源。
                    self._send(site._qr_svg(site.state()["qr_generation"], foreign=True), content_type="image/svg+xml")
                    return
                if path in {"/", "/login", "/auth/callback", "/sensitive"}:
                    self._send(site._render_page(path).encode("utf-8"))
                    return
                self._json({"error": "not_found"}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = parsed.path
                try:
                    if path == "/__control__/scenario":
                        payload = self._read_json()
                        self._json(site.set_scenario(str(payload.get("scenario", ""))))
                        return
                    if path == "/__control__/advance":
                        payload = self._read_json()
                        identity = payload.get("identity")
                        self._json(site.advance(str(payload.get("event", "")), identity=identity))
                        return
                    if path in {"/login/submit", "/form/submit"}:
                        # 读取并丢弃请求体，不解析、不保存、不回显任何字段。
                        length = int(self.headers.get("Content-Length", "0"))
                        if length:
                            self.rfile.read(length)
                        with site._lock:
                            site._state.submitted_count += 1
                            site._record_event_locked("form_submitted")
                        # 原生 POST 返回同源页面，浏览器不会把字段值带入响应。
                        self._send(site._render_page("/login").encode("utf-8"))
                        return
                    self._json({"error": "not_found"}, status=404)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, status=400)

        return Handler

    def _qr_svg(self, generation: int, *, foreign: bool = False) -> bytes:
        label = "foreign" if foreign else "approved"
        # 这是确定性的占位图，不编码账号、会话或凭据。
        blocks = "".join(
            f'<rect x="{(index * 17) % 180}" y="{(index * 29) % 180}" width="12" height="12" />'
            for index in range(1, 20)
            if (index + generation) % 3
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
            f'data-qr-kind="{label}" data-qr-generation="{generation}" role="img" '
            'aria-label="受控二维码"><rect width="200" height="200" fill="white" />'
            f'<g fill="black">{blocks}</g></svg>'
        ).encode("utf-8")

    def _render_page(self, path: str) -> str:
        state = self.state()
        qr = ""
        if state["qr_status"] == "presenting":
            generation = state["qr_generation"]
            qr = (
                f'<section id="qr-region" data-qr-origin="{escape(self.base_url)}" '
                f'data-qr-generation="{generation}"><h2>扫码登录</h2>'
                f'<img id="login-qr" src="/qr/{generation}.svg" alt="受控二维码" '
                f'data-qr-generation="{generation}" /></section>'
            )
        elif state["qr_status"] == "expired":
            qr = '<section id="qr-region" data-qr-status="expired"><p>二维码已过期，请刷新。</p></section>'
        if state["scenario"] == "multi_origin" and state["qr_status"] == "presenting":
            qr += (
                f'<img id="foreign-login-qr" src="{escape(self.alternate_origin)}/foreign/qr.svg" '
                f'alt="未批准来源二维码" data-qr-origin="{escape(self.alternate_origin)}" />'
            )
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
        return f'''<!doctype html>
<html lang="zh-CN" data-scenario="{escape(state["scenario"])}"
 data-flow-state="{escape(state["flow_state"])}" data-sensitive-mode="{sensitive}"
 data-document-generation="{state["document_generation"]}"
 data-view-generation="{state["view_generation"]}" data-frame-id="{escape(state["frame_id"])}">
<head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>受控账号登录站点</title></head>
<body><main id="login-page"><h1>受控登录测试站点</h1>
<p id="identity-evidence" data-identity="{escape(state["presented_identity"])}"
 data-identity-status="{escape(state["identity_status"])}">身份证据：{escape(state["presented_identity"])}</p>
{qr}{challenge}
<form id="login-form" method="post" action="/login/submit" data-sensitive-form="{sensitive}">
<label>账号<input id="username" name="username" type="text" autocomplete="off"
 data-identity-field="true"></label>
<label>密码<input id="password" name="password" type="{state["password_input_type"]}"
 autocomplete="off" data-sensitive-field="password"></label>{otp}{token}
<button id="submit-login" type="submit">提交到原生表单</button></form>
{auth}
<p id="generation-evidence" data-document-generation="{state["document_generation"]}"
 data-view-generation="{state["view_generation"]}" data-frame-id="{escape(state["frame_id"])}">
文档代际 {state["document_generation"]}，视图代际 {state["view_generation"]}</p>
</main></body></html>'''

def create_login_site(*, host: str = "127.0.0.1", port: int = 0) -> ControlledLoginSite:
    """创建尚未启动的受控登录站点。"""
    return ControlledLoginSite(host=host, port=port)


@contextmanager
def running_login_site(*, host: str = "127.0.0.1", port: int = 0) -> Iterator[ControlledLoginSite]:
    """启动站点并在退出时可靠关闭。"""
    site = create_login_site(host=host, port=port)
    with site:
        yield site


# 这些别名保持测试夹具调用简洁，同时不引入第二套行为。
login_site = running_login_site
browser_account_login_site = running_login_site


def _main() -> int:
    parser = argparse.ArgumentParser(description="启动受控账号登录测试站点")
    parser.add_argument("--host", default="127.0.0.1", choices=sorted(_LOOPBACK_HOSTS))
    parser.add_argument("--port", default=0, type=int)
    args = parser.parse_args()
    site = create_login_site(host=args.host, port=args.port).start()
    print(f"LOGIN_FIXTURE_URL={site.base_url}", flush=True)
    try:
        assert site._server is not None
        site._server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        site.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
