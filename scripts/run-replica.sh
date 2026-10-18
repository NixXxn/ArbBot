#!/bin/sh
set -eu
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
mode=${1:-monitor}
strategy=${2:-arbitrage}
case "$strategy" in arb|all|combined) strategy=arbitrage ;; esac
case "$strategy" in arbitrage) ;; *) echo 'Strategy must be arbitrage' >&2; exit 2;; esac
mkdir -p data logs
echo "Arbot $mode strategy=$strategy" >&2
case "$mode" in
  monitor|paper)
    exec python3 -u scripts/bounded_console.py logs/replica-paper-console.log \
      python3 -u scripts/run_arb_paper.py data leadlag . 31536000 arbitrage "${PAPER_BUDGET:-1000}" \
      data/replica-stat-resets.json
    ;;
  live)
    echo 'Arbot: live entries disabled in this build (LIVE_KILL / paper-only validation). Use monitor.' >&2
    exit 4
    ;;
  *)
    echo 'Usage: sh scripts/run-replica.sh monitor|paper [arbitrage]' >&2
    exit 2
    ;;
esac
