import { cli } from '@jackwener/opencli/registry';
import { collectProduct } from './shared.js';
cli({
  "site": "ebay",
  "name": "product",
  "access": "read",
  "description": "eBay Browse REST item details",
  "domain": "ebay.com",
  "strategy": "public",
  "browser": false,
  "navigateBefore": false,
  "args": [
    {
      "name": "input",
      "type": "str",
      "positional": true,
      "required": true,
      "help": "REST itemId, including variation: v1|listing|variation"
    },
    {
      "name": "marketplace",
      "default": "EBAY_US",
      "choices": [
        "EBAY_US"
      ],
      "help": "Explicit marketplace"
    },
    {
      "name": "environment",
      "default": "production",
      "choices": [
        "production",
        "sandbox"
      ],
      "help": "Isolated eBay API environment"
    }
  ],
  "columns": [
    "itemId",
    "title",
    "price",
    "itemWebUrl",
    "marketplace",
    "environment"
  ],
  func: collectProduct
});
