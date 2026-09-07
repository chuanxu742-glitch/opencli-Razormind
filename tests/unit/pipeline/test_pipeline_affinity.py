"""Session affinity never infers account identity from legacy site mappings."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.channels.base import ChannelResult
from backend.channels.opencli_channel import OpenCLIChannel
from backend.channels.skill_channel import SkillChannel
from backend.pipeline.sinks import SinkResult

pytestmark = pytest.mark.usefixtures("anonymous_account_resolution")


def _session_cm():
    sess = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _ok_sink():
    sink = MagicMock()
    sink.write_batch = AsyncMock(return_value=SinkResult(records=[]))
    return sink


def test_opencli_and_skill_declare_session_affinity():
    assert OpenCLIChannel().capabilities.session_affinity is True
    assert SkillChannel().capabilities.session_affinity is True


@pytest.mark.asyncio
async def test_pipeline_does_not_infer_account_from_legacy_site(db_session):
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask
    from backend.pipeline.pipeline import run_pipeline

    source = DataSource(name="O", channel_type="opencli", channel_config={"site": "x.com"})
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    from backend.models.browser import BrowserBinding

    db_session.add(BrowserBinding(browser_endpoint="ws://chrome:9222", site="x.com"))
    await db_session.flush()
    captured = {}

    async def fake_collect(src, params):
        captured["params"] = params
        return ChannelResult.ok([{"title": "x"}])

    with (
        patch("backend.pipeline.collector.collect", new=fake_collect),
        patch("backend.database.AsyncSessionLocal", return_value=_session_cm()),
    ):
        await run_pipeline(
            task.id, source, enable_ai=False, enable_notifications=False, sink=_ok_sink()
        )

    assert "chrome_endpoint" not in captured["params"]


@pytest.mark.asyncio
async def test_pipeline_skips_binding_for_non_affinity_channel(db_session):
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask
    from backend.pipeline.pipeline import run_pipeline

    source = DataSource(
        name="R",
        channel_type="rss",
        channel_config={"feed_url": "https://x/f", "site": "x.com"},
    )
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    captured = {}

    async def fake_collect(src, params):
        captured["params"] = params
        return ChannelResult.ok([{"title": "x"}])

    with (
        patch("backend.pipeline.collector.collect", new=fake_collect),
        patch("backend.database.AsyncSessionLocal", return_value=_session_cm()),
    ):
        await run_pipeline(
            task.id, source, enable_ai=False, enable_notifications=False, sink=_ok_sink()
        )

    assert "chrome_endpoint" not in captured["params"]
