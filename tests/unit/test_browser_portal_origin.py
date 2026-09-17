from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api.v1.browser_accounts import _require_same_origin


@pytest.mark.parametrize(
    "configured,origin,allowed",
    [
        ("", "http://api.test:8031", True),
        ("", "http://ui.test:3000", False),
        ("http://ui.test:3000", "http://ui.test:3000", True),
        ("http://ui.test:3000", "http://evil.test:3000", False),
        ("http://ui.test:3000", "http://api.test:8031", False),
    ],
)
def test_portal_origin_uses_only_explicit_configuration(monkeypatch, configured, origin, allowed):
    monkeypatch.setattr(
        "backend.config.get_settings",
        lambda: SimpleNamespace(
            browser_portal_public_origin=configured,
        ),
    )
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/",
            "query_string": b"",
            "server": ("api.test", 8031),
            "headers": [
                (b"host", b"api.test:8031"),
                (b"origin", origin.encode()),
                (b"x-forwarded-host", b"evil.test:3000"),
            ],
        }
    )
    if allowed:
        _require_same_origin(request)
    else:
        with pytest.raises(HTTPException) as error:
            _require_same_origin(request)
        assert error.value.status_code == 403
