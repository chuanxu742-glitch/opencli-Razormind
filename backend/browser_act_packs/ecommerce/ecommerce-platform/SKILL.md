---
name: ecommerce-platform
description: "Extract visible product listings, product details, and reviews from public JD, Pinduoduo, Douyin Shop, Kuaishou Shop, Xiaohongshu Shop, Vipshop, Suning, 1688, Dewu, or other e-commerce pages. Uses the supplied page URL and reads only data rendered in the current browser session."
---

# E-commerce — Platform Adapter

This pack follows the existing `ecommerce-listing`, `ecommerce-product-detail`, and `ecommerce-reviews` packs. It combines their JSON-LD, Open Graph, microdata, and generic DOM strategies, with a small set of platform card selectors for pages that expose them.

The platform list selects an adapter strategy; it is not a guarantee that the
current live site is reachable or exposes every field. Login state, CAPTCHA,
anti-bot controls, page revisions, and platform-specific rendering must be
verified in the target browser session before production use.

Parameters:
- `url`: the public or already-authorized page URL
- `platform`: `jd`, `pinduoduo`, `douyin`, `kuaishou`, `xiaohongshu`, `vipshop`, `suning`, `1688`, `dewu`, or `auto`
- `operation`: `listing`, `detail`, or `reviews`
- `max_results`: maximum listing/review items per page
- `max_pages`: listing page cap, default 5 and hard-capped at 100
- `cdp_endpoint`: optional absolute HTTP(S) endpoint for an already-running,
  persistent Chrome profile; when set, the pack attaches to that browser and
  leaves the Chrome process and its cookies running after collection

Listing pagination follows the existing BrowserAct `url_page` contract. The
default query parameter is `page`; live platform mappings currently verified
by the project browser are `suning -> cp` (zero-based) and `1688 ->
beginPage` (one-based). Automatic pagination is enabled only for those two
platforms until the other sites expose a verifiable public page control.
It replaces or appends the selected query parameter, stops when a page has no
results, and de-duplicates items by `url`, `id`, `item_id`, `product_id`, or
`sku`. Detail and review operations remain single-page.

Output follows the existing pack contracts:
- `listing`: `{"count": ..., "items": [...]}`
- `detail`: one product object
- `reviews`: `{"count": ..., "reviews": [...]}`

Listing items use a normalized core schema. Every listing item has stable
top-level names (`platform`, `product_id`, `title`, `url`, `price`,
`original_price`, `currency`, `image_urls`, `seller_name`, `rating`,
`review_count`, and `sales_count`). Add `crawl_time` and `page_number` in the
persistence layer if historical tracking is required. `pricing`, `inventory`,
and `metrics` group the same concepts without changing the legacy aliases
`name`, `image`, `seller`, `price`, and `review_count`. Missing values are
`null`, not guessed.

Platform-only values stay under `platform_data`: 1688 can expose
`minimum_order_quantity`, `price_unit`, `business_type`, and
`supplier_location`; Suning can expose `store_type` and `shipping_fee`.
These fields are populated only when the current rendered card exposes them.

Detail and review objects retain the original pack fields.

## Execution efficiency

- Listing runs wait for a platform card selector before paying the bounded
  lazy-render settle wait; already-hydrated pages continue immediately.
- Keep ``max_pages`` and ``max_results`` aligned with the task. The configured
  local sources use two pages and 20 items per page.
- Run listing first, then use ``detail`` only for shortlisted products. This
  avoids opening a detail page for every candidate and reduces platform
  verification pressure.

Live quality note: the public Suning search page can render product identity,
seller, and images while marking price data as unavailable (`hasprice=false`).
The extractor keeps those products with `price: null` instead of discarding or
inventing a price; use the product detail operation for an authoritative price.

Manual authentication workflow: start a headed Chrome with a dedicated
`--user-data-dir`, expose its CDP port, and complete the site's login,
QR-code, OTP, or CAPTCHA interaction in that Chrome window. Store only the
`cdp_endpoint` in the pack configuration; never store a website password,
cookie, or token in a manifest or source record.
