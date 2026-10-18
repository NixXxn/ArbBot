# Arbot deploy notes

Renamed from polymarket-sniperbot. Single strategy: arbitrage. Paper default; LIVE_KILL=1.

# Deploy the Kraken 5-minute + 15-minute worker

The default container mode is `dashboard`, initially in paper mode. The dashboard can
run the Kraken BTC/ETH 5m and 15m engines, the BTC/ETH Up-60 engines, both sets, the
separate SOL five-minute Down-0.35 engine, or all seven feeds, and
paper mode never loads credentials or sends orders. Live mode runs the selected feeds
with one shared wallet controller and concurrent positions in distinct events.
Reservations scale with available collateral and total open exposure cannot exceed 30%.

The dashboard runs as a separate supervisor process. The trading engine does not
call the dashboard and its market-data path is unchanged. Files are read only when
the dashboard is open, at five-second intervals, and each log view is capped at
128 KiB and 250 lines. Journals and console logs retain their existing rotation.

## Publish your own GitHub repository

The checkout still points at the original upstream repository. Create an empty
private repository in your GitHub account, then run:

```sh
cd /path/to/polymarket-sniperbot
git remote rename origin upstream
git remote add origin https://github.com/YOUR_USER/polymarket-sniperbot.git
git add .
git commit -m "Add bounded Kraken wallet-replica strategy and Coolify deployment"
git push -u origin main
```

`.env`, logs, raw public-data downloads, virtual environments, and build output are
ignored. Verify that no secret is staged before committing:

```sh
git status --short
git ls-files '.env' '.env.*' 'logs/*' 'research/data/*'
```

The GitHub Actions workflow runs all Rust and Python tests and builds the image.

## Coolify

1. Add a new **Application** from the private GitHub repository.
2. Select **Dockerfile** as the build pack, expose port `8080`, and attach an HTTPS
   domain to that port. The server also honors Coolify's `PORT` variable, preventing
   a proxy mismatch when Coolify supplies a different internal port.
3. Add persistent storage mounted at `/data`. Run exactly one replica.
4. Set `BOT_MODE=dashboard`, `BOT_INITIAL_MODE=paper`, `BOT_STRATEGY=all`,
   `DASHBOARD_USER=admin`, and
   a random `DASHBOARD_TOKEN` containing at least 16 characters. Store the token as
   a Coolify secret. Start with `LIVE_KILL=1`.
5. Deploy and open the HTTPS domain. Your browser will request the dashboard user
   and token through HTTP Basic authentication. After the first full minute,
   `/data/replica-paper-btc-15m.jsonl`, `/data/replica-paper-eth-15m.jsonl`,
   `/data/replica-paper-btc-5m.jsonl`, `/data/replica-paper-eth-5m.jsonl`,
   `/data/replica-paper-btc-up60.jsonl`, `/data/replica-paper-eth-up60.jsonl`, or
   `/data/replica-paper-sol-down35.jsonl` for the selected strategy
   should contain health records with advancing Kraken and Polymarket event counts.

The dashboard shows worker health, live state, recent bounded logs, individual log
downloads, and a ZIP export. Exports include only `.json`, `.jsonl`, `.log`, and
rotated log files from `/data`; environment variables and secret files are excluded.

Paper mode runs the selected signal engines with a shared hypothetical portfolio and does not
submit orders. Its dashboard budget field controls the simulated bankroll (minimum $16,
default $30). Position tiers scale with current paper equity and combined open exposure
is capped at 30%. A background resolver records each settled outcome, allowing the
dashboard to report per-strategy trades, winrate, ROI, and P/L. Paper P/L and ROI
include a conservative 2% fee reserve. Live cost uses confirmed collateral flows from
the buy receipts; payout uses the filled share quantity and confirmed resolved outcome.
The redemption-receipt transfer remains available as an audit field because relayed
proxy-wallet transactions may not transfer collateral directly to `POLY_FUNDER`.
Live statistics persist across worker sessions in
`/data/replica-live-history.jsonl`, with a compact snapshot in
`/data/replica-portfolio-live.json`. Select **Kraken only**, **Up-60 only**,
**Kraken + Up-60**, **SOL 5m Down 0.35 only**, or **All strategies (risk balanced)**
from the dashboard. A Live strategy change requests a safe stop, waits for any held
position to resolve and redeem, and then starts a new Live session with the selection.
The selection persists and is passed unchanged to the live worker. SOL Down-0.35 is
isolated from the other engines: it buys `Down` once per SOL five-minute event at an
ask no higher than 0.35, spends exactly $5, attempts TP at an executable `Down` bid
of 0.65 and SL when the `Up` ask reaches 0.80, and otherwise holds to resolution.
It never opens a second leg. Its $6.25 order reservation must remain below the shared
30% exposure limit, so the available live collateral must be at least about $20.84.
All mode adds a 15% concentration ceiling per strategy and a 15-minute cooldown after
three consecutive realized losses. One executable market-minimum order may exceed the
strategy ceiling on a small bankroll, but it must still fit below the total 30% limit.
The dashboard reports these risk-control skips. These controls reduce loss clustering;
they do not establish or guarantee profitable execution.
Live sizing defaults to **Minimum market size**, which calculates the smallest
cent-denominated reservation that can buy the market's minimum shares at the guarded
execution price. **Adaptive bankroll sizing** restores the configured tiers scaled
up or down with available collateral. Both modes retain the 30% exposure ceiling.
The dashboard displays live capital at cost, available collateral, and open exposure
beside its current 30% limit.
Distinct events may remain open simultaneously. Each market is limited to one entry,
and reservations plus all open positions share the same 30% portfolio ceiling.
To unlock the Live button, set `LIVE_KILL=0` in Coolify
and redeploy. The button then requires
confirming the funded switch. Switching back to paper creates a safe-stop request: no new live entry
is accepted, while an entry already in progress remains active through resolution
and redemption before paper mode begins. Each start creates a timestamped live state
file. A previous ambiguous `buy_intent`, `exit_intent`, `holding`, or `redemption_intent` is passed to
the next Live worker for authenticated recovery before new signals are accepted. FOK
settlement and current token inventory are checked; inventory is held to resolution and
redeemed first. A known legacy order ID is cancelled if it is unexpectedly still resting.
Temporary resolution-API failures are retried without terminating the funded worker. A deterministic
price, spread, depth, size, expiry, or stale-signal rejection that happened before
`post_order` is recorded as `not_submitted` and does not block continued monitoring.
If the live worker or a signal engine exits, the dashboard supervisor restarts it with
exponential backoff. The replacement worker reconciles unresolved state before trading.

