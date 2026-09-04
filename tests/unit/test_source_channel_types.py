"""DataSource channel types remain compatible with registered channels."""

import pytest

from backend.schemas.source import DataSourceCreate
from backend.workflow.node_registry import WORKFLOW_CATALOG_IDS


@pytest.mark.parametrize(
    "channel_type",
    ["feishu_table", "crawl4ai", "browser_act", "kuaishou_search"],
)
def test_existing_and_new_channel_types_are_accepted(channel_type):
    source = DataSourceCreate(
        name=f"source-{channel_type}",
        channel_type=channel_type,
    )

    assert source.channel_type == channel_type

def test_existing_workflow_source_catalog_ids_are_preserved():
    assert "intelligence.source.doubao-research" in WORKFLOW_CATALOG_IDS
    assert "intelligence.source.feishu-table" in WORKFLOW_CATALOG_IDS
