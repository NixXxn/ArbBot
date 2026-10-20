/**
 * Keep Polymarket and Kalshi trading budgets aligned after P&L.
 *
 * Venues cannot wire cash to each other via API. We:
 * 1) measure available cash on each side
 * 2) compute a 50/50 target
 * 3) equalize the *tradable budget ledger* used for sizing
 * 4) surface any leftover real-world transfer the operator still needs
 */

export function availableUsd(balanceRows) {
  if (!Array.isArray(balanceRows) || balanceRows.length === 0) return null;
  const row = balanceRows.find((b) => b && (b.available != null || b.total != null)) || balanceRows[0];
  const n = Number(row.available ?? row.total ?? 0);
  return Number.isFinite(n) ? n : null;
}

/**
 * @param {number} polyUsd
 * @param {number} kalshiUsd
 * @param {{ targetRatio?: number, minDeltaUsd?: number }} [opts]
 */
export function planRebalance(polyUsd, kalshiUsd, opts = {}) {
  const poly = Math.max(0, Number(polyUsd) || 0);
  const kalshi = Math.max(0, Number(kalshiUsd) || 0);
  const total = poly + kalshi;
  const targetRatio = clamp(opts.targetRatio ?? 0.5, 0.05, 0.95);
  const minDeltaUsd = Math.max(0, Number(opts.minDeltaUsd ?? 1) || 0);
  const targetPoly = total * targetRatio;
  const targetKalshi = total - targetPoly;
  const deltaPoly = targetPoly - poly; // + means poly needs more
  const imbalanceUsd = Math.abs(deltaPoly);

  let from = null;
  let to = null;
  let transferUsd = 0;
  if (imbalanceUsd >= minDeltaUsd && total > 0) {
    if (deltaPoly > 0) {
      from = 'kalshi';
      to = 'polymarket';
      transferUsd = round2(deltaPoly);
    } else {
      from = 'polymarket';
      to = 'kalshi';
      transferUsd = round2(-deltaPoly);
    }
  }

  const equalized = {
    polymarket: round2(targetPoly),
    kalshi: round2(targetKalshi),
  };

  return {
    poly: round2(poly),
    kalshi: round2(kalshi),
    total: round2(total),
    targetPoly: equalized.polymarket,
    targetKalshi: equalized.kalshi,
    imbalanceUsd: round2(imbalanceUsd),
    from,
    to,
    transferUsd,
    balanced: transferUsd === 0,
    equalized,
    tradeCapUsd: round2(Math.min(equalized.polymarket, equalized.kalshi)),
  };
}

/** Apply cash flow to one venue (USD). */
export function applyCashFlow(balances, venue, usdDelta) {
  const next = {
    polymarket: Math.max(0, Number(balances.polymarket) || 0),
    kalshi: Math.max(0, Number(balances.kalshi) || 0),
  };
  const key = venue === 'kalshi' ? 'kalshi' : 'polymarket';
  next[key] = round2(Math.max(0, next[key] + Number(usdDelta)));
  return next;
}

/** Split total cash 50/50 (or targetRatio) into the trading ledger. */
export function equalizeLedger(balances, opts = {}) {
  const plan = planRebalance(balances.polymarket, balances.kalshi, opts);
  return {
    balances: { ...plan.equalized },
    plan,
  };
}

function clamp(n, lo, hi) {
  return Math.min(hi, Math.max(lo, n));
}

function round2(n) {
  return Math.round(Number(n) * 100) / 100;
}
