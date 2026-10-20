# Arbot — Polymarket ↔ Kalshi (pmxt)

Synthetic YES/NO arbitrage between Polymarket and Kalshi, adapted from
[prediction-market-arbitrage-bot](https://github.com/realfishsam/prediction-market-arbitrage-bot)
for **Coolify** with a Basic-auth **web dashboard**.

Not financial advice. Educational / own-risk only.

## Quick start (local)

```bash
npm install
cp .env.example .env   # or keep your existing .env
npm start              # dashboard + bot on :8080
```

Open `http://localhost:8080` with `DASHBOARD_USER` / `DASHBOARD_TOKEN`.

## Coolify

1. Deploy from this Git repo (Dockerfile build).
2. Expose port **8080**.
3. Set env vars (at least `DASHBOARD_TOKEN`, market URLs, `PMXT_API_KEY`).
4. Keep `LIVE_KILL=1` until you intentionally go live.
5. Persist `/data` as a volume for `bot-status.json`.

## Credentials

Existing ArbBot `.env` keys are accepted:

| Your key | Used as |
|----------|---------|
| `POLY_PRIVATE_KEY` | Polymarket signer |
| `POLY_FUNDER` | Proxy / funder |
| `KALSHI_KEY_ID` | Kalshi API key id |
| `KALSHI_PK` | Kalshi private key |
| `PMXT_API_KEY` | Hosted pmxt reads |
| `LIVE_KILL=1` | Forces dry-run |

Live trades require `LIVE_KILL=0` and `DRY_RUN=false` plus venue credentials.

## Config

| Env | Default | Notes |
|-----|---------|--------|
| `POLYMARKET_URL` / `KALSHI_URL` | sample Fed-chair event | Event URLs to pair |
| `TRADING_MODE` | `CONSERVATIVE` | or `YOLO` |
| `TRADE_AMOUNT_CENTS` | `100` | per leg in CONSERVATIVE |
| `MIN_PROFIT_CENTS` | `1` | entry floor |
| `POLL_INTERVAL_SECONDS` | `60` | scan cadence |

## Scripts

- `npm start` — dashboard + bot
- `npm test` — matcher / arbitrage unit tests
- `BOT_MODE=bot npm start` — bot only (no HTTP)

## Strategy (from upstream)

1. Fuzzy-match outcomes across venues.
2. Compare Poly YES+Kalshi NO vs Kalshi YES+Poly NO.
3. Market-buy both legs when profit ≥ threshold (dry-run by default).
4. Exit / rotate when spread collapses or a better opp appears.
5. **Rebalance after every exit:** equalize trade budgets 50/50 so both venues stay funded. Dry-run does this on an internal ledger; live sizes to `min(Poly, Kalshi)` and shows any bank/USDC transfer still needed (venues cannot wire to each other via API).
