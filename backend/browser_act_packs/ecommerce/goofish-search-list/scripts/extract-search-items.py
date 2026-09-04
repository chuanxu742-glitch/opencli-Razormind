import argparse
import sys

def main():
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    js = f"""
    (function() {{
      try {{
        var clean = function(value) {{
          return (value || '').replace(/\\s+/g, ' ').trim() || null;
        }};
        var text = function(el) {{
          return el ? clean(el.textContent) : null;
        }};
        var items = Array.from(document.querySelectorAll('a[class*="feeds-item-wrap"]'));
        if (items.length === 0) {{
          items = Array.from(document.querySelectorAll('a[href*="/item"]')).filter(function(item) {{
            return /[?&]id=\\d+/.test(item.getAttribute('href') || '');
          }});
        }}
        if (items.length === 0) {{
          return JSON.stringify({{ error: true, message: 'No search result items found — page may not have loaded or search returned no results' }});
        }}
        var result = [];
        for (var i = 0; i < items.length; i++) {{
          var item = items[i];
          var href = item.getAttribute('href') || '';
          var itemUrl = href;
          try {{
            itemUrl = new URL(href, window.location.href).href;
          }} catch (_) {{}}
          var itemQuery = itemUrl.match(/[?&]id=([^&#]+)/);
          var catQuery = itemUrl.match(/[?&]categoryId=([^&#]+)/);

          var titleEl = item.querySelector('[class*="row1-wrap-title"], [title]');
          var title = titleEl ? clean(titleEl.getAttribute('title')) || text(titleEl) : text(item);
          var imgEl = item.querySelector('img[class*="feeds-image"], img');
          var imageUrl = imgEl ? (imgEl.currentSrc || imgEl.src || imgEl.getAttribute('data-src')) : null;

          var numEl = item.querySelector('[class*="number--"]');
          var decEl = item.querySelector('[class*="decimal--"]');
          var price = clean((numEl ? numEl.textContent : '') + (decEl ? decEl.textContent : ''));
          if (!price) {{
            price = text(item.querySelector('[class*="price--"], [class*="price"]'));
          }}

          var tagEl = item.querySelector('[class*="row2-wrap"]');
          var serviceTag = text(tagEl);
          var descEl = item.querySelector('[class*="price-desc"]');
          var priceDesc = text(descEl);
          var locEl = item.querySelector('[class*="seller-text--"], [class*="location"]');
          var location = text(locEl);

          result.push({{
            item_id: itemQuery ? decodeURIComponent(itemQuery[1]) : null,
            category_id: catQuery ? decodeURIComponent(catQuery[1]) : null,
            item_url: itemUrl,
            title: title,
            image_url: imageUrl,
            price: price,
            service_tag: serviceTag,
            price_desc: priceDesc,
            location: location
          }});
        }}
        return JSON.stringify({{ items: result, count: result.length }});
      }} catch(e) {{
        return JSON.stringify({{ error: true, message: e.message }});
      }}
    }})()
    """
    print(js)

if __name__ == '__main__':
    main()
