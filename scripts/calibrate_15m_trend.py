"""Calibrate the BTC/ETH 15-minute trend model on chronological Kraken data."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path


BUCKETS = ((5.0, 7.5), (7.5, 10.0),
           (10.0, 15.0), (15.0, 25.0), (25.0, None))
MIN_MOVE_BP = 5.0


def wilson(wins, count):
    if not count:
        return 0.0
    p, z = wins / count, 1.96
    return (p + z*z/(2*count) - z*math.sqrt(p*(1-p)/count + z*z/(4*count*count))) / (1+z*z/count)


def ema(values, period):
    alpha, value = 2/(period+1), values[0]
    for current in values[1:]:
        value = alpha*current + (1-alpha)*value
    return value


def rsi(values, period=14):
    changes = [b-a for a, b in zip(values[-period-1:-1], values[-period:])]
    gain = sum(max(change, 0) for change in changes)/period
    loss = sum(max(-change, 0) for change in changes)/period
    return 100.0 if loss == 0 else 100-100/(1+gain/loss)


def observations(candles):
    closes = {row["time"]//1000: float(row["close"]) for row in candles}
    opens = {row["time"]//1000: float(row["open"]) for row in candles}
    result = []
    for start in sorted(closes):
        if start % 900:
            continue
        entry = start+180
        history = list(range(entry-30*60, entry, 60))
        if (any(timestamp not in closes for timestamp in history)
                or start not in opens or start+840 not in closes):
            continue
        values = [closes[timestamp] for timestamp in history]
        move_bp = math.log(values[-1]/opens[start])*10_000
        ema_bp = math.log(ema(values, 9)/ema(values, 21))*10_000
        rsi_value = rsi(values)
        direction = 1 if move_bp > 0 else -1
        # Open-to-now direction is the strongest high-coverage holdout feature.
        # EMA/RSI remain telemetry rather than hard vetoes: requiring agreement
        # reduced coverage without improving the broad holdout rule.
        eligible = abs(move_bp) >= MIN_MOVE_BP
        won = (closes[start+840] >= opens[start]) == (direction > 0)
        result.append({"start": start, "move_bp": abs(move_bp),
                       "ema_spread_bp": abs(ema_bp), "rsi": rsi_value,
                       "eligible": eligible, "won": won})
    return result


def metrics(rows):
    selected = [row for row in rows if row["eligible"]]
    wins = sum(row["won"] for row in selected)
    days = (rows[-1]["start"]-rows[0]["start"]+900)/86400 if rows else 0
    return {"eligible_markets": len(rows), "signals": len(selected), "wins": wins,
            "win_rate": wins/len(selected) if selected else None,
            "wilson_95_lower": wilson(wins, len(selected)),
            "coverage": len(selected)/len(rows) if rows else 0,
            "signals_per_day_proxy": len(selected)/days if days else 0}


def calibrate(asset, path):
    candles = json.loads(path.read_text())["candles"]
    rows = observations(candles)
    split = rows[0]["start"] + int((rows[-1]["start"]-rows[0]["start"])*.67)
    train = [row for row in rows if row["start"]+900 <= split]
    test = [row for row in rows if row["start"] >= split]
    buckets = []
    for lower, upper in BUCKETS:
        selected = [row for row in train if row["eligible"] and row["move_bp"] >= lower
                    and (upper is None or row["move_bp"] < upper)]
        wins = sum(row["won"] for row in selected)
        buckets.append({"min_move_bp": lower, "max_move_bp": upper,
                        "samples": len(selected), "wins": wins,
                        "probability_lower": wilson(wins, len(selected))})
    holdout = metrics(test)
    passed = (holdout["signals"] >= 500 and holdout["wilson_95_lower"] >= .65
              and all(bucket["samples"] >= 100 for bucket in buckets))
    config = {
        "version": 3, "asset": asset, "strategy": "kraken_trend_15m",
        "interval_seconds": 900, "remaining_seconds": 720,
        "entry_window_seconds": 30, "min_z": 0.0, "min_move_bp": MIN_MOVE_BP,
        "volatility_minutes": 60, "volatility_floor": .0001,
        "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
        "min_probability": .55, "min_edge": .005,
        "min_ask": .20, "max_ask": .75, "fee_rate_ceiling": .10,
        "probability_haircut": .02, "probability_buckets": [],
        "move_probability_buckets": buckets,
        "position_tiers": [
            {"min_probability": .55, "budget": 4.0},
            {"min_probability": .65, "budget": 4.5},
            {"min_probability": .72, "budget": 5.0},
        ],
        "order_notional": 4.0, "entry_budget": 5.0, "target_entries": 6,
        "proxy_validation_passed": passed, "live_profitability_established": False,
    }
    report = {
        "asset": asset, "data_source": f"Kraken PF_{'XBT' if asset == 'btc' else 'ETH'}USD one-minute trade candles",
        "candles": len(candles),
        "first_utc": datetime.fromtimestamp(rows[0]["start"], timezone.utc).isoformat(),
        "last_utc": datetime.fromtimestamp(rows[-1]["start"], timezone.utc).isoformat(),
        "split_utc": datetime.fromtimestamp(split, timezone.utc).isoformat(),
        "rule": "Three minutes after the 15m open, follow the open-to-now move at 5bp or more; EMA9/21 and RSI14 are logged as context, not hard vetoes.",
        "train": metrics(train), "chronological_holdout": holdout,
        "train_move_probability_buckets": buckets,
        "proxy_validation_passed": passed,
        "position_sizing": config["position_tiers"],
        "limitations": [
            "Kraken candle direction is a proxy for the Polymarket resolution source.",
            "Historical Polymarket executable asks, queue position, latency and net PnL are unavailable.",
            "Price and fee gates can reduce live frequency substantially below the signal proxy.",
            "A bounded paper/live sample is still required; profitability is not established.",
        ],
    }
    return config, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc", type=Path, required=True)
    parser.add_argument("--eth", type=Path, required=True)
    parser.add_argument("--directory", type=Path, default=Path("."))
    args = parser.parse_args()
    combined = {}
    for asset, path in (("btc", args.btc), ("eth", args.eth)):
        config, report = calibrate(asset, path)
        if not report["proxy_validation_passed"]:
            raise SystemExit(f"{asset} chronological validation failed")
        (args.directory/f"kraken-trend-{asset}.json").write_text(json.dumps(config, indent=2)+"\n")
        combined[asset] = report
    (args.directory/"trend-15m-optimization-report.json").write_text(json.dumps(combined, indent=2)+"\n")
    print(json.dumps({asset: report["chronological_holdout"] for asset, report in combined.items()}, indent=2))


if __name__ == "__main__":
    main()
