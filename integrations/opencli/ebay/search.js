import { cli } from '@jackwener/opencli/registry';
import { collectSearch } from './shared.js';
cli({
  "site": "ebay",
  "name": "search",
  "access": "read",
  "description": "eBay Browse one-page keyword search",
  "domain": "ebay.com",
  "strategy": "public",
  "browser": false,
  "navigateBefore": false,
  "args": [
    {
      "name": "query",
      "type": "str",
      "positional": true,
      "required": true,
      "help": "Keywords (max 100 characters)"
    },
    {
      "name": "limit",
      "type": "int",
      "default": 20,
      "help": "Items per page (1-200)"
    },
    {
      "name": "offset",
      "type": "int",
      "default": 0,
      "help": "Page offset, a multiple of limit (0-9999)"
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
  func: collectSearch
});
