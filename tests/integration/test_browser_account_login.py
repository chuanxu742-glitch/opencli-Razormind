from __future__ import annotations

import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from tests.fixtures.browser_account_login_app import running_login_site


class _FixtureClient:
    """Small real-HTTP client preserving one fixture browser cookie context."""

    def __init__(self) -> None:
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, method: str, url: str, body: object | None = None):
        encoded = None
        headers = {"Accept": "application/json"}
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self._opener.open(Request(url, data=encoded, headers=headers, method=method), timeout=3)

    def json(self, method: str, url: str, body: object | None = None) -> dict:
        with self.request(method, url, body) as response:
            return json.load(response)

    def text(self, method: str, url: str, body: object | None = None) -> str:
        with self.request(method, url, body) as response:
            return response.read().decode("utf-8")

    def status(self, method: str, url: str, body: object | None = None) -> int:
        with self.request(method, url, body) as response:
            return response.status


def _state(client: _FixtureClient, base_url: str) -> dict:
    return client.json("GET", f"{base_url}/__control__/state")


def _advance(client: _FixtureClient, base_url: str, session_id: str, event: str) -> dict:
    return client.json(
        "POST",
        f"{base_url}/__control__/advance",
        {"session_id": session_id, "event": event},
    )


def test_qr_refresh_invalidates_old_generation_and_current_confirmation_succeeds():
    with running_login_site() as site:
        client = _FixtureClient()
        initial = _state(client, site.base_url)
        current = client.json("GET", f"{site.base_url}/qr/current")
        old_generation = current["generation"]
        old_confirmation_url = current["confirmation_url"]

        assert current["status"] == "presenting"
        assert current["origin"] == site.base_url
        assert current["image_url"].startswith(f"{site.base_url}/qr/")
        assert client.status("GET", current["image_url"]) == 200

        refreshed = _advance(client, site.base_url, initial["session_id"], "refresh_qr")
        assert refreshed["view_generation"] == initial["view_generation"] + 1
        assert refreshed["qr_generation"] == old_generation + 1

        with pytest.raises(HTTPError) as stale_image:
            client.request("GET", f"{site.base_url}/qr/{old_generation}.svg")
        assert stale_image.value.code == 410
        with pytest.raises(HTTPError) as stale_confirmation:
            client.request("GET", old_confirmation_url)
        assert stale_confirmation.value.code == 410

        next_qr = client.json("GET", f"{site.base_url}/qr/current")
        assert next_qr["generation"] == refreshed["qr_generation"]
        assert client.status("GET", next_qr["image_url"]) == 200
        with client.request("GET", next_qr["confirmation_url"]) as response:
            assert response.status == 200
            confirmed = json.load(response)
        assert confirmed["confirmed"] is True
        assert _state(client, site.base_url)["trusted"] is True


def test_cookie_contexts_are_isolated_and_wrong_identity_is_not_trusted():
    with running_login_site() as site:
        first = _FixtureClient()
        second = _FixtureClient()
        first_state = _state(first, site.base_url)
        second_state = _state(second, site.base_url)
        assert first_state["session_id"] != second_state["session_id"]

        mismatch = first.json(
            "POST",
            f"{site.base_url}/__control__/scenario",
            {"session_id": first_state["session_id"], "scenario": "wrong_identity"},
        )
        assert mismatch["identity_status"] == "mismatch"
        assert mismatch["authenticated"] is True
        assert mismatch["trusted"] is False
        assert _state(second, site.base_url)["identity_status"] == "unknown"
        assert _state(second, site.base_url)["authenticated"] is False


