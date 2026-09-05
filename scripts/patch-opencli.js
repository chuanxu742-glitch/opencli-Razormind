#!/usr/bin/env node
/**
 * Post-install patch for @jackwener/opencli (CommonJS, works on Node 18+).
 *
 * Usage:
 *   node patch-opencli.js               # patch the default global install
 *   node patch-opencli.js /opt/my-dir   # patch a specific npm prefix dir
 *
 * Adds two env-var hooks that the published package lacks:
 *
 *   OPENCLI_DAEMON_LISTEN  (daemon.js)
 *     Default: 127.0.0.1 — set to 0.0.0.0 so the API container can reach
 *     the daemon running inside the chrome-N container.
 *
 *   OPENCLI_DAEMON_HOST  (daemon-client.js + browser/bridge.js)
 *     Default: 127.0.0.1 — set to chrome-1 (or chrome-N) so the CLI in the
 *     API container contacts the remote daemon instead of spawning one locally.
 *
 * v1.7.0 migration: mcp.js renamed to browser/bridge.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const staged = new Map();

function readPlanned(filePath) {
  return staged.has(filePath) ? staged.get(filePath) : fs.readFileSync(filePath, 'utf8');
}

function resolvePackageDir(prefixDir) {
  if (prefixDir) {
    // Explicit prefix supplied (e.g. /opt/opencli-bridge)
    const candidate = path.join(prefixDir, 'lib', 'node_modules', '@jackwener', 'opencli');
    if (fs.existsSync(candidate)) return candidate;
    // Some npm versions omit the 'lib/' level
    const candidate2 = path.join(prefixDir, 'node_modules', '@jackwener', 'opencli');
    if (fs.existsSync(candidate2)) return candidate2;
    throw new Error('Could not find @jackwener/opencli under prefix: ' + prefixDir);
  }
  try {
    return path.dirname(require.resolve('@jackwener/opencli/package.json'));
  } catch (_) {
    // Fallback: ask npm where its global root is (works with NodeSource installs)
    const { execSync } = require('child_process');
    const npmRoot = execSync('npm root -g').toString().trim();
    return path.join(npmRoot, '@jackwener', 'opencli');
  }
}

function patch(filePath, search, replace, label) {
  if (!fs.existsSync(filePath)) {
    console.log('  [skip] ' + label + ': file not found ' + filePath);
    return;
  }
  let content = readPlanned(filePath);
  if (label === 'execution.js: managed web-adapter CDP routing' &&
      content.includes('OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2')) {
    console.log('  [skip] ' + label + ' superseded by V2');
    return;
  }
  if (content.includes(replace.slice(0, 40))) {
    console.log('  [skip] ' + label + ' already patched');
    return;
  }
  if (!content.includes(search)) {
    console.error('  [warn] ' + label + ': search string not found in ' + filePath);
    return;
  }
  content = content.replace(search, replace);
  staged.set(filePath, content);
  console.log('  [ok]   ' + label);
}

const prefixDir = process.argv[2] || null;
const pkgDir = resolvePackageDir(prefixDir);
console.log('Patching opencli at ' + pkgDir + ' ...');

// ── 1. daemon.js: honour OPENCLI_DAEMON_LISTEN ───────────────────────────────
patch(
  path.join(pkgDir, 'dist', 'src', 'daemon.js'),
  "httpServer.listen(PORT, '127.0.0.1', () => {",
  "const DAEMON_LISTEN = process.env.OPENCLI_DAEMON_LISTEN ?? '127.0.0.1';\nhttpServer.listen(PORT, DAEMON_LISTEN, () => {",
  'daemon.js: OPENCLI_DAEMON_LISTEN'
);

// ── 2. daemon-client.js: honour OPENCLI_DAEMON_HOST ─────────────────────────
patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'daemon-transport.js'),
  'const DAEMON_URL = `http://127.0.0.1:${DAEMON_PORT}`;',
  "const DAEMON_HOST = process.env.OPENCLI_DAEMON_HOST ?? '127.0.0.1';\nconst DAEMON_URL = `http://${DAEMON_HOST}:${DAEMON_PORT}`;",
  'daemon-client.js: OPENCLI_DAEMON_HOST'
);

// ── 3. browser/bridge.js: skip local auto-spawn when daemon is remote ────────
// v1.7.0+: _ensureDaemon moved from mcp.js to browser/bridge.js.
// When OPENCLI_DAEMON_HOST is set to a remote address, probe that daemon
// instead of spawning a process inside the caller's container.
patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'),
  "import { ensureBrowserBridgeReady } from './daemon-lifecycle.js';",
  "// OPENCLI_ADMIN_REMOTE_DAEMON_IMPORT_V1\nimport { ensureBrowserBridgeReady } from './daemon-lifecycle.js';\nimport { fetchDaemonStatus } from './daemon-transport.js';",
  'browser/bridge.js: import remote daemon status probe'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'),
  `    async _ensureDaemon(timeoutSeconds, contextId, preferredContextId) {
        await ensureBrowserBridgeReady({
            timeoutSeconds: timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000),
            contextId,
            preferredContextId,
        });
    }`,
  `    // OPENCLI_ADMIN_REMOTE_DAEMON_ROUTE_V2
    async _ensureDaemon(timeoutSeconds, contextId, preferredContextId) {
        const daemonHost = process.env.OPENCLI_DAEMON_HOST;
        if (daemonHost && daemonHost !== '127.0.0.1' && daemonHost !== 'localhost') {
            const remoteStatus = await fetchDaemonStatus({
                timeout: (timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000)) * 1000,
                contextId,
                preferredContextId,
            });
            if (!remoteStatus) {
                throw new Error('Remote Browser Bridge daemon at ' + daemonHost + ' is not reachable. Ensure BROWSER_BRIDGE_ENABLED=true on the chrome container.');
            }
            return;
        }
        await ensureBrowserBridgeReady({
            timeoutSeconds: timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000),
            contextId,
            preferredContextId,
        });
    }`,
  'browser/bridge.js: route remote daemon health checks'
);

// ── 4. execution.js: honour explicit CDP endpoint for web adapters ──────────
// OpenCLI 1.8.7 only reads OPENCLI_CDP_ENDPOINT inside the Electron branch.
// A normal web adapter therefore silently falls back to Browser Bridge and can
// escape Admin's selected profile. Managed acquisition must fail closed at the
// requested endpoint instead.
patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  `            let cdpEndpoint;
            if (electron) {
                // Electron apps: respect manual endpoint override, then try auto-detect
                const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
                if (manualEndpoint) {
                    const port = Number(new URL(manualEndpoint).port);
                    if (!await probeCDP(port)) {
                        throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the app is running with --remote-debugging-port and the endpoint is correct.');
                    }
                    cdpEndpoint = manualEndpoint;
                }
                else {
                    cdpEndpoint = await resolveElectronEndpoint(cmd.site);
                }
            }`,
  `            // OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V1
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
            let cdpEndpoint;
            if (manualEndpoint) {
                const port = Number(new URL(manualEndpoint).port);
                if (!await probeCDP(port)) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  'execution.js: managed web-adapter CDP routing'
);

// Upgrade the first managed-routing patch so remote/container endpoints are
// probed at their real host. OpenCLI's probeCDP(port) is intentionally local
// to Electron discovery and always calls 127.0.0.1.
patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  `            // OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V1
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
            let cdpEndpoint;
            if (manualEndpoint) {
                const port = Number(new URL(manualEndpoint).port);
                if (!await probeCDP(port)) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  `// OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT?.trim();
            let cdpEndpoint;
            if (manualEndpoint) {
                let reachable = true;
                try {
                    const probeUrl = new URL(manualEndpoint);
                    if (probeUrl.protocol === 'http:' || probeUrl.protocol === 'https:') {
                        probeUrl.pathname = probeUrl.pathname.replace(/\\/$/, '') + '/json';
                        const response = await fetch(probeUrl, { signal: AbortSignal.timeout(3000) });
                        reachable = response.ok;
                    }
                }
                catch {
                    reachable = false;
                }
                if (!reachable) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  'execution.js: probe the actual managed CDP host'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  '            const BrowserFactory = getBrowserFactory(cmd.site);',
  '            // OPENCLI_ADMIN_FACTORY_SELECTION_V1\n            const BrowserFactory = getBrowserFactory(cmd.site, { cdpEndpoint });',
  'execution.js: select CDP factory for managed endpoint'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'runtime.js'),
  `export function getBrowserFactory(site) {
    if (site && isElectronApp(site))
        return CDPBridge;
    return BrowserBridge;
}`,
  `// OPENCLI_ADMIN_RUNTIME_FACTORY_V1
export function getBrowserFactory(site, opts = {}) {
    if (opts.cdpEndpoint || (site && isElectronApp(site)))
        return CDPBridge;
    return BrowserBridge;
}`,
  'runtime.js: explicit endpoint selects CDPBridge'
);

const executionSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'execution.js'));
const runtimeSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'runtime.js'));
const bridgeSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'));
if (!executionSource.includes('OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2') ||
    !executionSource.includes('OPENCLI_ADMIN_FACTORY_SELECTION_V1') ||
    !runtimeSource.includes('OPENCLI_ADMIN_RUNTIME_FACTORY_V1') ||
    !bridgeSource.includes('OPENCLI_ADMIN_REMOTE_DAEMON_ROUTE_V2')) {
  throw new Error('Managed CDP routing patch verification failed');
}

// The installer downloads this file alone. Keep generated adapters embedded;
// preflight all supported upstream bytes before committing any staged changes.
const upstream = {
  'amazon/shared.js': 'b92b01e9326973bae112e79ade8410d92c5c6717fb0a912d71f50f3042da5d65',
  'amazon/search.js': 'b73556cc5450f7d7882566c15c76483050bdb3f983713969a27cf19e855bd940',
  'amazon/product.js': 'eaa14c9db0f4de6820fdd5d639f30ed4779ee25c7cdebcf60fce4fe53a9f5c8e',
  'amazon/offer.js': 'e063f583a7758414eb68cd316b09816008e27fc5fe01d199fe5d3c1922ad7696',
  'amazon/discussion.js': '2ba9ca614612160bb9629b474023b99282f2b4fb3330aa6d0274111fc06f4059',
  'amazon/rankings.js': '35afd4c94db3a36a636a02945a652e6adebc2dfc96503d5c5898c7d1dfec74d7',
  'taobao/search.js': 'bc0a4a3b2ca29ae0107769dc5bf4855bac775758787ae9ef626390a867e077ab',
  'coupang/utils.js': '55a66fe7841f53424ac299e750b4baa6337e03e570c50899f64522272f0b7b28',
  'coupang/search.js': 'f76bdcc883e8c5de84c28218e7c3857834dc2f6268f9cb071c75ce4b72f69749',
  'coupang/product.js': 'fd6d40aa1d6023d43fdc8bf432329798ec81b4a362e2d3c26da5e837563ad499',
};
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const clisDir = path.join(pkgDir, 'clis');
const receiptPath = path.join(pkgDir, '.opencli-admin-ecommerce.json');

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2) throw new Error('Unexpected upstream patch anchor: ' + before);
  return source.replace(before, after);
}

const localeSource = String.raw`// Managed by opencli-admin; emitted by patch-opencli.js.
const markets = {
  'amazon.com': ['USD', 'en-US'], 'amazon.ca': ['CAD', 'en-CA'],
  'amazon.com.mx': ['MXN', 'es-MX'], 'amazon.com.br': ['BRL', 'pt-BR'],
  'amazon.co.uk': ['GBP', 'en-GB'], 'amazon.de': ['EUR', 'de-DE'],
  'amazon.fr': ['EUR', 'fr-FR'], 'amazon.it': ['EUR', 'it-IT'],
  'amazon.es': ['EUR', 'es-ES'], 'amazon.nl': ['EUR', 'nl-NL'],
  'amazon.com.be': ['EUR', 'nl-BE'], 'amazon.ie': ['EUR', 'en-IE'],
  'amazon.pl': ['PLN', 'pl-PL'], 'amazon.se': ['SEK', 'sv-SE'],
  'amazon.com.tr': ['TRY', 'tr-TR'], 'amazon.ae': ['AED', 'ar-AE'],
  'amazon.sa': ['SAR', 'ar-SA'], 'amazon.eg': ['EGP', 'ar-EG'],
  'amazon.co.za': ['ZAR', 'en-ZA'], 'amazon.in': ['INR', 'en-IN'],
  'amazon.co.jp': ['JPY', 'ja-JP'], 'amazon.com.au': ['AUD', 'en-AU'],
  'amazon.sg': ['SGD', 'en-SG'],
};
function marketFor(input) {
  try {
    const host = new URL(input).hostname.toLowerCase().replace(/\.$/, '');
    return Object.entries(markets).find(([domain]) => host === domain || host.endsWith('.' + domain))?.[1];
  } catch { return undefined; }
}
function clean(value) {
  return typeof value === 'string' ? value.replace(/[\u00a0\u202f]/g, ' ').replace(/\s+/g, ' ').trim() : '';
}
function numberValue(raw, locale) {
  let text = raw.replace(/\s/g, '');
  if (!/^\d[\d.,]*$/.test(text)) return null;
  const explicitDecimal = text.match(/[.,](?=\d{1,2}$)/)?.[0];
  let decimal;
  // Display-language decimal evidence wins; marketplace resolves grouping.
  if (explicitDecimal) {
    decimal = explicitDecimal;
  } else if (locale) {
    decimal = new Intl.NumberFormat(locale).formatToParts(1.1).find(p => p.type === 'decimal')?.value;
  } else if (text.includes('.') && text.includes(',')) {
    decimal = text.lastIndexOf('.') > text.lastIndexOf(',') ? '.' : ',';
  } else {
    const sep = text.includes(',') ? ',' : '.';
    const chunks = text.split(sep);
    if (chunks.length === 1) decimal = '.';
    else if (chunks.length === 2 && chunks[1].length <= 2) decimal = sep;
    else if (chunks.slice(1).every(x => x.length === 3)) decimal = sep === ',' ? '.' : ',';
    else return null;
  }
  const grouping = decimal === ',' ? '.' : ',';
  const pieces = text.split(decimal);
  if (pieces.length > 2 || (pieces[1] !== undefined && !/^\d{1,2}$/.test(pieces[1]))) return null;
  const integer = pieces[0].split(grouping);
  if (integer.length > 1 && (!/^\d{1,3}$/.test(integer[0]) || !integer.slice(1).every(x => /^\d{3}$/.test(x)))) return null;
  text = integer.join('') + (pieces[1] === undefined ? '' : '.' + pieces[1]);
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}
export function parsePriceText(text, sourceUrl) {
  const original = clean(text);
  const market = marketFor(sourceUrl);
  const tokens = '(?:CDN\\$|US\\$|CA\\$|A\\$|AU\\$|S\\$|R\\$|USD|CAD|AUD|SGD|EUR|GBP|JPY|CNY|MXN|BRL|INR|PLN|SEK|TRY|AED|SAR|EGP|ZAR|[$€£¥￥₹])';
  const amount = '(\\d(?:[\\d., ]*\\d)?)';
  const match = original.match(new RegExp('^(' + tokens + ')\\s*' + amount + '$', 'i'));
  const trailing = match ? null : original.match(new RegExp('^' + amount + '\\s*(' + tokens + ')$', 'i'));
  if (!match && !trailing) return { price_text: original || null, price_value: null, currency: null };
  const token = (match ? match[1] : trailing[2]).toUpperCase();
  const raw = match ? match[2] : trailing[1];
  const known = { 'CDN$': 'CAD', 'CA$': 'CAD', 'US$': 'USD', 'A$': 'AUD', 'AU$': 'AUD', 'S$': 'SGD', 'R$': 'BRL', '€': 'EUR', '£': 'GBP', '₹': 'INR' };
  let currency = known[token] || (/^[A-Z]{3}$/.test(token) ? token : null);
  if (token === '$' && market && ['USD','CAD','AUD','SGD','MXN'].includes(market[0])) currency = market[0];
  if ((token === '¥' || token === '￥') && market?.[0] === 'JPY') currency = 'JPY';
  // An explicit currency can differ from the store's default display currency.
  const value = numberValue(raw, market?.[1]);
  return { price_text: original || null, price_value: value, currency: value === null ? null : currency };
}
export function parseRatingValue(text) {
  const value = clean(text);
  const match = value.match(/(?<![\d.,+-])(\d(?:[.,]\d+)?)\s*(?:out of|von|sur|su|de)\s*5(?![\d.,])/i)
    || value.match(/5つ星のうち\s*(\d(?:[.,]\d+)?)(?![\d.,])/);
  const rating = match ? Number(match[1].replace(',', '.')) : null;
  return rating !== null && rating >= 0 && rating <= 5 ? rating : null;
}
export function parseReviewCount(text, sourceUrl) {
  const value = clean(text);
  const compact = value.match(/(\d+(?:[.,]\d+)?)\s*([kKmM](?![\p{L}\p{N}_])|万)/u);
  if (compact) {
    const multiplier = compact[2] === '万' ? 10000 : /m/i.test(compact[2]) ? 1000000 : 1000;
    return Math.round(Number(compact[1].replace(',', '.')) * multiplier);
  }
  const match = value.match(/\d[\d., ]*/);
  if (!match) return null;
  const number = numberValue(match[0].trim(), marketFor(sourceUrl)?.[1]);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}
