"""Vipshop search-card contract for the unified e-commerce adapter."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_vipshop_listing_extracts_product_card_fields():
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
        [sys.executable, str(script_path), "listing", "vipshop", "--max-results", "5"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fixture = r"""
const title = {textContent: "无线降噪耳机"};
const price = {textContent: "¥199"};
const originalPrice = {textContent: "¥399"};
const brand = {textContent: "品牌A"};
const stock = {textContent: "售罄"};
const image = {currentSrc: "https://img.example/vip.jpg", getAttribute: () => null};
const link = {href: "https://detail.vip.com/detail-1710613337-6918741867783667929.html"};
const card = {
  textContent: "无线降噪耳机",
  getAttribute(name) {
    return name === "data-product-id" ? "6918741867783667929" : null;
  },
  querySelector(selector) {
    if (selector === "a[href]") return link;
    if (selector === ".c-goods-item__name") return title;
    if (selector.includes("sale-price")) return price;
    if (selector.includes("market-price")) return originalPrice;
    if (selector.includes("brand")) return brand;
    if (selector.includes("stock") || selector.includes("status")) return stock;
    return null;
  },
  querySelectorAll(selector) {
    return selector === "img" ? [image] : [];
  }
};
global.document = {
  querySelectorAll(selector) {
    if (selector === "script[type=\"application/ld+json\"]") return [];
    const isCard =
      selector.includes(".c-goods-item") || selector.includes("[data-product-id]");
    return isCard ? [card] : [];
  },
  querySelector() {
    return null;
  }
};
global.window = {location: {href: "https://category.vip.com/suggest.php?keyword=耳机"}};
"""
    result = subprocess.run(
        ["node", "--input-type=commonjs", "-"],
        input=fixture + "\nconsole.log(" + generated.strip() + ");\n",
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())
    item = payload["items"][0]

    assert payload["count"] == 1
    assert item["platform"] == "vipshop"
    assert item["product_id"] == "6918741867783667929"
    assert item["title"] == item["name"] == "无线降噪耳机"
    assert item["brand"] == "品牌A"
    assert item["price"] == 199
    assert item["original_price"] == 399
    assert item["currency"] == "CNY"
    assert item["image_urls"] == ["https://img.example/vip.jpg"]
    assert item["inventory"]["status"] == "售罄"
    assert item["seller_name"] is None
