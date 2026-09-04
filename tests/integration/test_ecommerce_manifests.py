
"""Executable contracts for the vendored e-commerce BrowserAct packs."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.browser_act.cli import BrowserActResult
from backend.browser_act_packs.catalog import PackCatalog
from backend.browser_act_packs.manifest import load_manifest
from backend.channels.browser_act_channel import BrowserActChannel

EcommerceChannel = "ecommerce/{}"
PLATFORM_ADAPTERS = [
    "jd",
    "pinduoduo",
    "douyin",
    "kuaishou",
    "xiaohongshu",
    "vipshop",
    "suning",
    "1688",
    "dewu",
    "auto",
]


def _run_side_effect(payload: str):
    async def _run(args, *, timeout=None, env=None):
        return BrowserActResult(returncode=0, stdout=payload, stderr="")

    return _run


@pytest.mark.parametrize(
    "capability",
    [
        "taobao-keyword-search",
        "taobao-product-detail",
        "taobao-product-reviews",
        "taobao-shop-catalog",
        "goofish-search-list",
        "goofish-item-detail",
        "ecommerce-listing",
        "ecommerce-product-detail",
        "ecommerce-reviews",
        "ecommerce-seller-info",
        "ecommerce-platform",
    ],
)
def test_ecommerce_manifest_loads(capability):
    path = PackCatalog().root / "ecommerce" / capability / "channel.manifest.json"
    manifest = load_manifest(path)
    assert manifest.domain == "ecommerce"
    assert manifest.capability == capability


def test_platform_adapter_manifest_lists_supported_platforms():
    manifest = load_manifest(
        PackCatalog().root
        / "ecommerce"
        / "ecommerce-platform"
        / "channel.manifest.json"
    )
    platform = next(param for param in manifest.param_schema if param.name == "platform")
    assert platform.enum == PLATFORM_ADAPTERS


def test_platform_adapter_manifest_supports_bounded_listing_pagination():
    manifest = load_manifest(
        PackCatalog().root
        / "ecommerce"
        / "ecommerce-platform"
        / "channel.manifest.json"
    )
    assert manifest.pagination.mode == "url_page"
    assert manifest.pagination.page_param == "page"
    assert manifest.pagination.operations == ["listing"]
    assert manifest.pagination.page_param_map == {"suning": "cp", "1688": "beginPage"}
    assert manifest.pagination.page_index_offset == {"suning": -1}
    assert manifest.pagination.platforms == ["suning", "1688"]
    wait_step = manifest.steps[1]
    assert wait_step.selector and "product-box" in wait_step.selector
    assert "#J_goodsList" in wait_step.selector
    assert ".c-goods-item" in wait_step.selector
    cdp_endpoint = next(param for param in manifest.param_schema if param.name == "cdp_endpoint")
    assert cdp_endpoint.default == ""
    max_pages = next(param for param in manifest.param_schema if param.name == "max_pages")
    assert max_pages.default == "5"

def test_taobao_and_goofish_listing_wait_and_pagination_contracts():
    catalog = PackCatalog().root
    taobao = load_manifest(
        catalog / "ecommerce" / "taobao-shop-catalog" / "channel.manifest.json"
    )
    goofish = load_manifest(
        catalog / "ecommerce" / "goofish-search-list" / "channel.manifest.json"
    )

    assert taobao.pagination.page_param == "pageNo"
    assert taobao.steps[1].selector == '[id^="item_id_"]'
    assert goofish.steps[1].selector == 'a[class*="feeds-item-wrap"]'

def test_platform_listing_script_emits_normalized_product_schema():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute browser extraction scripts")

    script_path = (
        Path(__file__).parents[2]
        / "backend"
        / "browser_act_packs"
        / "ecommerce"
        / "ecommerce-platform"
        / "scripts"
        / "extract.py"
    )
    generated = subprocess.run(
        [sys.executable, str(script_path), "listing", "1688", "--max-results", "5"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fixture = r"""
