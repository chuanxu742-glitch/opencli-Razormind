"""Preserved legacy browser mappings remain readable after account cutover."""

import pytest

from backend.models.browser import BrowserBinding
from backend.services import browser_service


@pytest.mark.asyncio
async def test_list_bindings_empty(db_session):
    assert await browser_service.list_bindings(db_session) == []


@pytest.mark.asyncio
async def test_read_preserved_bindings_with_same_site(db_session):
    rows = [
        BrowserBinding(
            browser_endpoint="http://chrome:9222", site="example.com", notes="preserve me"
        ),
        BrowserBinding(browser_endpoint="http://chrome-2:9222", site="example.com", notes=None),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    listed = await browser_service.list_bindings(db_session)
    assert {row.id for row in listed} == {row.id for row in rows}
    for original in rows:
        result = await browser_service.get_binding(db_session, original.id)
        assert result is not None
        assert (result.browser_endpoint, result.site, result.notes) == (
            original.browser_endpoint,
            original.site,
            original.notes,
        )


@pytest.mark.asyncio
async def test_get_binding_not_found(db_session):
    assert await browser_service.get_binding(db_session, "nonexistent-id") is None
