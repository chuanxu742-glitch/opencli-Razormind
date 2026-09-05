"""Command-scoped product identity and fact snapshots for OpenCLI collectors."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

_COMMANDS = {
    "taobao": {"search": "search", "detail": "product"},
    "jd": {"search": "search", "detail": "product", "item": "product"},
    "1688": {"search": "search", "item": "product"},
    "xianyu": {"search": "search", "item": "product"},
    "amazon": {"search": "search", "product": "product", "offer": "offer", "discussion": "discussion",
               "bestsellers": "search", "new-releases": "search", "movers-shakers": "search"},
    "coupang": {"search": "search", "product": "product"},
    "ebay": {"search": "search", "product": "product"},
}
_AMAZON_MARKETS = frozenset("amazon.com amazon.ca amazon.com.mx amazon.com.br amazon.co.uk amazon.de amazon.fr amazon.it amazon.es amazon.nl amazon.pl amazon.se amazon.com.be amazon.ie amazon.com.tr amazon.ae amazon.sa amazon.eg amazon.co.za amazon.in amazon.co.jp amazon.com.au amazon.sg".split())
_ID_KEYS = {"taobao": "item_id", "jd": "sku", "1688": "offer_id", "xianyu": "item_id", "amazon": "asin", "coupang": "product_id", "ebay": "itemId"}
_FIELD_KEYS = {"商品名称": "title", "价格": "price", "ID": "item_id", "SKU": "sku", "链接": "url"}
_OBSERVATION_KEYS = frozenset({"fetched_at", "observed_at", "rank", "source_url", "strategy", "pagination"})


def _url_id(site: str, value: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid {site} product URL type")
    if not value:
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    allowed = {
        "taobao": ("taobao.com", "tmall.com"), "jd": ("jd.com",), "1688": ("1688.com",),
        "xianyu": ("goofish.com",), "coupang": ("coupang.com",), "amazon": tuple(_AMAZON_MARKETS),
        "ebay": ("ebay.com",),
    }[site]
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not any(host == d or host.endswith("." + d) for d in allowed):
        raise ValueError(f"Invalid {site} product URL")
    patterns = {"amazon": r"/(?:dp|gp/product|product-reviews)/([A-Za-z0-9]{10})(?:/|$)",
                "jd": r"/(\d+)\.html(?:/|$)", "1688": r"/offer/(\d+)\.html(?:/|$)",
                "coupang": r"/vp/products/(\d+)(?:/|$)"}
    if site in patterns:
        match = re.search(patterns[site], parsed.path)
        if not match:
            raise ValueError(f"Invalid {site} product URL path")
        return match[1].upper()
    if site in {"taobao", "xianyu"}:
        native_id = parse_qs(parsed.query).get("id", [None])[0]
        if not native_id:
            raise ValueError(f"Missing {site} product URL identity")
        return native_id
    return None  # eBay web URLs contain legacy IDs, not Browse REST variant IDs.


def _request_input(site: str, positional_args: list[str], args: dict) -> str:
    if positional_args:
        return str(positional_args[0])
    for key in ("input", _ID_KEYS[site], "id", "product-id", "item-id", "url"):
        if args.get(key) is not None:
            return str(args[key])
    return ""


def _observation_time(value: Any) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("Invalid product fetched_at type")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    except ValueError as exc:
        raise ValueError("Invalid product fetched_at timestamp") from exc
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _amazon_market(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid Amazon marketplace evidence type")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        raise ValueError("Invalid Amazon marketplace evidence")
    for domain in _AMAZON_MARKETS:
        if host == domain or host.endswith("." + domain):
            return domain
    raise ValueError("Unknown Amazon marketplace")


def _coupang_variants(urls: list[str], fields: dict) -> dict[str, str]:
    variants: dict[str, str] = {}
    for values in [*(parse_qs(urlparse(url).query, keep_blank_values=True) for url in urls), fields]:
        for canonical, aliases in (("itemId", ("itemId", "item_id")), ("vendorItemId", ("vendorItemId", "vendor_item_id"))):
            for key in aliases:
                if key not in values:
                    continue
                value = values[key]
                if isinstance(value, list):
                    if len(value) != 1:
                        raise ValueError("Duplicate Coupang variant parameter")
                    value = value[0]
                value = str(value)
                if not value.isdigit() or canonical in variants and variants[canonical] != value:
                    raise ValueError("Conflicting Coupang variant identity")
                variants[canonical] = value
    return variants


def adapt_items(site: str, command: str, items: list[dict], *,
                positional_args: list[str] | None = None, args: dict | None = None) -> list[dict]:
    """Adapt only known product commands; reject ambiguous or conflicting identity."""
    facet = _COMMANDS.get(site, {}).get(command)
    if facet is None:
        return items
    args = args or {}
    request = _request_input(site, positional_args or [], args) if facet != "search" else ""
    request_id = _url_id(site, request) if "://" in request else request or None
    original_batch = None
    if site in {"taobao", "jd"} and command == "detail":
        if not items or any(not isinstance(row, dict) or "field" not in row or "value" not in row for row in items):
            raise ValueError(f"Invalid {site} detail field/value batch")
        original_batch = items
        fields = {}
        for row in items:
            if not isinstance(row["field"], str):
                raise ValueError(f"Invalid {site} detail field")
            key = _FIELD_KEYS.get(row["field"], row["field"])
            if key in fields and fields[key] != row["value"]:
                raise ValueError(f"Conflicting {site} detail field")
            fields[key] = row["value"]
        items = [fields]
    if facet != "search" and not items:
        raise ValueError(f"Empty {site} {facet} product result")
    result = []
    for raw in items:
        if not isinstance(raw, dict) or raw.get("error") or raw.get("errors"):
            raise ValueError(f"Invalid {site} product response")
        row = dict(raw)
        observed_at = _observation_time(row.get("fetched_at"))
        if row.get("pageState") is not None and not isinstance(row["pageState"], dict):
            raise ValueError(f"Invalid {site} pageState shape")
        urls = [str(row[key]) for key in ("product_url", "item_url", "url", "itemWebUrl") if row.get(key)]
        source_url = row.get("source_url")
        if site == "coupang" and isinstance(source_url, str) and "/vp/products/" in urlparse(source_url).path:
            urls.append(source_url)
        url = urls[0] if urls else ""
        page_url = (row.get("pageState") or {}).get("href", "") if isinstance(row.get("pageState"), dict) else ""
        observed_ids = [row.get(_ID_KEYS[site]), *(_url_id(site, value) for value in urls), _url_id(site, page_url)]
        if not any(value not in (None, "") for value in observed_ids):
            raise ValueError(f"Missing {site} page product identity")
        if site == "jd" and command == "item":
            state = row.get("pageState") or {}
            if not _url_id(site, page_url) or state.get("isProductPage") is not True or any(
                state.get(key) is True for key in ("looksBlocked", "isLoginPage", "hasSecurityChallenge")
            ):
                raise ValueError("JD item requires a confirmed, unblocked product page")
            price = row.get("price")
            has_content = (
                isinstance(row.get("title"), str) and bool(row["title"].strip())
                or isinstance(price, (int, float)) and not isinstance(price, bool)
                or isinstance(price, str) and price.strip().lower() not in {"", "not found"}
                or any(isinstance(row.get(key), (dict, list)) and bool(row[key]) for key in ("specs", "mainImages", "detailImages"))
            )
            if not has_content:
                raise ValueError("JD item contains no observed product content")
        named_url_id = _url_id(site, str(args["url"])) if facet != "search" and args.get("url") else None
        ids = [str(value) for value in (*observed_ids, request_id, named_url_id) if value not in (None, "")]
        if site == "amazon":
            ids = [value.upper() for value in ids]
        if not ids or len(set(ids)) != 1:
            raise ValueError(f"Missing or conflicting {site} product identity")
        native_id = ids[0]
        if site == "amazon":
            valid = re.fullmatch(r"[A-Z0-9]{10}", native_id)
        elif site == "ebay":
            valid = re.fullmatch(r"v1\|\d+\|\d+", native_id)
        else:
            valid = re.fullmatch(r"\d+", native_id)
        if not valid:
            raise ValueError(f"Invalid {site} native product ID")
        variant = {}
        if site == "amazon":
            evidence_urls = [*urls, page_url, *(row.get(key) for key in ("source_url", "review_url", "qa_url", "discussion_url"))]
            markets = {_amazon_market(value) for value in evidence_urls if value}
            if "://" in request:
                markets.add(_amazon_market(request))
            elif request:
                markets.add("amazon.com")  # Explicit bare-ASIN request uses the CLI's US default.
            if args.get("url") and facet != "search":
                markets.add(_amazon_market(str(args["url"])))
            if len(markets) != 1:
                raise ValueError("Missing or conflicting Amazon marketplace evidence")
            marketplace = markets.pop()
            url = f"https://www.{marketplace}/dp/{native_id}"
        elif site == "coupang":
            marketplace = "KR"
            variant = _coupang_variants([value for value in [*urls, page_url] if value], row)
            requested_variants = _coupang_variants(
                [value for value in (request if "://" in request else "", str(args.get("url") or "")) if value], {},
            )
            if any(variant.get(key) != value for key, value in requested_variants.items()):
                raise ValueError("Coupang requested variant lacks matching observed page evidence")
            url = f"https://www.coupang.com/vp/products/{native_id}"
            if variant:
                url += "?" + urlencode(variant)
        elif site == "ebay":
            for key in ("marketplace", "environment"):
                if row.get(key) is not None and args.get(key) is not None and row[key] != args[key]:
                    raise ValueError(f"Conflicting eBay {key}")
            marketplace = str(row.get("marketplace") or args.get("marketplace") or "EBAY_US")
            if marketplace != "EBAY_US":
                raise ValueError("Only EBAY_US is supported")
            environment = str(row.get("environment") or args.get("environment") or "production")
            if environment not in {"production", "sandbox"}:
                raise ValueError("Invalid eBay environment")
            if environment == "sandbox":
                marketplace = "sandbox:" + marketplace
            if not url:
                raise ValueError("Missing eBay itemWebUrl")
            parsed_url = urlparse(url)
            host = parsed_url.hostname or ""
            is_sandbox = host == "sandbox.ebay.com" or host.endswith(".sandbox.ebay.com")
            if is_sandbox != (environment == "sandbox"):
                raise ValueError("Conflicting eBay URL environment")
            query = parse_qs(parsed_url.query)
            variation = query.get("var", [])
            if variation and (len(variation) != 1 or variation[0] != native_id.split("|")[2]):
                raise ValueError("Conflicting eBay URL variation")
            url = parsed_url._replace(
                scheme="https", query=urlencode({"var": variation[0]}) if variation else "", fragment="",
            ).geturl()
        else:
            marketplace = "CN"
            url = {"taobao": f"https://item.taobao.com/item.htm?id={native_id}",
                   "jd": f"https://item.jd.com/{native_id}.html", "1688": f"https://detail.1688.com/offer/{native_id}.html",
                   "xianyu": f"https://www.goofish.com/item?id={native_id}"}[site]
        entity_id = f"{site}:{marketplace}:{native_id}" + "".join(f":{key}={value}" for key, value in sorted(variant.items()))
        currency = row.get("currency")
        if currency is None and isinstance(row.get("price"), dict):
            currency = row["price"].get("currency")
        if currency is None:
            currency = {"CN": "CNY", "KR": "KRW"}.get(marketplace)
        if currency is not None and not isinstance(currency, str):
            raise ValueError(f"Invalid {site} currency type")
        row["_ecommerce"] = {
            "entity_id": entity_id, "platform": site, "marketplace": marketplace,
            "native_id": native_id, "variant": variant, "facet": facet, "url": url,
            "currency": currency,
            "observed_at": observed_at,
            **({"raw": original_batch} if original_batch is not None else {}),
        }
        result.append(row)
    return result


def ecommerce_metadata(raw: dict) -> dict | None:
    """Recognize the complete internal envelope, not an unrelated same-name field."""
    product = raw.get("_ecommerce")
    if not isinstance(product, dict):
        return None
    if any(not isinstance(product.get(key), str) or not product[key] for key in (
        "entity_id", "platform", "marketplace", "native_id", "facet", "url", "observed_at",
    )):
        return None
    if product["platform"] not in _COMMANDS or product["facet"] not in _COMMANDS[product["platform"]].values():
        return None
    variant = product.get("variant")
    if not isinstance(variant, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in variant.items()):
        return None
    expected = f"{product['platform']}:{product['marketplace']}:{product['native_id']}"
    expected += "".join(f":{key}={value}" for key, value in sorted(variant.items()))
    if product["entity_id"] != expected or "currency" not in product or not (
        product["currency"] is None or isinstance(product["currency"], str)
    ):
        return None
    try:
        _observation_time(product["observed_at"])
        platform = product["platform"]
        native_id = product["native_id"]
        pattern = r"[A-Z0-9]{10}" if platform == "amazon" else r"v1\|\d+\|\d+" if platform == "ebay" else r"\d+"
        if not re.fullmatch(pattern, native_id):
            return None
        url_id = _url_id(platform, product["url"])
        if url_id is not None and url_id != native_id:
            return None
        if platform == "amazon" and _amazon_market(product["url"]) != product["marketplace"]:
            return None
        if platform == "coupang":
            if product["marketplace"] != "KR" or _coupang_variants([product["url"]], {}) != variant:
                return None
        elif variant:
            return None
        if platform in {"taobao", "jd", "1688", "xianyu"} and product["marketplace"] != "CN":
            return None
        if platform == "ebay" and product["marketplace"] not in {"EBAY_US", "sandbox:EBAY_US"}:
            return None
    except ValueError:
        return None
    return product


def ecommerce_identity(item: dict) -> str | None:
    product = ecommerce_metadata(item)
    if product is None:
        return None
    return product["entity_id"] + "|" + product["facet"]


def _facts(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _facts(child) for key, child in value.items() if key not in _OBSERVATION_KEYS}
    if isinstance(value, list):
        return [_facts(child) for child in value]
    return value


def product_snapshot(raw: dict) -> dict | None:
    """Separate facts from observation clocks without discarding original evidence."""
    product = ecommerce_metadata(raw)
    if product is None:
        return None
    facts = _facts({key: value for key, value in raw.items() if key != "_ecommerce"})
    if product["platform"] == "amazon":
        facts.pop("review_url", None)
        facts.pop("qa_url", None)
    # URL query tracking and search position are provenance, not product changes.
    for key in ("url", "product_url", "item_url", "itemWebUrl"):
        if key in facts:
            facts[key] = product["url"]
    if isinstance(facts.get("pageState"), dict) and "href" in facts["pageState"]:
        facts["pageState"]["href"] = product["url"]
    snapshot = {key: value for key, value in product.items() if key not in {"raw", "observed_at"}}
    snapshot["facts"] = facts
    serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    snapshot["fact_version"] = hashlib.sha256(serialized.encode()).hexdigest()
    snapshot["observed_at"] = _observation_time(product["observed_at"])
    return snapshot
