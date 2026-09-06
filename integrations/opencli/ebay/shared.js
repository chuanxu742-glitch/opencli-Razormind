// Managed by opencli-admin; official eBay Browse read-only adapter.
import { ArgumentError, AuthRequiredError, CommandExecutionError } from '@jackwener/opencli/errors';
const SCOPE = 'https://api.ebay.com/oauth/api_scope';
function options(kwargs) {
  const environment = kwargs.environment ?? 'production';
  if (!['production', 'sandbox'].includes(environment)) throw new ArgumentError('eBay environment must be production or sandbox');
  const marketplace = kwargs.marketplace ?? 'EBAY_US';
  if (marketplace !== 'EBAY_US') throw new ArgumentError('This adapter currently supports EBAY_US only');
  return { environment, marketplace, origin: environment === 'sandbox' ? 'https://api.sandbox.ebay.com' : 'https://api.ebay.com' };
}
function integer(value, fallback, min, max, label) {
  const number = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(number) || number < min || number > max) throw new ArgumentError('Invalid eBay ' + label);
  return number;
}
async function request(url, init, phase) {
  let response;
  try {
    response = await fetch(url, { ...init, redirect: 'error', signal: AbortSignal.timeout(30000) });
  } catch {
    // Never include exception URLs, request headers, or vendor response bodies.
    throw new CommandExecutionError('eBay ' + phase + ' transport failed');
  }
  if (response.status === 401 || response.status === 403) throw new AuthRequiredError('ebay', 'eBay ' + phase + ' authorization rejected (HTTP ' + response.status + ')');
  if (response.status === 429) throw new CommandExecutionError('eBay ' + phase + ' rate limited (HTTP 429)');
  if (!response.ok) throw new CommandExecutionError('eBay ' + phase + ' failed (HTTP ' + response.status + ')');
  let data;
  try { data = await response.json(); } catch { throw new CommandExecutionError('eBay ' + phase + ' returned invalid JSON'); }
  if (!data || Array.isArray(data) || typeof data !== 'object' || data.error || data.error_description ||
      (data.errors !== undefined && (!Array.isArray(data.errors) || data.errors.length))) {
    throw new CommandExecutionError('eBay ' + phase + ' returned an error response');
  }
  return data;
}
async function accessToken(config) {
  const prefix = config.environment === 'sandbox' ? 'EBAY_SANDBOX_' : 'EBAY_';
  const clientId = process.env[prefix + 'CLIENT_ID'];
  const clientSecret = process.env[prefix + 'CLIENT_SECRET'];
  if (!clientId?.trim() || !clientSecret?.trim()) throw new AuthRequiredError('ebay', 'Set ' + prefix + 'CLIENT_ID and ' + prefix + 'CLIENT_SECRET in the execution node environment');
  const data = await request(config.origin + '/identity/v1/oauth2/token', {
    method: 'POST',
    headers: { Authorization: 'Basic ' + Buffer.from(clientId + ':' + clientSecret).toString('base64'), 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'client_credentials', scope: SCOPE }).toString(),
  }, 'token');
  if (typeof data.access_token !== 'string' || !data.access_token || /[\r\n]/.test(data.access_token)) throw new CommandExecutionError('eBay token response omitted a usable access token');
  return data.access_token;
}
function nextPage(value, config) {
  if (value === undefined || value === null) return null;
  try {
    const url = new URL(value);
    if (url.origin !== config.origin || url.username || url.password || url.hash || url.pathname !== '/buy/browse/v1/item_summary/search') throw new Error();
    return url.toString();
  } catch { throw new CommandExecutionError('eBay returned an unsafe pagination URL'); }
}
function item(raw, config, sourceUrl, expectedId) {
  if (!raw || typeof raw !== 'object' || !/^v1\|\d+\|\d+$/.test(raw.itemId ?? '') ||
      typeof raw.title !== 'string' || !raw.title.trim()) throw new CommandExecutionError('eBay returned an invalid item');
  if (expectedId && raw.itemId !== expectedId) throw new CommandExecutionError('eBay returned a different item identity');
  try {
    const url = new URL(raw.itemWebUrl);
    const domain = config.environment === 'sandbox' ? 'sandbox.ebay.com' : 'ebay.com';
    const [, listing, variation] = raw.itemId.split('|');
    const urlListing = url.pathname.match(/^\/itm\/(?:[^/]+\/)?(\d+)\/?$/)?.[1];
    const urlVariations = url.searchParams.getAll('var');
    if (urlListing !== listing || urlVariations.length > 1 ||
        (urlVariations.length === 1 && urlVariations[0] !== variation)) throw new Error();
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !(url.hostname === domain || url.hostname.endsWith('.' + domain)) ||
        (config.environment === 'production' && (url.hostname === 'sandbox.ebay.com' || url.hostname.endsWith('.sandbox.ebay.com'))) ||
        !/^\/itm\/(?:[^/]+\/)?\d+\/?$/.test(url.pathname)) throw new Error();
  } catch { throw new CommandExecutionError('eBay returned an invalid item web URL'); }
  if (raw.price !== undefined && (!raw.price || typeof raw.price !== 'object' ||
      !/^\d+(?:\.\d+)?$/.test(String(raw.price.value)) || !/^[A-Z]{3}$/.test(raw.price.currency ?? ''))) {
    throw new CommandExecutionError('eBay returned an invalid item price');
  }
  return { ...raw, marketplace: config.marketplace, environment: config.environment,
    source_url: sourceUrl, fetched_at: new Date().toISOString(), strategy: 'public' };
}
export async function collectSearch(kwargs) {
  const config = options(kwargs);
  const query = String(kwargs.query ?? '').trim();
  if (!query || query.length > 100 || query.includes('*')) throw new ArgumentError('eBay query must contain 1-100 characters without wildcards');
  const limit = integer(kwargs.limit, 20, 1, 200, 'limit');
  const offset = integer(kwargs.offset, 0, 0, 9999, 'offset');
  if (offset % limit) throw new ArgumentError('eBay offset must be a multiple of limit');
  const url = new URL(config.origin + '/buy/browse/v1/item_summary/search');
  url.search = new URLSearchParams({ q: query, limit: String(limit), offset: String(offset) }).toString();
  const token = await accessToken(config);
  const data = await request(url.toString(), { headers: { Authorization: 'Bearer ' + token, 'X-EBAY-C-MARKETPLACE-ID': config.marketplace } }, 'search');
  if (!Number.isInteger(data.total) || data.total < 0 ||
      (data.itemSummaries !== undefined && !Array.isArray(data.itemSummaries)) ||
      (data.total > 0 && data.itemSummaries === undefined)) throw new CommandExecutionError('eBay search response has no valid result collection');
  const pagination = { next: nextPage(data.next, config), offset, limit, total: data.total };
  // One API page per call; offset and the validated next URL expose continuation.
  return (data.itemSummaries ?? []).map(raw => ({ ...item(raw, config, url.toString()), pagination }));
}
export async function collectProduct(kwargs) {
  const config = options(kwargs);
  const id = String(kwargs.input ?? '');
  if (!/^v1\|\d+\|\d+$/.test(id)) throw new ArgumentError('eBay product requires a REST itemId: v1|listing|variation');
  const url = config.origin + '/buy/browse/v1/item/' + encodeURIComponent(id);
  const token = await accessToken(config);
  const data = await request(url, { headers: { Authorization: 'Bearer ' + token, 'X-EBAY-C-MARKETPLACE-ID': config.marketplace } }, 'product');
  return [item(data, config, url, id)];
}
