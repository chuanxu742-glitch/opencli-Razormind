import argparse
import sys


PLATFORMS = [
    "jd",
    "pinduoduo",
    "xiaohongshu",
    "vipshop",
    "suning",
    "1688",
    "dewu",
    "auto",
]


def main():
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["listing", "detail", "reviews"])
    parser.add_argument("platform", choices=PLATFORMS)
    parser.add_argument("--max-results", type=int, default=20)
    args = parser.parse_args()
    max_results = max(1, min(args.max_results, 100))

    js_template = r"""
    (function() {
      try {
        const operation = 'OPERATION';
        const platform = 'PLATFORM';
        const maxResults = MAX_RESULTS;
        const lds = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => {
          try { return JSON.parse(s.textContent); } catch(e) { return null; }
        }).filter(Boolean);
        const flat = lds.flatMap(l => {
          if (Array.isArray(l)) return l;
          if (Array.isArray(l['@graph'])) return l['@graph'];
          return [l];
        });
        const parseNumber = value => value != null ? parseFloat(String(value).replace(/,/g, '').replace(/[^0-9.]/g, '')) || null : null;
        const productType = item => item && (item['@type'] === 'Product' || (Array.isArray(item['@type']) && item['@type'].includes('Product')));
        const productLds = flat.filter(productType);
        const offerOf = item => Array.isArray(item?.offers) ? item.offers[0] : item?.offers;
        const text = el => el?.innerText?.trim() || el?.textContent?.trim() || null;
        const firstText = (root, selectors) => {
          for (const selector of selectors) {
            const value = text(root.querySelector(selector));
            if (value) return value;
          }
          return null;
        };
        const platformCards = {
          jd: '#J_goodsList ul.gl-warp.clearfix > li.gl-item[data-sku], .gl-item[data-sku], [data-sku]',
          pinduoduo: '[class*="goods-item"], [class*="goodsItem"], [class*="search-result"]',
          xiaohongshu: '[data-testid*="product"], [data-goods-id], [data-product-id]',
          vipshop: '#J_searchCatList .c-goods-item[data-product-id], #J_wrap_pro_add .c-goods-item[data-product-id], .c-goods-item[data-product-id], .c-goods-item[data-spu], [data-spu], [data-sku]',
          suning: '[data-sku], [data-product-id], [class*="product-box"]',
          '1688': '[data-offer-id], [data-offerid], [class*="offer-item"], [class*="offerItem"], [class*="search-offer"], [class*="offerCard"]',
          dewu: '[data-spu], [data-spu-id], [data-product-id]',
          auto: ''
        };
        const genericCards = [
          platformCards[platform],
          'article[class*="product"]',
          '[class*="product-card"]',
          '[class*="product-item"]',
          '[class*="product-tile"]',
          '[class*="goods-card"]',
          '[class*="goods-item"]',
          'li.product'
        ].filter(Boolean).join(',');
        const clean = value => {
          if (value == null) return null;
          const result = String(value).replace(/\s+/g, ' ').trim();
          return result || null;
        };
        const firstAttribute = (root, selectors, attributes) => {
          if (!root) return null;
          for (const attribute of attributes) {
            const value = clean(root.getAttribute?.(attribute));
            if (value) return value;
          }
          for (const selector of selectors) {
            const element = root.querySelector(selector);
            if (!element) continue;
            for (const attribute of attributes) {
              const value = clean(element.getAttribute(attribute));
              if (value) return value;
            }
          }
          return null;
        };
        const parseCount = value => {
          const result = clean(value);
          const match = result?.match(/(\d[\d,.]*)(亿|万)?/);
          if (!match) return null;
          const number = parseFloat(match[1].replace(/,/g, ''));
          if (!Number.isFinite(number)) return null;
          return match[2] === '亿' ? number * 100000000 : match[2] === '万' ? number * 10000 : number;
        };
        const parsePrices = value => {
          const normalized = String(value ?? '').replace(/\s+/g, '');
          const matches = normalized.match(/\d[\d,]*(?:\.\d+)?/g) || [];
          return matches.map(v => parseFloat(v.replace(/,/g, ''))).filter(Number.isFinite);
        };
        const putIfPresent = (target, key, value) => {
          const result = clean(value);
          if (result != null) target[key] = result;
        };
        const extractImages = root => root ? Array.from(root.querySelectorAll('img')).map(img =>
          img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') ||
          img.getAttribute('data-original')
        ).filter(Boolean) : [];
        const extractProductId = (raw, card) => {
          const normalizeId = value => {
            const result = clean(value);
            if (!result) return null;
            const prefix = result.split('|')[0];
            if (prefix) return prefix;
            const suningMatch = result.match(/\|\|([^|]+)\|\|/);
            return suningMatch ? suningMatch[1] : result;
          };
          if (raw.product_id || raw.sku) return normalizeId(raw.product_id || raw.sku);
          const id = firstAttribute(card, [
            '[data-sku]', '[data-product-id]', '[data-offer-id]', '[data-offerid]',
            '[data-spu]', '[data-spu-id]', '[datasku]', '[datapro]'
          ], [
            'data-sku', 'data-product-id', 'data-offer-id', 'data-offerid',
            'data-spu', 'data-spu-id', 'datasku', 'datapro'
          ]);
          if (id) return normalizeId(id);
          const ancestorId = card?.closest?.('[id]')?.id;
          if (ancestorId) {
            const ancestorMatch = ancestorId.match(/(?:-|_)(\d+)$/);
            if (ancestorMatch) return ancestorMatch[1];
          }
          const match = String(raw.url || '').match(/[?&](?:offerId|sku|id)=([^&#]+)/i);
          if (match) return decodeURIComponent(match[1]);
          const jdMatch = String(raw.url || '').match(/item\.jd\.com\/(\d+)\.html/i);
          if (jdMatch) return jdMatch[1];
          const vipMatch = String(raw.url || '').match(/detail\.vip\.com\/detail-\d+-(\d+)\.html/i);
          return vipMatch ? vipMatch[1] : null;
        };
        const normalizeListingItem = (raw, card = null) => {
          const title = clean(raw.title || raw.name);
          const priceText = raw.price_text || '';
          const prices = parsePrices(priceText);
          const currentPrice = raw.price ?? prices[0] ?? null;
          const originalPrice = raw.original_price ?? parsePrices(raw.original_price_text || '')[0] ?? null;
          const sellerFallbackSelectors = platform === 'vipshop'
            ? ['[class*="shop"]', '[class*="store"]', '[class*="seller"]', '[class*="company"]']
            : [
              '.p-shop a', '[class*="shop"]', '[class*="store"]',
              '[class*="seller"]', '[class*="brand"]', '[class*="company"]'
            ];
          const sellerName = raw.seller_name || raw.seller ||
            (card ? firstText(card, sellerFallbackSelectors) : null);
          const reviewCount = raw.review_count ?? parseCount(raw.review_text);
          const salesCount = raw.sales_count ?? parseCount(raw.sales_text);
          const stockStatus = raw.stock_status || clean(raw.stock_text);
          const images = raw.image_urls?.length ? raw.image_urls :
            (raw.image ? [raw.image] : extractImages(card));
          const platformData = {...(raw.platform_data || {})};
          if (card && platform === 'suning') {
            putIfPresent(platformData, 'store_type', firstText(card, [
              '.store-class', '[class*="store-class"]',
              '[class*="self"]', '[class*="shop-type"]'
            ]));
            putIfPresent(platformData, 'shipping_fee', firstText(card, [
              '[class*="freight"]', '[class*="shipping"]', '[class*="delivery-fee"]'
            ]));
          }
          if (card && platform === '1688') {
            const minimumOrder = parseCount(firstText(card, [
              '[class*="moq"]', '[class*="min-order"]', '[class*="order-num"]',
              '[class*="起批"]'
            ]));
            if (minimumOrder != null) platformData.minimum_order_quantity = minimumOrder;
            putIfPresent(platformData, 'price_unit', firstText(card, [
              '[class*="price-unit"]', '[class*="unit"]', '[class*="计价"]'
            ]));
            putIfPresent(platformData, 'business_type', firstText(card, [
              '[class*="factory"]', '[class*="supplier-type"]', '[class*="厂家"]'
            ]));
            putIfPresent(platformData, 'supplier_location', firstText(card, [
              '[class*="location"]', '[class*="address"]', '[class*="发货地"]'
            ]));
          }
          const pricing = {
            current: currentPrice,
            min: raw.price_min ?? (prices.length > 1 ? prices[0] : currentPrice),
            max: raw.price_max ?? (prices.length > 1 ? prices[prices.length - 1] : currentPrice),
            original: originalPrice,
            currency: raw.currency || 'CNY',
            unit: raw.price_unit || platformData.price_unit || null,
            min_quantity: platformData.minimum_order_quantity || null,
            tiers: raw.price_tiers || null
          };
          return {
            platform,
            product_id: extractProductId(raw, card),
            title,
            name: title,
            url: raw.url || null,
            category: raw.category || null,
            brand: raw.brand || null,
            model: raw.model || null,
            price: currentPrice,
            original_price: originalPrice,
            currency: pricing.currency,
            image_urls: images,
            image: images[0] || null,
            seller_name: sellerName,
            seller: sellerName,
            pricing,
            inventory: {status: stockStatus},
            stock_status: stockStatus,
            metrics: {rating: raw.rating ?? null, review_count: reviewCount, sales_count: salesCount},
            rating: raw.rating ?? null,
            review_count: reviewCount,
            sales_count: salesCount,
            specifications: raw.specifications || null,
            platform_data: Object.keys(platformData).length > 0 ? platformData : null
          };
        };


        if (operation === 'listing') {
          let items = [];

          // Strategy 1: JSON-LD ItemList, matching ecommerce-listing.
          const listEl = flat.find(l => l['@type'] === 'ItemList' && l.itemListElement);
          if (listEl) {
            items = (listEl.itemListElement || []).slice(0, maxResults).map(e => {
              const item = e.item || e;
              const offer = offerOf(item);
              return normalizeListingItem({
                product_id: item.sku || item.productID || null,
                url: item.url || null,
                title: item.name || null,
                category: item.category || null,
                brand: typeof item.brand === 'string' ? item.brand : item.brand?.name,
                price: parseNumber(offer?.price),
                currency: offer?.priceCurrency || null,
                image_urls: Array.isArray(item.image) ? item.image : (item.image ? [item.image] : []),
                rating: parseNumber(item.aggregateRating?.ratingValue),
                review_count: parseCount(item.aggregateRating?.reviewCount)
              });
            }).filter(item => item.url || item.title);
          }

          // Strategy 2: JSON-LD Product collection, matching ecommerce-listing.
          if (items.length === 0 && productLds.length > 1) {
            items = productLds.slice(0, maxResults).map(item => {
              const offer = offerOf(item);
              return normalizeListingItem({
                product_id: item.sku || item.productID || null,
                url: item.url || item['@id'] || null,
                title: item.name || null,
                category: item.category || null,
                brand: typeof item.brand === 'string' ? item.brand : item.brand?.name,
                price: parseNumber(offer?.price),
                currency: offer?.priceCurrency || null,
                image_urls: Array.isArray(item.image) ? item.image : (item.image ? [item.image] : []),
                rating: parseNumber(item.aggregateRating?.ratingValue),
                review_count: parseCount(item.aggregateRating?.reviewCount)
              });
            }).filter(item => item.url || item.title);
          }

          // Strategy 3: platform cards plus generic product-card fallback.
          if (items.length === 0) {
            const cards = Array.from(document.querySelectorAll(genericCards));
            items = cards.slice(0, maxResults).map(card => {
              const productLinks = Array.from(card.querySelectorAll?.('a[href]') || [])
                .filter(anchor => anchor.href?.includes('item.jd.com'));
              const fallbackLink = platform === 'jd'
                ? null
                : (card.tagName?.toLowerCase() === 'a' ? card : card.querySelector('a[href]'));
              const link = productLinks[0] || fallbackLink;
              const cardProductId = firstAttribute(card, [
                '[data-sku]', '[data-product-id]', '[data-offer-id]',
                '[data-offerid]', '[data-spu]', '[data-spu-id]'
              ], [
                'data-sku', 'data-product-id', 'data-offer-id',
                'data-offerid', 'data-spu', 'data-spu-id'
              ]);
              const offerId = link?.href?.match(/[?&]offerId=([^&#]+)/i)?.[1]
                || card.getAttribute('data-renderkey')?.match(/_(\d{8,})$/)?.[1]
                || card.getAttribute('data-aplus-report')?.match(/object_id@(\d+)/)?.[1]
                || null;
              const nameSelectors = platform === 'jd'
                ? ['.p-name em', '.p-name a', '.p-name', '[class*="goods_title"]', '[class*="product-title"]', '[class*="goods-title"]']
                : platform === 'vipshop'
                  ? ['.c-goods-item__name', '[class*="goods-item__name"]', '[class*="product-title"]', '[class*="goods-title"]', '[class*="title"]']
                  : [
                    '.title-selling-point > a', '.p-name a',
                    'h2', 'h3', 'h4', '.p-name em',
                    '[class*="product-title"]', '[class*="goods-title"]',
                    '[class*="title"]', '[class*="name"]'
                  ];
              const priceSelectors = platform === 'jd'
                ? ['.p-price em', '.p-price i', '.p-price', '[itemprop="price"]', '[class*="price"]', '[data-testid*="price"]']
                : platform === 'vipshop'
                  ? ['.c-goods-item__sale-price.J-goods-item__sale-price', '.c-goods-item__sale-price', '[itemprop="price"]', '[class*="sale-price"]', '[class*="price"]']
                  : [
                    '.def-price', '.p-price i', '[itemprop="price"]',
                    '[class*="price"]', '[data-testid*="price"]'
                  ];
              const originalPriceSelectors = platform === 'vipshop'
                ? ['.c-goods-item__market-price.J-goods-item__market-price', '.c-goods-item__market-price', '[class*="market-price"]', '[class*="original"]', '[class*="origin"]', '[class*="del"]']
                : [
                  '[class*="origin"]', '[class*="original"]',
                  '[class*="market-price"]', '[class*="del"]'
                ];
              const reviewSelectors = platform === 'jd'
                ? ['.p-commit strong a', '.p-commit a', '[class*="review"]', '[class*="comment"]']
                : ['.p-commit a', '[class*="review"]', '[class*="comment"]'];
              const salesSelectors = platform === 'jd'
                ? ['[class*="goods_volume"]', '[class*="volume"]', '[title*="已售"]', '[class*="sold"]', '[class*="sale"]']
                : platform === 'vipshop'
                  ? ['[class*="sale-count"]', '[class*="sales"]', '[class*="volume"]', '[class*="sold"]']
                  : [
                    '[class*="sale"]', '[class*="deal"]',
                    '[class*="volume"]', '[class*="sold"]',
                    '[class*="成交"]', '[class*="销量"]'
                  ];
              const stockSelectors = platform === 'vipshop'
                ? ['[class*="stock"]', '[class*="soldout"]', '[class*="saleout"]', '[class*="status"]']
                : [
                  '[class*="stock-status"]', '[class*="stock-info"]',
                  '[class*="inventory"]', '[class*="availability"]',
                  '[class*="库存"]', '[class*="有货"]'
                ];
              const sellerSelectors = platform === 'jd'
                ? ['.p-shop a', '[class*="shop"]', '[class*="store"]', '[class*="seller"]', '[class*="_limit_"]']
                : platform === 'vipshop'
                  ? ['[class*="shop"]', '[class*="store"]', '[class*="seller"]', '[class*="company"]']
                  : [
                    '.p-shop a', '[class*="shop"]', '[class*="store"]',
                    '[class*="seller"]', '[class*="brand"]', '[class*="company"]'
                  ];
              const name = platform === 'jd'
                ? (firstAttribute(card, ['[title]'], ['title']) || firstText(card, nameSelectors))
                : firstText(card, nameSelectors);
              const priceText = firstText(card, priceSelectors);
              const originalPriceText = firstText(card, originalPriceSelectors);
              const reviewText = firstText(card, reviewSelectors);
              const salesText = firstText(card, salesSelectors);
              const stockText = firstText(card, stockSelectors);
              return normalizeListingItem({
                product_id: offerId || cardProductId,
                url: link?.href || (
                  platform === 'jd' && cardProductId
                    ? `https://item.jd.com/${cardProductId}.html`
                    : null
                ),
                title: name,
                brand: platform === 'vipshop' ? firstText(card, [
                  '.c-goods-item__brand', '[class*="brand-name"]', '[class*="brand"]'
                ]) : null,
                price: parseNumber(priceText),
                price_text: priceText,
                original_price_text: originalPriceText,
                currency: 'CNY',
                image_urls: extractImages(card),
                seller: firstText(card, sellerSelectors),
                review_text: reviewText,
                sales_text: salesText,
                stock_text: stockText,
                rating: parseNumber(firstText(card, [
                  '[class*="rating"]', '[class*="score"]'
                ]))
              }, card);
            }).filter(item => item.url && item.title);
          }

          if (items.length === 0) {
            const authUrl = /(?:\/|[._-])(login|passport|signin|sign-in|punish|x5sec)(?=[./?#_-]|$)/i.test(window.location.href);
            if (authUrl) {
              return JSON.stringify({ error: true, message: `Login or verification required for ${platform} before collecting product listings` });
            }
            return JSON.stringify({ error: true, message: 'No product listings found on this page. Ensure this is a category, search results, or product listing page.' });
          }
          return JSON.stringify({ count: items.length, items });
        }

        if (operation === 'detail') {
          const result = {};
          const pld = productLds[0];
          const rld = flat.find(l => l['@type'] === 'AggregateRating' && l.itemReviewed?.['@type'] === 'Product');

          // Strategy 1: JSON-LD Product, matching ecommerce-product-detail.
          if (pld) {
            result.name = pld.name || null;
            result.description = pld.description || null;
            result.brand = (typeof pld.brand === 'string' ? pld.brand : pld.brand?.name) || null;
            const imgs = Array.isArray(pld.image) ? pld.image : (pld.image ? [pld.image] : []);
            result.image = imgs[0] || null;
            result.images = imgs.length > 0 ? imgs : null;
            result.category = Array.isArray(pld.category) ? pld.category : (pld.category ? [pld.category] : null);
            result.sku = pld.sku || null;
            result.gtin = pld.gtin || pld.gtin14 || pld.gtin13 || pld.gtin12 || pld.gtin8 || null;
            result.mpn = pld.mpn || null;
            const offer = offerOf(pld);
            if (offer) {
              result.price = parseNumber(offer.price);
              result.price_currency = offer.priceCurrency || null;
              result.availability = offer.availability ? offer.availability.replace('https://schema.org/', '') : null;
              result.seller = (typeof offer.seller === 'string' ? offer.seller : offer.seller?.name) || null;
            }
            const agg = pld.aggregateRating || rld;
            if (agg) {
              result.rating = parseNumber(agg.ratingValue);
              result.review_count = parseNumber(agg.reviewCount || agg.ratingCount);
            }
            result._source = 'json-ld';
          }

          // Strategy 2: Open Graph and microdata fallbacks, matching the original detail pack.
          if (!result.name) result.name = document.querySelector('meta[property="og:title"]')?.getAttribute('content') || null;
          if (!result.image) result.image = document.querySelector('meta[property="og:image"]')?.getAttribute('content') || null;
          if (!result.description) result.description = document.querySelector('meta[property="og:description"], meta[name="description"]')?.getAttribute('content') || null;
          if (!result.price) {
            const ogp = document.querySelector('meta[property="og:price:amount"]')?.getAttribute('content');
            if (ogp) result.price = parseNumber(ogp);
          }
          if (!result.price_currency) result.price_currency = document.querySelector('meta[property="og:price:currency"]')?.getAttribute('content') || null;
          if (!result.name) result.name = document.querySelector('[itemprop="name"], h1, [class*="product-title"], [class*="goods-title"], [class*="product-name"]')?.textContent.trim() || null;
          if (!result.price) {
            const mp = document.querySelector('[itemprop="price"], [class*="price"]');
            if (mp) result.price = parseNumber(mp.getAttribute('content') || mp.textContent);
          }
          if (!result.sku) result.sku = document.querySelector('[itemprop="sku"]')?.textContent.trim() || document.querySelector('[itemprop="sku"]')?.getAttribute('content') || null;
          if (!result.brand) result.brand = document.querySelector('[itemprop="brand"] [itemprop="name"], [itemprop="brand"], [class*="brand"], [class*="shop"]')?.textContent.trim() || null;
          if (!result.image) result.image = document.querySelector('img')?.src || null;
          result.url = window.location.href;
          result._platform = platform;

          if (!result.name && !result.price) {
            return JSON.stringify({ error: true, message: 'No product data found. Ensure this is a product detail page and the page has fully loaded.' });
          }
          return JSON.stringify(result);
        }

        let reviews = [];

        // Strategy 1: JSON-LD Review[], matching ecommerce-reviews.
        const reviewProduct = productLds.find(l => l.review);
        if (reviewProduct?.review) {
          const ldReviews = Array.isArray(reviewProduct.review) ? reviewProduct.review : [reviewProduct.review];
          reviews = ldReviews.slice(0, maxResults).map(r => ({
            reviewer: r.author?.name || (typeof r.author === 'string' ? r.author : null),
            rating: parseNumber(r.reviewRating?.ratingValue),
            date: r.datePublished || null,
            title: r.name || null,
            body: r.reviewBody || null,
            verified: null,
            helpful_votes: null
          }));
        }

        // Strategy 2: generic microdata and review containers, matching the original pack.
        if (reviews.length === 0) {
          const genericCards = Array.from(document.querySelectorAll('[itemprop="review"]'));
          if (genericCards.length > 0) {
            reviews = genericCards.slice(0, maxResults).map(card => ({
              reviewer: card.querySelector('[itemprop="author"]')?.textContent.trim() || null,
              rating: parseNumber(card.querySelector('[itemprop="ratingValue"]')?.getAttribute('content') || card.querySelector('[itemprop="ratingValue"]')?.textContent),
              date: card.querySelector('[itemprop="datePublished"]')?.getAttribute('content') || card.querySelector('[itemprop="datePublished"]')?.textContent.trim() || null,
              title: card.querySelector('[itemprop="name"]')?.textContent.trim() || null,
              body: card.querySelector('[itemprop="description"], [itemprop="reviewBody"]')?.textContent.trim() || null,
              verified: null,
              helpful_votes: null
            }));
          }
        }
        if (reviews.length === 0) {
          const cards = Array.from(document.querySelectorAll('.review-item, .review-card, .product-review, [class*="comment-item"]'));
          reviews = cards.slice(0, maxResults).map(card => ({
            reviewer: card.querySelector('[class*="name"], [class*="author"], [class*="user"]')?.textContent.trim() || null,
            rating: parseNumber(card.querySelector('[class*="rating"], [class*="score"]')?.textContent),
            date: card.querySelector('time, [class*="date"]')?.textContent.trim() || null,
            title: card.querySelector('h3, h4, [class*="title"]')?.textContent.trim() || null,
            body: card.querySelector('p, [class*="body"], [class*="content"], [class*="text"]')?.textContent.trim() || null,
            verified: null,
            helpful_votes: null
          })).filter(review => review.body);
        }
        if (reviews.length === 0) {
          return JSON.stringify({ error: true, message: 'No reviews found on this page. Navigate to the product reviews section or reviews page first.' });
        }
        return JSON.stringify({ count: reviews.length, reviews });
      } catch(e) {
        return JSON.stringify({ error: true, message: e.message });
      }
    })()
    """
    js = (
        js_template.replace("OPERATION", args.operation)
        .replace("PLATFORM", args.platform)
        .replace("MAX_RESULTS", str(max_results))
    )
    print(js)


if __name__ == "__main__":
    main()