`;

function patchAmazon(source, relative) {
  if (relative === 'amazon/shared.js') {
    source = replaceOnce(source,
      "import { ArgumentError, CommandExecutionError } from '@jackwener/opencli/errors';",
      "import { ArgumentError, CommandExecutionError } from '@jackwener/opencli/errors';\nimport { parsePriceText, parseRatingValue, parseReviewCount } from './admin-locale.js';\nexport { parsePriceText, parseRatingValue, parseReviewCount };");
    const start = source.indexOf('export function parsePriceText(text) {');
    const end = source.indexOf('export function extractReviewCountFromCardText', start);
    if (start < 0 || end < 0) throw new Error('Amazon parser section not found');
    source = source.slice(0, start) + source.slice(end);
    source = replaceOnce(source,
      '        return isAmazonMarketplaceHost(url.hostname) ? url.hostname : null;',
      "        return url.protocol === 'https:' && !url.username && !url.password && !url.port && isAmazonMarketplaceHost(url.hostname) ? url.hostname : null;");
    source = replaceOnce(source,
      '([A-Z0-9]{10})/i);',
      String.raw`([A-Z0-9]{10})(?=\/|[?#]|$)/i);`);
    for (const label of ['product', 'discussion']) {
      const fragment = "    const host = amazonHostFromInput(input);\n    return host ?";
      const offset = source.indexOf('export function build' + (label === 'product' ? 'Product' : 'Discussion') + 'Url');
      const at = source.indexOf(fragment, offset);
      if (at < 0) throw new Error('Amazon URL boundary not found');
      source = source.slice(0, at) + source.slice(at).replace(fragment,
        "    const host = amazonHostFromInput(input);\n    if (!host && !/^[A-Z0-9]{10}$/i.test(cleanText(input))) throw new ArgumentError('Expected a bare ASIN or an HTTPS Amazon marketplace URL');\n    return host ?");
    }
    source = replaceOnce(source,
      '    const normalized = cleanText(value);\n    const asin = extractAsin(normalized);',
      "    const normalized = cleanText(value);\n    const candidate = normalized.startsWith('/') ? new URL(normalized, HOME_URL).toString() : normalized;\n    if (candidate && !amazonHostFromInput(candidate) && !/^[A-Z0-9]{10}$/i.test(candidate)) throw new ArgumentError('Expected an Amazon product URL');\n    const asin = extractAsin(candidate);");
    source = replaceOnce(source, '        return buildProductUrl(normalized);', '        return buildProductUrl(candidate);');
    return source;
  }
  const context = relative === 'amazon/rankings.js' ? 'context.sourceUrl' : 'sourceUrl';
  for (const [before, after] of [
    ['parsePriceText(candidate.price_text)', 'parsePriceText(candidate.price_text, ' + context + ')'],
    ['parsePriceText(payload.price_text)', 'parsePriceText(payload.price_text, ' + context + ')'],
    ['parsePriceText(cleanText(candidate.price_text) || candidate.card_text)', 'parsePriceText(cleanText(candidate.price_text) || candidate.card_text, ' + context + ')'],
    ['parseReviewCount(reviewCountText)', 'parseReviewCount(reviewCountText, ' + context + ')'],
    ['parseReviewCount(totalReviewCountText)', 'parseReviewCount(totalReviewCountText, ' + context + ')'],
  ]) {
    if (source.includes(before)) source = replaceOnce(source, before, after);
  }
  return source;
}

