import 'dotenv/config';

function env(name, fallback = undefined) {
  const v = process.env[name];
  if (v === undefined || v === '') return fallback;
  return v;
}

function envBool(name, fallback = false) {
  const v = env(name);
  if (v === undefined) return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase());
}

function envNum(name, fallback) {
  const v = env(name);
  if (v === undefined) return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

// Live only when LIVE_KILL=0 and DRY_RUN is not forced on.
const liveKill = env('LIVE_KILL', '1') !== '0';
const dryRun = liveKill || envBool('DRY_RUN', true);

// SCAN_ALL=true (or empty URLs) → pull top markets on both venues and fuzzy-match all.
const scanAll = envBool('SCAN_ALL', false)
  || !env('POLYMARKET_URL')
  || !env('KALSHI_URL');

export const config = {
  // Single-event mode (ignored when scanAll is true)
  polymarketUrl: env('POLYMARKET_URL', ''),
  kalshiUrl: env('KALSHI_URL', ''),

  // Universe scan: fetch many markets, match every Poly outcome ↔ Kalshi outcome
  scanAll,
  scanLimitPoly: envNum('SCAN_LIMIT_POLY', 100),
  scanLimitKalshi: envNum('SCAN_LIMIT_KALSHI', 100),
  scanQuery: env('SCAN_QUERY', ''), // optional text filter, e.g. "fed" or "bitcoin"
  scanSort: env('SCAN_SORT', 'volume'), // volume | liquidity | newest

  pollIntervalSeconds: envNum('POLL_INTERVAL_SECONDS', 60),
  minProfitCents: envNum('MIN_PROFIT_CENTS', 1),
  // CONSERVATIVE (fixed) | KELLY (scales with edge + match confidence) | YOLO
  tradingMode: env('TRADING_MODE', 'KELLY'),
  tradeAmountCents: envNum('TRADE_AMOUNT_CENTS', 100),
  kellyFraction: envNum('KELLY_FRACTION', 0.5), // 0.5 = half-Kelly
  kellyMaxBankrollPct: envNum('KELLY_MAX_BANKROLL_PCT', 0.25),
  kellyMinCents: envNum('KELLY_MIN_CENTS', 25),
  kellyBaseFillProb: envNum('KELLY_BASE_FILL_PROB', 0.92),
  // Lower = more matches (noisier). Enhanced matcher: 0.5–0.6 broad; 0.75+ strict.
  matchingThreshold: envNum('MATCHING_THRESHOLD', scanAll ? 0.55 : 0.65),
  topNOpportunities: envNum('TOP_N_OPPORTUNITIES', scanAll ? 20 : 5),
  minPriceThreshold: envNum('MIN_PRICE_THRESHOLD', 2),

  dryRun,
  liveKill,

  // After each exit (win/loss): equalize trade budgets across venues.
  // Real Poly↔Kalshi bank wires are not available via API; we redistribute
  // the internal ledger and size the next trade by min(poly, kalshi).
  rebalanceEnabled: envBool('REBALANCE_ENABLED', true),
  rebalanceTargetRatio: envNum('REBALANCE_TARGET_RATIO', 0.5),
  rebalanceMinDeltaUsd: envNum('REBALANCE_MIN_DELTA_USD', 1),
  // Starting cash per side in dry-run (total = 2 × this unless PAPER_BUDGET set)
  paperBudgetUsd: envNum('PAPER_BUDGET', envNum('REBALANCE_START_USD', 1000)),

  // Polymarket — accept legacy ArbBot names + reference names
  polymarketPrivateKey: env('POLYMARKET_PRIVATE_KEY') || env('POLY_PRIVATE_KEY'),
  polymarketFunder: env('POLYMARKET_PROXY_ADDRESS') || env('POLY_FUNDER'),
  polymarketSignatureType: envNum('POLY_SIGNATURE_TYPE', 3),

  // Kalshi — KEY_ID + PK from existing .env, or reference names
  kalshiApiKey: env('KALSHI_API_KEY') || env('KALSHI_KEY_ID'),
  kalshiPrivateKey: env('KALSHI_PRIVATE_KEY') || env('KALSHI_API_SECRET') || env('KALSHI_PK'),

  // Hosted pmxt reads (preferred when set)
  pmxtApiKey: env('PMXT_API_KEY'),

  // Dashboard / Coolify
  botMode: env('BOT_MODE', 'dashboard'), // dashboard | bot
  port: envNum('PORT', 8080),
  dataDir: env('BOT_DATA_DIR', 'data'),
  dashboardUser: env('DASHBOARD_USER', 'admin'),
  dashboardToken: env('DASHBOARD_TOKEN', ''),
};

export default config;
