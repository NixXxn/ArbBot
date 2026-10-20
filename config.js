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

export const config = {
  // Market URLs (override via env for Coolify without editing files)
  polymarketUrl: env(
    'POLYMARKET_URL',
    'https://polymarket.com/event/who-will-trump-nominate-as-fed-chair',
  ),
  kalshiUrl: env(
    'KALSHI_URL',
    'https://kalshi.com/markets/kxfedchairnom/fed-chair-nominee/kxfedchairnom-29',
  ),

  pollIntervalSeconds: envNum('POLL_INTERVAL_SECONDS', 60),
  minProfitCents: envNum('MIN_PROFIT_CENTS', 1),
  tradingMode: env('TRADING_MODE', 'CONSERVATIVE'), // YOLO | CONSERVATIVE
  tradeAmountCents: envNum('TRADE_AMOUNT_CENTS', 100),
  matchingThreshold: envNum('MATCHING_THRESHOLD', 0.7),
  topNOpportunities: envNum('TOP_N_OPPORTUNITIES', 5),
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
