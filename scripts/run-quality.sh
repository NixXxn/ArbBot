#!/bin/sh
set -eu
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
mode=${1:-monitor}
if [ "$mode" != monitor ] && [ "$mode" != live ]; then
    echo 'Usage: sh scripts/run-quality.sh monitor|live' >&2
    exit 1
fi
run_id=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p logs
if [ "$mode" = live ]; then
    exec .venv/bin/python -u scripts/bounded_console.py logs/quality-console.log \
        .venv/bin/python -u scripts/run_ten.py --live --kraken-quality \
        --quality-config kraken-quality.json --env .env --seconds 31536000 \
        --state "logs/quality-live-$run_id.json" --journal logs/quality-signals.jsonl
fi
exec .venv/bin/python -u scripts/bounded_console.py logs/quality-monitor-console.log \
    target/release/leadlag --signals-only --kraken-quality --quality-config kraken-quality.json \
    --seconds 31536000 --journal logs/quality-monitor-signals.jsonl
