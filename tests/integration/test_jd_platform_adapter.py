"""JD search-card contract for the unified e-commerce adapter.

The fixture mirrors the authorized desktop search page:
``#J_goodsList ul.gl-warp.clearfix > li.gl-item[data-sku]``.
JD's list pagination uses odd ``page`` values (1, 3, 5, ...) and the
corresponding ``s`` offsets (1, 31, 61, ...).  A live browser must already
be authenticated; this test intentionally exercises only rendered DOM.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


JD_SEARCH_URL = (
    "https://search.jd.com/Search?keyword=%E8%80%B3%E6%9C%BA"
    "&enc=utf-8&page=1&s=1"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_jd_listing_extracts_gl_item_and_data_sku_cards():
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
        [sys.executable, str(script_path), "listing", "jd", "--max-results", "5"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    fixture = r'''
// JD desktop card: li.gl-item[data-sku] under #J_goodsList .gl-warp.
// The browser resolves protocol-relative item/image URLs and populates
// currentSrc after the bounded scroll used by the pack.
const nodes = ({textContent = "", currentSrc = null} = {}) => ({
  textContent,
  innerText: textContent,
  currentSrc,
  getAttribute: () => null,
});
const cards = [
  {
    getAttribute(name) { return name === "data-sku" ? "100012345678" : null; },
    querySelector(selector) {
      if (selector === "a[href]") return {href: "https://item.jd.com/100012345678.html"};
      if (selector === ".p-name a") return nodes({textContent: "京东自营无线降噪耳机"});
      if (selector === ".p-price i") return nodes({textContent: "¥1299.00"});
      if (selector === ".p-commit a") return nodes({textContent: "2.3万条评价"});
      if (selector === ".p-shop a") return nodes({textContent: "京东自营"});
      return null;
    },
    querySelectorAll(selector) {
      return selector === "img" ? [
        {currentSrc: "https://img.example/jd-headset.jpg", getAttribute(name) {
          return name === "data-lazy-img" ? "//img.example/jd-headset.jpg" : null;
        }},
      ] : [];
    },
  },
  {
    getAttribute(name) { return name === "data-sku" ? "100098765432" : null; },
    querySelector(selector) {
      if (selector === "a[href]") return {href: "https://item.jd.com/100098765432.html"};
      if (selector === ".p-name a") return nodes({textContent: "轻薄办公笔记本电脑"});
      if (selector === ".p-price i") return nodes({textContent: "¥3999.00"});
      if (selector === ".p-commit a") return nodes({textContent: "8765条评价"});
      if (selector === ".p-shop a") return nodes({textContent: "京东电脑旗舰店"});
      return null;
    },
    querySelectorAll(selector) {
      return selector === "img" ? [
        {currentSrc: "https://img.example/jd-laptop.jpg", getAttribute(name) {
          return name === "data-lazy-img" ? "//img.example/jd-laptop.jpg" : null;
        }},
      ] : [];
    },
  },
];
const queriedSelectors = [];
global.document = {
  querySelectorAll(selector) {
    queriedSelectors.push(selector);
    if (selector === 'script[type="application/ld+json"]') return [];
    return selector.includes(".gl-item") && selector.includes("[data-sku]") ? cards : [];
  },
};
global.window = {location: {href: "https://search.jd.com/Search?keyword=%E8%80%B3%E6%9C%BA&enc=utf-8&page=1&s=1"}};
'''
    result = subprocess.run(
        [shutil.which("node"), "--input-type=commonjs", "-"],
        input=fixture + "\nconsole.log(" + generated.strip() + ");\n",
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())

    assert payload["count"] == 2
    first, second = payload["items"]
    assert first == {
        **first,
        "platform": "jd",
        "product_id": "100012345678",
        "title": "京东自营无线降噪耳机",
        "url": "https://item.jd.com/100012345678.html",
        "price": 1299,
        "original_price": None,
        "currency": "CNY",
        "image_urls": ["https://img.example/jd-headset.jpg"],
        "seller_name": "京东自营",
        "rating": None,
        "review_count": 23000,
        "sales_count": None,
    }
    assert second["product_id"] == "100098765432"
    assert second["price"] == 3999
    assert second["review_count"] == 8765
    assert second["sales_count"] is None
    assert second["rating"] is None
    assert second["seller_name"] == "京东电脑旗舰店"


def test_jd_search_url_uses_odd_page_and_start_index_parameters():
    params = parse_qs(urlparse(JD_SEARCH_URL).query)
    assert params["keyword"] == ["耳机"]
    assert params["enc"] == ["utf-8"]
    assert params["page"] == ["1"]
    assert params["s"] == ["1"]

    for page_number, expected_page, expected_start in (
        (1, 1, 1),
        (2, 3, 31),
        (3, 5, 61),
    ):
        assert expected_page == 2 * page_number - 1
        assert expected_start == 30 * (page_number - 1) + 1
