// Amazon marketplace locale parsing maintained by opencli-admin.
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
