"""Exact CDP page/frame ownership and capture-disabled portal recording."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.browser_portal_contract import _requested_frame
from backend.skills.page import SkillPage, _page_for_cdp_target
from backend.skills.record import RecordSession


def browser_with_targets(ids):
    pages = [SimpleNamespace(target_id=target_id) for target_id in ids]
    channels = []

    async def channel_for(page):
        channel = SimpleNamespace(
            send=AsyncMock(return_value={"targetInfo": {"targetId": page.target_id}}),
            detach=AsyncMock(),
        )
        channels.append(channel)
        return channel

    return (
        SimpleNamespace(contexts=[SimpleNamespace(pages=pages, new_cdp_session=channel_for)]),
        pages,
        channels,
    )


@pytest.mark.asyncio
async def test_exact_target_selects_second_page_and_detaches_inspection_channels():
    browser, pages, channels = browser_with_targets(["blank-page", "authorized-page"])
    assert await _page_for_cdp_target(browser, "authorized-page") is pages[1]
    for channel in channels:
        channel.detach.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [["unrelated"], ["authorized", "authorized"]])
async def test_missing_or_ambiguous_target_has_no_first_page_fallback(ids):
    browser, _, _ = browser_with_targets(ids)
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        await _page_for_cdp_target(browser, "authorized")


def make_record():
    class Frame:
        def __init__(self, name=""):
            self.name = name
            self.evaluate = AsyncMock(return_value=True)

    main = Frame()
    child = Frame("0")
    page = SimpleNamespace(
        main_frame=main,
        frames=[main, child],
        on=lambda *args: None,
        add_init_script=AsyncMock(),
        is_closed=lambda: False,
    )
    record = RecordSession(
        session_id="portal-record",
        domain="test",
        capability="account-login",
        page=SkillPage(None, None, page),
    )
    return record, page, main, child


def test_only_verified_page_can_map_extension_zero_to_main_frame():
    record, page, main, child = make_record()
    with pytest.raises(ValueError, match="not unique"):
        _requested_frame(record, 0)
    record._portal_verified_page = page
    assert _requested_frame(record, 0) is main
    assert record.binding_identity(0)[1] is main
    # A page swap cannot inherit the attestation of the old page.
    record.page.page = SimpleNamespace(main_frame=main, frames=[main])
    with pytest.raises(ValueError):
        _requested_frame(record, 0)


@pytest.mark.asyncio
async def test_account_record_starts_sensitive_without_exposing_event_binding():
    record, page, main, child = make_record()
    await record.start()
    assert record.sensitive is True
    assert record.is_common_listener_installed() is False
    assert record.is_common_listener_revoked() is True
    assert not record.steps
    for frame in [main, child]:
        assert frame.evaluate.await_args_list[0].args[1]["sensitive"] is True
        assert frame.evaluate.await_args_list[1].args[1]["enabled"] is True
    assert '"sensitive":true' in page.add_init_script.await_args.args[0]


@pytest.mark.asyncio
async def test_verified_sensitive_registration_supports_repeated_prepare():
    from backend.schemas.browser_account import SensitiveSessionBindingV1
    from backend.services.browser_portal_contract import (
        freeze_portal_record_session,
        register_portal_record_session,
    )

    record, page, _, _ = make_record()
    record.session_id = "portal-repeat-test"
    await record.start()
    record._portal_verified_page = page
    binding = SensitiveSessionBindingV1(
        account_ref={"workspace_id": "w", "account_id": "a"},
        session_id="s",
        epoch=1,
        target={"tab_id": 7, "frame_id": 0, "document_id": "d", "origin": "https://example.com"},
        view_generation=1,
        record_session_id=record.session_id,
    )
    register_portal_record_session(binding, record)
    first = await freeze_portal_record_session(binding, record)
    register_portal_record_session(binding, record)
    assert await freeze_portal_record_session(binding, record) == first


@pytest.mark.asyncio
async def test_prepare_replaces_initial_record_then_reuses_exact_document(monkeypatch):
    from backend import agent_runtime_dispatch as dispatch

    @dataclass
    class Registration:
        record_session: object
        agent_url: str = "https://node.test"

    old = SimpleNamespace(page=SimpleNamespace(aclose=AsyncMock()), stop=AsyncMock())
    replacement, _, main, _ = make_record()
    registration = Registration(old)
    current = [registration]
    registry = SimpleNamespace(
        resolve=lambda **kwargs: current[0],
        revoke=lambda **kwargs: current.__setitem__(0, None),
        register=lambda value: current.__setitem__(0, value),
    )
    monkeypatch.setattr(dispatch, "session_portal_registry", lambda: registry)
    mapping = AsyncMock(return_value=("ws://cdp/devtools/page/exact", "exact"))
    monkeypatch.setattr(dispatch, "_authorized_cdp_page", mapping)
    start = AsyncMock(return_value=replacement)
    monkeypatch.setattr("backend.skills.record.start_recording", start)
    session = SimpleNamespace(
        session_id="s",
        node_id="n",
        node_boot_id="b",
        epoch=1,
        target=SimpleNamespace(frame_id=0, document_id="d", origin="https://example.com"),
        view_generation=1,
    )
    updated = await dispatch._bind_portal_record("http://cdp", registration, session)
    assert updated.record_session is replacement
    old.stop.assert_awaited_once()
    old.page.aclose.assert_awaited_once()
    assert start.await_args.kwargs["target_id"] == "exact"
    assert await dispatch._bind_portal_record("http://cdp", updated, session) is updated
    start.assert_awaited_once()
    assert mapping.await_count == 3