def test_challenge_resolves_inside_same_session_before_trusted_authentication():
    with running_login_site() as site:
        client = _FixtureClient()
        initial = _state(client, site.base_url)
        challenge = client.json(
            "POST",
            f"{site.base_url}/__control__/scenario",
            {"session_id": initial["session_id"], "scenario": "challenge"},
        )
        assert challenge["flow_state"] == "challenge"
        assert challenge["challenge_required"] is True
        assert challenge["trusted"] is False

        unresolved = _advance(client, site.base_url, initial["session_id"], "resolve_challenge")
        assert unresolved["flow_state"] == "verifying"
        assert unresolved["trusted"] is False
        authenticated = _advance(client, site.base_url, initial["session_id"], "authenticate")
        assert authenticated["flow_state"] == "authenticated"
        assert authenticated["identity_status"] == "valid"
        assert authenticated["trusted"] is True


@pytest.mark.parametrize(
    ("scenario", "expected_type", "expected_otp", "expected_token"),
    [
        ("sensitive", "password", False, False),
        ("password_to_text", "text", False, False),
        ("otp_token", "password", True, True),
    ],
)
def test_sensitive_form_variants_never_echo_posted_secret(
    scenario: str, expected_type: str, expected_otp: bool, expected_token: bool
):
    secret = "fixture-only-secret-that-must-not-echo"
    with running_login_site() as site:
        client = _FixtureClient()
        initial = _state(client, site.base_url)
        state = client.json(
            "POST",
            f"{site.base_url}/__control__/scenario",
            {"session_id": initial["session_id"], "scenario": scenario},
        )
        assert state["sensitive_mode"] is True
        assert state["password_input_type"] == expected_type
        assert state["otp_visible"] is expected_otp
        assert state["token_visible"] is expected_token

        form_request = Request(
            f"{site.base_url}/login/submit",
            data=f"password={secret}".encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with client._opener.open(form_request, timeout=3) as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert secret not in body
        assert _state(client, site.base_url)["submitted_count"] == 1


def test_multi_origin_page_exposes_foreign_qr_as_distinct_unapproved_origin():
    with running_login_site() as site:
        client = _FixtureClient()
        initial = _state(client, site.base_url)
        client.json(
            "POST",
            f"{site.base_url}/__control__/scenario",
            {"session_id": initial["session_id"], "scenario": "multi_origin"},
        )
        page = client.text("GET", f"{site.base_url}/login")
        assert f'data-qr-origin="{site.base_url}"' in page
        assert f'data-qr-origin="{site.alternate_origin}"' in page
        assert 'id="foreign-login-qr"' in page


@pytest.mark.live
@pytest.mark.asyncio
async def test_controlled_login_fixture_runs_real_browser_qr_business_flow():
    """Drive the fixture through Chromium, not a mocked page or transport."""

    from playwright.async_api import async_playwright

    with running_login_site() as site:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(f"{site.base_url}/login", wait_until="domcontentloaded")
            assert await page.locator("#login-qr").is_visible()

            initial = await page.evaluate(
                """async () => (await fetch('/__control__/state')).json()"""
            )
            qr = await page.evaluate(
                """async () => (await fetch('/qr/current')).json()"""
            )
            old_image = qr["image_url"]
            old_generation = qr["generation"]
            assert initial["flow_state"] == "presenting"
            assert qr["status"] == "presenting"
            assert old_image.startswith(site.base_url)

            refreshed = await page.evaluate(
                """async (sessionId) => (await fetch('/__control__/advance', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: sessionId, event: 'refresh_qr'})
                })).json()""",
                initial["session_id"],
            )
            assert refreshed["qr_generation"] == old_generation + 1
            await page.reload(wait_until="domcontentloaded")
            assert await page.evaluate(
                """async (url) => (await fetch(url)).status""", old_image
            ) == 410

            current = await page.evaluate(
                """async () => (await fetch('/qr/current')).json()"""
            )
            confirmed = await page.evaluate(
                """async (url) => (await fetch(url)).json()""",
                current["confirmation_url"],
            )
            assert confirmed["confirmed"] is True
            await page.reload(wait_until="domcontentloaded")
            assert (
                await page.locator("#authenticated").get_attribute("data-authenticated")
                == "true"
            )
        finally:
            await browser.close()
            await playwright.stop()
