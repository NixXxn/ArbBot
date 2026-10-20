import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getState } from './state.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function basicAuth(user, token) {
  return (req, res, next) => {
    if (!token || String(token).length < 8) {
      res.status(503).json({ error: 'Set DASHBOARD_TOKEN (min 8 chars) to enable the dashboard' });
      return;
    }
    const header = req.headers.authorization || '';
    if (!header.startsWith('Basic ')) {
      res.set('WWW-Authenticate', 'Basic realm="Arbot"');
      res.status(401).send('Authentication required');
      return;
    }
    const decoded = Buffer.from(header.slice(6), 'base64').toString('utf8');
    const idx = decoded.indexOf(':');
    const u = idx >= 0 ? decoded.slice(0, idx) : '';
    const p = idx >= 0 ? decoded.slice(idx + 1) : '';
    if (u !== user || p !== token) {
      res.set('WWW-Authenticate', 'Basic realm="Arbot"');
      res.status(401).send('Invalid credentials');
      return;
    }
    next();
  };
}

export function createDashboard({ config, bot }) {
  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: '32kb' }));

  // Unauthenticated health for Coolify / Docker HEALTHCHECK
  app.get('/api/health', (_req, res) => {
    const s = getState();
    res.json({
      ok: true,
      running: s.running,
      dryRun: s.dryRun,
      lastPollAt: s.lastPollAt,
      lastError: s.lastError,
    });
  });

  app.use(basicAuth(config.dashboardUser, config.dashboardToken));
  app.use(express.static(path.join(__dirname, '..', 'public')));

  app.get('/api/status', (_req, res) => {
    res.json({
      ...getState(),
      config: {
        polymarketUrl: config.polymarketUrl,
        kalshiUrl: config.kalshiUrl,
        scanAll: config.scanAll,
        scanLimitPoly: config.scanLimitPoly,
        scanLimitKalshi: config.scanLimitKalshi,
        matchingThreshold: config.matchingThreshold,
        pollIntervalSeconds: config.pollIntervalSeconds,
        minProfitCents: config.minProfitCents,
        tradingMode: config.tradingMode,
        tradeAmountCents: config.tradeAmountCents,
        dryRun: config.dryRun,
        liveKill: config.liveKill,
        hasPmxtKey: Boolean(config.pmxtApiKey),
        hasPolyKey: Boolean(config.polymarketPrivateKey),
        hasKalshiKey: Boolean(config.kalshiApiKey && config.kalshiPrivateKey),
      },
    });
  });

  app.post('/api/poll', async (_req, res) => {
    try {
      if (!bot) {
        res.status(503).json({ error: 'Bot not running' });
        return;
      }
      await bot.poll();
      res.json({ ok: true, status: getState() });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  return app;
}
