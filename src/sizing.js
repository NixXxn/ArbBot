/**
 * Position sizing: CONSERVATIVE | KELLY | YOLO
 *
 * Kelly for synthetic arb (pay `cost` cents to lock ~100¢ payout):
 *   b = net odds = profit / cost
 *   p = success probability (match confidence × base fill confidence)
 *   f* = (b*p - (1-p)) / b   = (p - cost/100) / (profit/100)
 * Then apply fractional Kelly and bankroll caps.
 */

export function clamp(n, lo, hi) {
  return Math.min(hi, Math.max(lo, n));
}

/**
 * @param {object} opts
 * @param {'CONSERVATIVE'|'KELLY'|'YOLO'|string} opts.mode
 * @param {number} opts.bankrollCents  min(poly,kalshi) in cents
 * @param {number} [opts.fixedCents]   CONSERVATIVE fixed size
 * @param {number} [opts.profitCents]  locked edge in cents (e.g. 2.5)
 * @param {number} [opts.totalCostCents] pair cost in cents (e.g. 97.5)
 * @param {number} [opts.similarity]   match score 0–1
 * @param {number} [opts.kellyFraction] 0.25–1 (default half-Kelly 0.5)
 * @param {number} [opts.kellyMaxBankrollPct] max share of bankroll (default 0.25)
 * @param {number} [opts.kellyMinCents] floor (default 25)
 * @param {number} [opts.baseFillProb] P(both legs fill) default 0.92
 */
export function sizeTradeCents(opts) {
  const mode = String(opts.mode || 'CONSERVATIVE').toUpperCase();
  const bankroll = Math.max(0, Math.floor(Number(opts.bankrollCents) || 0));
  if (bankroll < 10) return { cents: 0, mode, reason: 'bankroll_too_low' };

  if (mode === 'YOLO') {
    const cents = Math.max(0, Math.floor(bankroll * 0.95));
    return { cents, mode, fraction: 0.95, reason: 'yolo_95pct' };
  }

  if (mode === 'CONSERVATIVE' || mode === 'FIXED') {
    const fixed = Math.max(0, Math.floor(Number(opts.fixedCents) || 100));
    const cents = Math.min(fixed, bankroll);
    return { cents, mode: 'CONSERVATIVE', fraction: cents / bankroll, reason: 'fixed' };
  }

  // KELLY (default middle ground)
  const cost = Math.max(1, Number(opts.totalCostCents) || 99);
  const profit = Math.max(0.01, Number(opts.profitCents) || (100 - cost));
  const b = profit / cost; // net fractional odds on stake
  const similarity = clamp(Number(opts.similarity) || 0.7, 0.5, 0.99);
  const baseFill = clamp(Number(opts.baseFillProb) || 0.92, 0.5, 0.99);
  // Blend: match quality × execution confidence
  const p = clamp(similarity * baseFill + (1 - baseFill) * 0.5, 0.5, 0.99);
  const q = 1 - p;
  let fStar = (b * p - q) / b;
  if (!Number.isFinite(fStar) || fStar <= 0) {
    // Edge not enough vs confidence — fall back to tiny conservative clip
    const fallback = Math.min(Number(opts.fixedCents) || 50, Math.floor(bankroll * 0.05));
    return { cents: Math.max(0, fallback), mode: 'KELLY', fraction: 0, p, b, reason: 'kelly_nonpositive' };
  }

  const frac = clamp(Number(opts.kellyFraction) || 0.5, 0.05, 1);
  const maxPct = clamp(Number(opts.kellyMaxBankrollPct) || 0.25, 0.02, 0.95);
  const minCents = Math.max(1, Math.floor(Number(opts.kellyMinCents) || 25));
  const f = Math.min(fStar * frac, maxPct);
  let cents = Math.floor(bankroll * f);
  cents = clamp(cents, Math.min(minCents, bankroll), Math.floor(bankroll * maxPct));
  return {
    cents,
    mode: 'KELLY',
    fraction: f,
    fStar,
    p,
    b,
    reason: 'kelly',
  };
}

/** Build venue deep links for manual cross-check. */
export function polymarketEventUrl(outcome = {}) {
  if (outcome.eventUrl || outcome.url) return outcome.eventUrl || outcome.url;
  const slug = outcome.eventSlug || outcome.slug;
  if (slug) return `https://polymarket.com/event/${slug}`;
  // Fallback: search by title
  const q = outcome.question || outcome.marketTitle || outcome.title;
  if (q) return `https://polymarket.com/search?_q=${encodeURIComponent(q)}`;
  return null;
}

export function kalshiEventUrl(outcome = {}) {
  if (outcome.eventUrl || outcome.url) return outcome.eventUrl || outcome.url;
  const ticker = outcome.ticker || outcome.eventTicker || outcome.marketId;
  if (ticker) {
    const t = String(ticker).toLowerCase();
    // Kalshi market pages often: /markets/{series}/{event}/{ticker}
    // Search is the reliable cross-check landing page.
    return `https://kalshi.com/markets/${t}`;
  }
  const q = outcome.question || outcome.marketTitle || outcome.title;
  if (q) return `https://kalshi.com/search?query=${encodeURIComponent(q)}`;
  return null;
}