function patchTaobao(source) {
  source = replaceOnce(source, 'const seenTitles = new Set();', 'const seenIds = new Set();');
  source = replaceOnce(source, 'if (!title || title.length < 3 || seenTitles.has(title)) continue;\n          seenTitles.add(title);', 'if (!title || title.length < 3) continue;');
  return replaceOnce(source, '          results.push({', '          if (itemId && seenIds.has(itemId)) continue;\n          if (itemId) seenIds.add(itemId);\n          results.push({');
}

function coupangCanonicalUrl(rawUrl, productId) {
  const raw = String(rawUrl ?? '').trim();
  const expected = String(productId ?? '').trim();
  if (!raw) return /^\d{6,}$/.test(expected) ? 'https://www.coupang.com/vp/products/' + expected : '';
  try {
    const url = new URL(raw, 'https://www.coupang.com');
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !(url.hostname === 'coupang.com' || url.hostname.endsWith('.coupang.com'))) return '';
    const id = url.pathname.match(/^\/vp\/products\/(\d{6,})\/?$/)?.[1];
    if (!id || (expected && id !== expected)) return '';
    const canonical = new URL('https://www.coupang.com/vp/products/' + id);
    for (const key of ['itemId', 'vendorItemId']) {
      const values = url.searchParams.getAll(key);
      if (values.length > 1 || (values.length && !/^\d+$/.test(values[0]))) return '';
      if (values.length) canonical.searchParams.set(key, values[0]);
    }
    return canonical.toString();
  } catch { return ''; }
}

