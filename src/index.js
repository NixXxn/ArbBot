/**
 * Coolify entry: web dashboard + Polymarket ↔ Kalshi arb bot (pmxt).
 */
import fs from 'node:fs';
import { config } from '../config.js';
import { ArbitrageBot } from './bot.js';
import { createDashboard } from './dashboard.js';
import { configureState, patchState, pushLog } from './state.js';

async function main() {
  configureState({ dataDir: config.dataDir });
  fs.mkdirSync(config.dataDir, { recursive: true });

  patchState({
    dryRun: config.dryRun,
    liveKill: config.liveKill,
    tradingMode: config.tradingMode,
    polymarketUrl: config.polymarketUrl,
    kalshiUrl: config.kalshiUrl,
    pollIntervalSeconds: config.pollIntervalSeconds,
  });

  if (!config.polymarketUrl || !config.kalshiUrl) {
    console.error('Set POLYMARKET_URL and KALSHI_URL');
    process.exit(1);
  }

  if (!config.dryRun) {
    if (!config.polymarketPrivateKey || !config.kalshiApiKey || !config.kalshiPrivateKey) {
      console.error('Live mode needs POLY_PRIVATE_KEY (or POLYMARKET_PRIVATE_KEY) and KALSHI_KEY_ID + KALSHI_PK');
      console.error('Keep LIVE_KILL=1 or DRY_RUN=true for paper/dry-run.');
      process.exit(1);
    }
  }

  const bot = new ArbitrageBot(config);

  const shutdown = () => {
    bot.stop();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  const mode = config.botMode || 'dashboard';

  if (mode === 'dashboard' || mode === 'both') {
    if (!config.dashboardToken || String(config.dashboardToken).length < 8) {
      console.error('DASHBOARD_TOKEN must be at least 8 characters for dashboard mode');
      process.exit(1);
    }
    const app = createDashboard({ config, bot });
    app.listen(config.port, '0.0.0.0', () => {
      pushLog('info', `Dashboard listening on :${config.port}`);
    });
  }

  // Always run the bot loop unless BOT_MODE=dashboard-only (not used)
  if (mode !== 'dashboard-only') {
    try {
      await bot.start();
    } catch (error) {
      pushLog('error', `Fatal: ${error.message}`);
      console.error(error.stack);
      if (mode === 'bot') process.exit(1);
    }
  }
}

main();
