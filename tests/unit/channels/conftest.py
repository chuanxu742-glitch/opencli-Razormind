from __future__ import annotations

import httpx
import pytest

from backend.channels import rss_channel


@pytest.fixture
def mock_rss_public_client(monkeypatch):
    """Keep mocked RSS fetches independent from host DNS policy."""

    async def guarded_client(feed_url: str, **client_kwargs):
        return rss_channel.httpx.AsyncClient(**client_kwargs), feed_url

    monkeypatch.setattr(rss_channel, "guarded_async_client", guarded_client)


@pytest.fixture(autouse=True)
def allow_rss_fixture_hosts(monkeypatch, request):
    """Keep RSS parser/request tests independent from the machine's DNS.

    The production RSS channel still validates and resolves every URL. These tests replace the
    network client and therefore should exercise parsing/error handling, not live DNS policy.
    """
    if "test_rss_" not in request.node.nodeid:
        return

    async def _use_mocked_client(url: str, **kwargs: object):
        return httpx.AsyncClient(**kwargs), url

    monkeypatch.setattr("backend.channels.rss_channel.guarded_async_client", _use_mocked_client)

    async def _allow_fixture_url(url: str, **_kwargs: object) -> str:
        return url

    monkeypatch.setattr("backend.channels.rss_channel.avalidate_public_url", _allow_fixture_url)
