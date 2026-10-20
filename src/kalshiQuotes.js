/**
 * pmxt's Kalshi orderbooks are often empty or stub 50/50.
 * Enrich matched legs from Kalshi's public market endpoint (yes/no ask dollars).
 */

const KALSHI_API = 'https://api.elections.kalshi.com/trade-api/v2';

export function resolveKalshiTicker(outcome = {}) {
  const raw = outcome.ticker || outcome.marketId || outcome.yesId || '';
  return String(raw)
    .replace(/-(YES|NO)$/i, '')
    .trim()
    .toUpperCase();
}

function dollarsToCents(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return null;
  // Public API returns dollars (0–1). Legacy cents (0–100) pass through.
  return n <= 1.0001 ? Number((n * 100).toFixed(2)) : Number(n.toFixed(2));
}

export async function fetchKalshiQuote(ticker, fetchFn = fetch) {
  if (!ticker) return null;
  const url = `${KALSHI_API}/markets/${encodeURIComponent(ticker)}`;
  const res = await fetchFn(url);
  if (!res.ok) return null;
  const body = await res.json();
  const market = body?.market;
  if (!market) return null;

  const yesPrice = dollarsToCents(market.yes_ask_dollars ?? market.yes_ask);
  const noPrice = dollarsToCents(market.no_ask_dollars ?? market.no_ask);
  if (yesPrice == null || noPrice == null) return null;

  return {
    yesPrice,
    noPrice,
    volume: Number(market.volume_fp || market.volume || 0) || 0,
    liquidity: Number(market.open_interest_fp || market.open_interest || 0) || 0,
    quoteSource: 'kalshi-public',
  };
}

async function mapPool(items, concurrency, fn) {
  const out = new Array(items.length);
  let i = 0;
  async function worker() {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx], idx);
    }
  }
  const n = Math.min(concurrency, Math.max(1, items.length));
  await Promise.all(Array.from({ length: n }, () => worker()));
  return out;
}

/**
 * Mutates match.kalshi in place with live ask prices when available.
 * @returns {{ enriched: number, failed: number }}
 */
export async function enrichMatchKalshiQuotes(matches, opts = {}) {
  const concurrency = opts.concurrency ?? 10;
  const fetchFn = opts.fetchFn || fetch;
  const byTicker = new Map();

  for (const m of matches || []) {
    const t = resolveKalshiTicker(m.kalshi);
    if (t && !byTicker.has(t)) byTicker.set(t, m.kalshi);
  }

  const tickers = [...byTicker.keys()];
  const quotes = new Map();
  let failed = 0;

  await mapPool(tickers, concurrency, async (ticker) => {
    try {
      const q = await fetchKalshiQuote(ticker, fetchFn);
      if (q) quotes.set(ticker, q);
      else failed += 1;
    } catch {
      failed += 1;
    }
  });

  let enriched = 0;
  for (const m of matches || []) {
    const t = resolveKalshiTicker(m.kalshi);
    const q = t ? quotes.get(t) : null;
    if (!q || !m.kalshi) continue;
    m.kalshi.yesPrice = q.yesPrice;
    m.kalshi.noPrice = q.noPrice;
    if (q.volume) m.kalshi.volume = q.volume;
    if (q.liquidity) m.kalshi.liquidity = q.liquidity;
    m.kalshi.quoteSource = q.quoteSource;
    if (!m.kalshi.ticker) m.kalshi.ticker = t;
    enriched += 1;
  }

  return { enriched, failed, tickers: tickers.length };
}