function patchCoupang(source, relative) {
  if (relative === 'coupang/utils.js') {
    const start = source.indexOf('export function canonicalizeProductUrl(');
    const end = source.indexOf('function extractTokens(', start);
    if (start < 0 || end < 0) throw new Error('Missing Coupang canonical URL boundary');
    return source.slice(0, start) + 'export ' +
      coupangCanonicalUrl.toString().replace('coupangCanonicalUrl', 'canonicalizeProductUrl') + '\n' + source.slice(end);
  }
  if (relative === 'coupang/search.js') {
    const start = source.indexOf('      const canonicalUrl = (url, productId) => {');
    const end = source.indexOf('        const normalize = (raw) => {', start);
    if (start < 0 || end < 0) throw new Error('Missing Coupang browser search URL boundary');
    // This function lives inside the upstream in-page JavaScript template.
    source = source.slice(0, start) + '      const canonicalUrl = ' +
      coupangCanonicalUrl.toString().replaceAll('\\', '\\\\') + ';\n' + source.slice(end);
    return replaceOnce(source,
      '        const url = canonicalUrl(raw.url || raw.productUrl || raw.link, productId);',
      "        const url = canonicalUrl(raw.url || raw.productUrl || raw.link, productId);\n        if (!url) throw new Error('Invalid Coupang search product URL');");
  }
  source = replaceOnce(source, '        ok: true,\n        currentProductId,',
    '        ok: true,\n        currentProductId,\n        currentUrl: location.href,');
  source = replaceOnce(source,
    '        const targetUrl = canonicalizeProductUrl(kwargs.url, productId);',
    "        const targetUrl = canonicalizeProductUrl(kwargs.url, productId);\n        if (kwargs.url && !targetUrl) throw new ArgumentError('Invalid Coupang product or variant URL');");
  source = replaceOnce(source,
    '        const actualProductId = normalizeProductId(result?.currentProductId || result.data.product_id || productId);',
    "        const actualProductId = normalizeProductId(result?.currentProductId || '');\n" +
    "        const observedUrl = canonicalizeProductUrl(result?.currentUrl, actualProductId);\n" +
    "        if (!result?.currentUrl || !observedUrl || actualProductId !== productId) throw new EmptyResultError('coupang product', 'Missing or conflicting observed product page identity');\n" +
    "        const requested = new URL(finalUrl);\n        const observed = new URL(observedUrl);\n" +
    "        for (const key of ['itemId', 'vendorItemId']) {\n" +
    "            if (requested.searchParams.has(key) && requested.searchParams.get(key) !== observed.searchParams.get(key)) throw new EmptyResultError('coupang product', 'Requested variant does not match the observed product page');\n" +
    "        }");
  return replaceOnce(source, "                url: canonicalizeProductUrl('', actualProductId) || finalUrl,",
    '                url: observedUrl,\n                source_url: result.currentUrl,');
}

