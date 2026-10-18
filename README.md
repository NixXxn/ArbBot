# Arbot

Fee-aware Polymarket **YES/NO (Up/Down) pair arbitrage** bot.

Buys both sides of a binary market when the combined executable cost, fees, and
slippage leave a configurable net edge under the $1 payout. Legs are filled
**sequentially** through the fast limit-order path: the first leg is chosen by
short-term urgency (spread, depth, fill odds — not volatility alone). The second
leg’s max price is derived from the **actual first fill**, fees, `min_edge`, and
`safety_margin`. Unpaired inventory escalates (update / cancel / close) when the
hedge cannot complete in time. Non-atomic fills are **not** claimed as guaranteed
pairs.

## What changed from the sniper portfolio

- Removed Kraken / Up-60 / pre-open / clip / early3m directional strategies from
  the dashboard and worker spawn path.
- Single selectable strategy: **arbitrage**.
- Paper runner: `scripts/run_arb_paper.py`.
- Core logic: `scripts/arbitrage/`.
- Dashboard branded **Arbot** with pair status, fees, expected vs locked vs
  unpaired risk, and editable arb settings.

Shared infrastructure kept: order helpers (`live_executor.py`), session risk,
portfolio controls, logging, paper persistence, dashboard supervisor.

## Defaults (`config/arbitrage.yaml`)

| Knob | Default |
|------|---------|
| `max_pair_cost` | 0.94 |
| `min_edge` | 0.025 |
| `fee_buffer` | 0.01 |
| `safety_margin` | 0.005 |
| `position_usd` | 45 |
| `max_position_usd` | 110 |
| `max_open_pairs` | 8 |
| `hedge_timeout_seconds` | 120 |
| `max_unpaired_notional` | 45 |
| `min_pair_profit_pct` | 0.005 |
| mode | **paper** |

## Run (paper)

```bash
export BOT_DATA_DIR=./data
export DASHBOARD_TOKEN=dev-token-at-least-16
export LIVE_KILL=1
python scripts/run_arb_paper.py ./data ./config/arbitrage.yaml 60 1000
# or dashboard:
BOT_DATA_DIR=./data python scripts/dashboard_server.py
```

Docker:

```bash
export DASHBOARD_TOKEN=change-me-16chars
docker compose up --build
```

Live trading stays gated (`LIVE_KILL=1`). Do not enable live until any prior
unresolved wallet positions are reconciled (see `data/PREFLIGHT_NOTE.txt`).

## PnL labels

- **Locked pair profit** — matched equal shares; fee-aware $1 − costs (secured).
- **Expected profit** — planned/incomplete pairs (not secured).
- **Unpaired directional risk** — open first-leg notional still hedged incompletely.

## Tests

```bash
pip install -r requirements.txt pytest requests
pytest tests/test_arbitrage_core.py -q
```
