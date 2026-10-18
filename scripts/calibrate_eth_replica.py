"""Build the ETH replica proxy config from chronological public Kraken candles."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from optimize_kraken import evaluate, observations, wilson


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candles", type=Path)
    parser.add_argument("--config", type=Path, default=Path("kraken-eth-replica.json"))
    parser.add_argument("--report", type=Path, default=Path("eth-optimization-report.json"))
    args = parser.parse_args()
    candles = json.loads(args.candles.read_text())["candles"]
    rows = observations(candles, 300, 60)
    first, last = min(row["start"] for row in rows), max(row["start"] for row in rows)
    split = first + int((last-first)*.67)
    train = [row for row in rows if row["start"]+300 <= split]
    test = [row for row in rows if row["start"] >= split]
    buckets = []
    for lower, upper in ((5.0, 7.5), (7.5, None)):
        selected = [row for row in train if row["stable"] and row["move_bp"] >= lower
                    and (upper is None or row["move_bp"] < upper)]
        wins = sum(row["won"] for row in selected)
        buckets.append({"min_move_bp": lower, "max_move_bp": upper,
                        "samples": len(selected), "wins": wins,
                        "probability_lower": wilson(wins, len(selected))})
    train_metrics, test_metrics = evaluate(train, 0, 5), evaluate(test, 0, 5)
    passed = (test_metrics["signals"] >= 1000 and test_metrics["wilson_95_lower"] >= .90
              and all(bucket["samples"] >= 100 for bucket in buckets))
    report = {
        "asset": "eth", "data_source": "Kraken PF_ETHUSD one-minute trade candles",
        "candles": len(candles),
        "first_utc": datetime.fromtimestamp(first, timezone.utc).isoformat(),
        "last_utc": datetime.fromtimestamp(last, timezone.utc).isoformat(),
        "split_utc": datetime.fromtimestamp(split, timezone.utc).isoformat(),
        "rule": "At 55-60 seconds remaining, follow a >=5bp interval move only when the prior completed minute has the same sign.",
        "train": train_metrics, "chronological_holdout": test_metrics,
        "train_move_probability_buckets": buckets,
        "proxy_validation_passed": passed,
        "wallet_eth_crosscheck": {"signals": 5, "wins": 4, "warning": "Too sparse for calibration."},
        "limitations": [
            "Kraken candle direction is a proxy; Polymarket uses its own resolution source.",
            "One-minute candles cannot reproduce order-book latency or executable Polymarket asks.",
            "No fee-inclusive historical PnL or fill backtest is claimed.",
            "Paper and bounded live results must be evaluated separately for ETH.",
        ],
    }
    config = {
        "version": 2, "asset": "eth", "strategy": "kraken_eth_replica",
        "interval_seconds": 300, "remaining_seconds": 60, "min_z": 0.0,
        "min_move_bp": 5.0, "volatility_minutes": 60, "volatility_floor": .0001,
        "min_probability": .80, "min_edge": .02, "min_ask": .68, "max_ask": .77,
        "fee_rate_ceiling": .10, "probability_haircut": .03,
        "probability_buckets": [], "move_probability_buckets": buckets,
        "order_notional": 4.0, "entry_budget": 5.0, "target_entries": 6,
        "proxy_validation_passed": passed, "live_profitability_established": False,
    }
    if not passed:
        raise SystemExit("ETH chronological proxy validation failed")
    args.report.write_text(json.dumps(report, indent=2)+"\n")
    args.config.write_text(json.dumps(config, indent=2)+"\n")
    print(json.dumps({"train": train_metrics, "test": test_metrics,
                      "buckets": buckets, "proxy_validation_passed": passed}, indent=2))


if __name__ == "__main__":
    main()