const title = {textContent: "蓝牙耳机"};
const price = {textContent: "¥\n39\n.90"};
const originalPrice = {textContent: "¥\n59\n.90"};
const seller = {textContent: "供应商A"};
const minimumOrder = {textContent: "起批100件"};
const priceUnit = {textContent: "元/件"};
const businessType = {textContent: "生产厂家"};
const supplierLocation = {textContent: "浙江义乌"};
const reviews = {textContent: "1.2万条评价"};
const sales = {textContent: "已售3.4万"};
const image = {currentSrc: "https://img.example/earbuds.jpg", getAttribute: () => null};
const link = {href: "https://detail.1688.com/offer/123.html"};
const card = {
  textContent: "蓝牙耳机",
  getAttribute(name) {
    return name === "data-offer-id" ? "123" : null;
  },
  querySelector(selector) {
    if (selector === "a[href]") return link;
    if (selector === "h2") return title;
    if (selector === ".p-price i") return price;
    if (selector.includes("moq")) return minimumOrder;
    if (selector.includes("price-unit")) return priceUnit;
    if (selector.includes("factory")) return businessType;
    if (selector.includes("location")) return supplierLocation;
    if (selector.includes("origin")) return originalPrice;
    if (selector.includes("rating")) return {textContent: "4.8"};
    if (selector === ".p-commit a") return reviews;
    if (selector.includes("sale")) return sales;
    if (selector.includes("shop")) return seller;
    return null;
  },
  querySelectorAll(selector) {
    return selector === "img" ? [image] : [];
  }
};
global.document = {
  querySelectorAll(selector) {
    if (selector === "script[type=\"application/ld+json\"]") return [];
    return selector.includes('[class*="product-card"]') || selector.includes('[class*="offerCard"]') ? [card] : [];
  },
  querySelector() {
    return null;
  }
};
global.window = {location: {href: "https://s.1688.com/selloffer/offer_search.htm?keywords=耳机"}};
"""
    result = subprocess.run(
        [node, "--input-type=commonjs", "-"],
        input=fixture + "\nconsole.log(" + generated.strip() + ");\n",
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())
    item = payload["items"][0]

    assert payload["count"] == 1
    assert item["platform"] == "1688"
    assert item["product_id"] == "123"
    assert item["title"] == item["name"] == "蓝牙耳机"
    assert item["price"] == 39.9
    assert item["original_price"] == 59.9
    assert item["image_urls"] == ["https://img.example/earbuds.jpg"]
    assert item["seller_name"] == item["seller"] == "供应商A"
    assert item["metrics"] == {
        "rating": 4.8,
        "review_count": 12000,
        "sales_count": 34000,
    }
    assert item["pricing"]["current"] == 39.9
    assert item["pricing"]["original"] == 59.9
    assert item["platform_data"] == {
        "minimum_order_quantity": 100,
        "price_unit": "元/件",
        "business_type": "生产厂家",
        "supplier_location": "浙江义乌",
    }




@pytest.mark.asyncio
async def test_platform_adapter_can_reuse_persistent_cdp_endpoint():
    class FakeCdpSession:
        endpoint = None
        target_url = None
        selector = None
        def __init__(self, endpoint, target_url=None):
            self.endpoint = endpoint
            self.target_url = target_url
            self.urls = []

        async def __aenter__(self):
            FakeCdpSession.endpoint = self.endpoint
            FakeCdpSession.target_url = self.target_url
            return self

        async def __aexit__(self, *_exc):
            return None

        async def navigate(self, url):
            self.urls.append(url)

        async def wait(self, mode="stable"):
            return None

        async def wait_for_selector(self, selector, *, timeout=8000):
            FakeCdpSession.selector = selector

        async def run(self, args, *, timeout=None):
            return ""

        async def eval(self, js):
            return json.dumps(
                [{"id": "one", "title": "授权商品", "url": "https://example.com/one"}]
            )

    channel = BrowserActChannel(catalog=PackCatalog())
    with (
        patch(
            "backend.channels.browser_act_channel.CdpBrowserActSession",
            FakeCdpSession,
        ),
        patch(
            "backend.browser_act.cli._run",
            side_effect=AssertionError("persistent endpoint must not invoke browser-act"),
        ),
    ):
        result = await channel.collect(
            {
                "pack": EcommerceChannel.format("ecommerce-platform"),
                "params": {
                    "url": "https://example.com/listing",
                    "platform": "suning",
                    "operation": "listing",
                    "cdp_endpoint": "http://127.0.0.1:9224",
                },
                "max_pages": 1,
            },
            {},
        )

    assert result.success is True
    assert result.items == [
        {"id": "one", "title": "授权商品", "url": "https://example.com/one"}
    ]
    assert "offerCard" in FakeCdpSession.selector
    assert result.metadata["pages_fetched"] == 1
    assert FakeCdpSession.endpoint == "http://127.0.0.1:9224"
    assert FakeCdpSession.target_url == "https://example.com/listing"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", PLATFORM_ADAPTERS)
@pytest.mark.parametrize(
    ("operation", "payload", "required_field"),
    [
        ("listing", [{"url": "https://item.jd.com/1.html", "name": "商品"}], "url"),
        ("detail", {"name": "商品", "price": 1}, "name"),
        ("reviews", [{"body": "很好"}], "body"),
    ],
)
async def test_platform_adapter_collects_operations(
    platform, operation, payload, required_field
):
    channel = BrowserActChannel(catalog=PackCatalog())
    config = {
        "pack": EcommerceChannel.format("ecommerce-platform"),
        "params": {
            "url": f"https://example.com/{operation}",
            "platform": platform,
            "operation": operation,
        },
    }
    with patch(
        "backend.browser_act.cli._run",
        side_effect=_run_side_effect(json.dumps(payload)),
    ):
        result = await channel.collect(config, {})

    assert result.success is True
    assert result.items[0][required_field]
    assert result.metadata["pack"] == "ecommerce/ecommerce-platform"


@pytest.mark.asyncio
async def test_goofish_wrapper_payload_is_flattened_to_items():
    channel = BrowserActChannel(catalog=PackCatalog())
    with patch(
        "backend.browser_act.cli._run",
        side_effect=_run_side_effect(
            json.dumps({"items": [{"item_id": "1", "title": "二手商品"}], "count": 1})
        ),
    ):
        result = await channel.collect(
            {
                "pack": EcommerceChannel.format("goofish-search-list"),
                "params": {"keyword": "手机"},
            },
            {},
        )

    assert result.success is True
    assert result.items == [{"item_id": "1", "title": "二手商品"}]