const ebaySource = String.raw`// Managed by opencli-admin; official eBay Browse read-only adapter.
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
`;

function ebayOptions(name) {
  const search = name === 'search';
  return {
      site: 'ebay', name, access: 'read', description: search ? 'eBay Browse one-page keyword search' : 'eBay Browse REST item details',
      domain: 'ebay.com', strategy: 'public', browser: false, navigateBefore: false,
      args: [
        { name: search ? 'query' : 'input', type: 'str', positional: true, required: true, help: search ? 'Keywords (max 100 characters)' : 'REST itemId, including variation: v1|listing|variation' },
        ...(search ? [
          { name: 'limit', type: 'int', default: 20, help: 'Items per page (1-200)' },
          { name: 'offset', type: 'int', default: 0, help: 'Page offset, a multiple of limit (0-9999)' },
        ] : []),
        { name: 'marketplace', default: 'EBAY_US', choices: ['EBAY_US'], help: 'Explicit marketplace' },
        { name: 'environment', default: 'production', choices: ['production', 'sandbox'], help: 'Isolated eBay API environment' },
      ],
      columns: ['itemId', 'title', 'price', 'itemWebUrl', 'marketplace', 'environment'],
    };
}

function ebayCommand(name) {
  return "import { cli } from '@jackwener/opencli/registry';\n" +
    "import { " + (name === 'search' ? 'collectSearch' : 'collectProduct') + " } from './shared.js';\n" +
    'cli(' + JSON.stringify(ebayOptions(name), null, 2).replace(/\n}$/, ',\n  func: ' +
      (name === 'search' ? 'collectSearch' : 'collectProduct') + '\n}') + ');\n';
}

