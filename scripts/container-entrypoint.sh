#!/bin/sh
set -eu
mode=${BOT_MODE:-dashboard}
data_dir=${BOT_DATA_DIR:-/data}
strategy=${BOT_STRATEGY:-arbitrage}
case "$strategy" in arb|all|combined) strategy=arbitrage ;; esac
case "$strategy" in arbitrage) ;; *) echo 'BOT_STRATEGY must be arbitrage' >&2; exit 2;; esac
mkdir -p "$data_dir"
echo "Arbot starting mode=$mode strategy=$strategy" >&2
case "$mode" in
  dashboard)
    exec python -u /app/scripts/dashboard_server.py
    ;;
  monitor|paper)
    exec python -u /app/scripts/bounded_console.py "$data_dir/replica-monitor-console.log" \
      python -u /app/scripts/run_arb_paper.py "$data_dir" /app/leadlag /app 31536000 arbitrage "${PAPER_BUDGET:-1000}" \
      "$data_dir/replica-stat-resets.json"
    ;;
  preflight|live)
    # Live trading stays gated. Prefer paper until unpaired recovery is confirmed.
    if [ "${LIVE_KILL:-1}" != "0" ]; then
      echo "Arbot: LIVE_KILL!=0 — refusing live/preflight; use BOT_MODE=paper or dashboard" >&2
      exit 4
    fi
    echo "Arbot: live path not enabled for new entries in this build; use paper mode" >&2
    exit 5
    ;;
  *)
    echo 'BOT_MODE must be dashboard, monitor, or paper' >&2
    exit 2
    ;;
esac