Only live mode needs these Coolify environment variables:

```text
POLY_SIGNATURE_TYPE=3
POLY_PRIVATE_KEY=<secret>
POLY_FUNDER=<deposit-wallet-address>
RELAYER_API_KEY=<secret>
RELAYER_API_KEY_ADDRESS=<relayer-address>
LIVE_KILL=1
LIVE_SIZING=minimum
BOT_STRATEGY=all
```

Keep secret values marked as secrets. Do not store them in GitHub. The application
accepts Coolify environment variables directly; it does not need an `.env` file in
the image.

Before funded deployment, stop every older runner using the same wallet and inspect
its state for `buy_intent`, `holding`, or `redemption_intent`. From the Coolify
terminal, run the read-only preflight against the injected variables:

```sh
python /app/scripts/run_ten.py --preflight --combined \
  --quality-config /app/kraken-trend-btc.json \
  --eth-quality-config /app/kraken-trend-eth.json \
  --btc-5m-config /app/kraken-replica.json \
  --eth-5m-config /app/kraken-eth-replica.json --env /nonexistent \
  --state /data/manual-preflight-$(date -u +%Y%m%dT%H%M%SZ).json
```

For a dashboard-controlled bounded run, keep `BOT_MODE=dashboard`, set
`LIVE_KILL=0`, redeploy, and use the confirmed Live button.
The dashboard creates `/data/replica-live-<timestamp>-<suffix>.json` for each funded
session. Completed historical state files do not prevent a deliberate new start.
Historical unresolved states trigger recovery inside the new Live worker rather than
blocking the Paper-to-Live transition. Run only one dashboard replica per wallet.
Legacy `buy_intent` states whose recorded reason proves that the entry guard stopped
before submission are reconciled automatically without editing the historical file.

Inspect the exact state from the Coolify terminal:

```sh
current_state="$(ls -1t /data/replica-live-*.json | head -1)"
cat "$current_state"
tail -n 20 /data/replica-console.log
tail -n 5 /data/replica-signals-btc-15m.jsonl
tail -n 5 /data/replica-signals-eth-15m.jsonl
tail -n 5 /data/replica-signals-btc-5m.jsonl
tail -n 5 /data/replica-signals-eth-5m.jsonl
cat /data/replica-portfolio-live.json
```

After a terminal run, the timestamped state already remains available for review. You
may move old terminal files to an archive directory to keep `/data` tidy:

```sh
mkdir -p /data/archive
find /data -maxdepth 1 -name 'replica-live-*.json' -type f -mtime +30 \
  -exec mv {} /data/archive/ \;
```

Never move a state merely to bypass an ambiguous order or unresolved holding.

## Linux VPS with venv and tmux

After pulling the GitHub repository:

```sh
cd /tmp/bots/polymarket-sniperbot
source .venv/bin/activate
pip install -r requirements.txt
sh scripts/build.sh
python -m unittest discover -s tests -q
```

Continuous signal-only monitoring:

```sh
tmux new-session -d -s replica-monitor \
  'cd /tmp/bots/polymarket-sniperbot && exec sh scripts/run-replica.sh monitor all'
```

Bounded funded run, after stopping and reconciling every older wallet process:

```sh
tmux new-session -d -s replica-live \
  'cd /tmp/bots/polymarket-sniperbot && exec sh scripts/run-replica.sh live all'
```

Check it without attaching:

```sh
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} dead=#{pane_dead} command=#{pane_current_command} pid=#{pane_pid}'
pgrep -af 'scripts/run_ten.py|target/release/leadlag|bounded_console.py'
tail -n 5 logs/replica-monitor-signals.jsonl
current_state="$(ls -1t logs/replica-live-*.json 2>/dev/null | head -1)"
[ -z "$current_state" ] || cat "$current_state"
```

The Rust journal rotates near 8 MiB with two backups. Console logs rotate at 2 MiB
with two backups. Leave `--log-all-events` disabled for continuous operation.