const generated = {
  'amazon/admin-locale.js': localeSource,
  'ebay/shared.js': ebaySource,
  'ebay/search.js': ebayCommand('search'),
  'ebay/product.js': ebayCommand('product'),
};
const patchRevision = digest(localeSource + ebaySource + ebayCommand('search') + ebayCommand('product') +
  patchAmazon.toString() + patchTaobao.toString() + coupangCanonicalUrl.toString() + patchCoupang.toString());
const receipt = fs.existsSync(receiptPath) ? JSON.parse(fs.readFileSync(receiptPath, 'utf8')) : null;
const manifestPath = path.join(pkgDir, 'cli-manifest.json');
if (receipt) {
  if (receipt.revision !== patchRevision) throw new Error('Managed ecommerce patch revision changed; reinstall the pinned OpenCLI package before upgrading');
  for (const relative of [...Object.keys(upstream), ...Object.keys(generated)]) {
    const target = path.join(clisDir, relative);
    if (!fs.existsSync(target) || digest(fs.readFileSync(target)) !== receipt.files?.[relative]) throw new Error('Managed ecommerce adapter drift: ' + relative);
  }
  if (digest(fs.readFileSync(manifestPath)) !== receipt.manifest) throw new Error('Managed CLI manifest drift');
  console.log('  [skip] ecommerce adapters already patched and verified');
} else {
  const packageVersion = JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8')).version;
  if (packageVersion !== '1.8.7') throw new Error('Ecommerce patch requires @jackwener/opencli 1.8.7');
  for (const [relative, expected] of Object.entries(upstream)) {
    const target = path.join(clisDir, relative);
    if (!fs.existsSync(target) || digest(fs.readFileSync(target)) !== expected) throw new Error('Unsupported upstream ecommerce source: ' + relative);
  }
  for (const relative of Object.keys(generated)) {
    if (fs.existsSync(path.join(clisDir, relative))) throw new Error('Refusing to overwrite an unmanaged adapter: ' + relative);
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (!Array.isArray(manifest) || manifest.some(entry => entry.site === 'ebay')) throw new Error('Unsupported upstream CLI manifest');
  for (const name of ['search', 'product']) {
    manifest.push({ ...ebayOptions(name), modulePath: 'ebay/' + name + '.js', sourceFile: 'ebay/' + name + '.js' });
  }
  const manifestOutput = JSON.stringify(manifest, null, 2) + '\n';
  staged.set(manifestPath, manifestOutput);
  const files = {};
  for (const relative of Object.keys(upstream)) {
    const target = path.join(clisDir, relative);
    const source = readPlanned(target);
    const output = relative.startsWith('amazon/') ? patchAmazon(source, relative)
      : relative.startsWith('taobao/') ? patchTaobao(source) : patchCoupang(source, relative);
    staged.set(target, output);
    files[relative] = digest(output);
  }
  for (const [relative, output] of Object.entries(generated)) {
    staged.set(path.join(clisDir, relative), output);
    files[relative] = digest(output);
  }
  staged.set(receiptPath, JSON.stringify({ revision: patchRevision, files, manifest: digest(manifestOutput) }, null, 2) + '\n');
}
// Commit only after runtime routing and every ecommerce file passed preflight.
for (const [target, content] of staged) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
}
console.log('Done.');
