"""Summarize public Polymarket wallet exports without inferring unavailable intent."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (index - low)


def summary(values):
    values = list(values)
    return {
        "count": len(values),
        "p25": percentile(values, 0.25),
        "median": statistics.median(values) if values else None,
        "p75": percentile(values, 0.75),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def start_from_slug(slug):
    try:
        return int(slug.rsplit("-", 1)[1])
    except (AttributeError, ValueError, IndexError):
        return None


def analyze(directory, kraken_candles=None):
    directory = Path(directory)
    trades = json.loads((directory / "trades-0.json").read_text())
    activity = []
    for path in sorted(directory.glob("activity-*.json")):
        activity.extend(json.loads(path.read_text()))
    activity = list({
        (row.get("transactionHash"), row.get("type"), row.get("asset"), row.get("timestamp")): row
        for row in activity
    }.values())
    positions = json.loads((directory / "positions.json").read_text())
    closed = []
    for path in [directory / "closed_positions.json", *sorted(directory.glob("closed-*.json"))]:
        if path.exists():
            closed.extend(json.loads(path.read_text()))
    closed = list({row["conditionId"]: row for row in closed}.values())
    by_condition = defaultdict(list)
    for trade in trades:
        by_condition[trade["conditionId"]].append(trade)
    redeems = defaultdict(float)
    for event in activity:
        if event.get("type") == "REDEEM":
            redeems[event["conditionId"]] += float(event.get("usdcSize", 0))
    losing = {
        row["conditionId"] for row in positions
        if row.get("redeemable") and float(row.get("curPrice", -1)) == 0
    }
    winning = set(redeems) | {
        row["conditionId"] for row in closed if float(row.get("curPrice", 0)) == 1
    }
    markets = []
    for condition, rows in by_condition.items():
        rows.sort(key=lambda row: row["timestamp"])
        buys = [row for row in rows if row["side"] == "BUY"]
        sells = [row for row in rows if row["side"] == "SELL"]
        start = start_from_slug(rows[0].get("slug"))
        buy_cost = sum(float(row["size"]) * float(row["price"]) for row in buys)
        sell_value = sum(float(row["size"]) * float(row["price"]) for row in sells)
        buy_size = sum(float(row["size"]) for row in buys)
        markets.append({
            "condition_id": condition,
            "slug": rows[0].get("slug"),
            "outcomes": sorted({row.get("outcome") for row in rows}),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_size": buy_size,
            "buy_cost": buy_cost,
            "buy_vwap": buy_cost / buy_size if buy_size else None,
            "first_buy_timestamp": buys[0]["timestamp"] if buys else None,
            "first_entry_offset_s": buys[0]["timestamp"] - start if buys and start else None,
            "last_entry_offset_s": buys[-1]["timestamp"] - start if buys and start else None,
            "redeemed": redeems.get(condition, 0),
            "gross_pnl_known": sell_value + redeems.get(condition, 0) - buy_cost
                if condition in winning or condition in losing else None,
            "result": "win" if condition in winning else "loss" if condition in losing else "unresolved",
            "start_hour_utc": datetime.fromtimestamp(start, timezone.utc).hour if start else None,
        })
    resolved = [row for row in markets if row["result"] != "unresolved"]
    wins = [row for row in resolved if row["result"] == "win"]
    losses = [row for row in resolved if row["result"] == "loss"]
    bitcoin_15m = [row for row in markets if row["slug"].startswith("btc-updown-15m-")]
    resolved.sort(key=lambda row: start_from_slug(row["slug"]) or 0)
    cost_after_win, cost_after_loss = [], []
    for previous, current in zip(resolved, resolved[1:]):
        bucket = cost_after_win if previous["result"] == "win" else cost_after_loss
        bucket.append(current["buy_cost"])
    pnl_path, peak, max_drawdown = 0.0, 0.0, 0.0
    for row in resolved:
        pnl_path += row["gross_pnl_known"]
        peak = max(peak, pnl_path)
        max_drawdown = max(max_drawdown, peak - pnl_path)
    positive = sorted((row["gross_pnl_known"] for row in wins), reverse=True)
    total_cost = sum(row["buy_cost"] for row in resolved)
    categories = Counter(row["slug"].rsplit("-", 1)[0] for row in markets)
    known_winner_net = sum(float(row.get("realizedPnl", 0)) for row in closed)
    loser_net = sum(
        float(row.get("cashPnl", 0)) + float(row.get("realizedPnl", 0)) for row in positions
        if row["conditionId"] in losing
    )
    kraken_match = {}
    if kraken_candles:
        candles = json.loads(Path(kraken_candles).read_text())["candles"]
        closes = sorted((int(row["time"]) // 1000, float(row["close"])) for row in candles)
        def close_at(timestamp):
            available = [price for candle_time, price in closes if candle_time <= timestamp]
            return available[-1] if available else None
        for lookback in (60, 120, 180, 300, 600, 660, 690, 700, 720, 900):
            observations, mean_reversion_results = [], []
            for row in bitcoin_15m:
                timestamp = row["first_buy_timestamp"]
                current, prior = close_at(timestamp), close_at(timestamp - lookback)
                if current is None or prior is None or current == prior:
                    continue
                predicted = "Up" if current > prior else "Down"
                observations.append(predicted == row["outcomes"][0])
                if row["result"] != "unresolved":
                    actual = row["outcomes"][0] if row["result"] == "win" else ("Down" if row["outcomes"][0] == "Up" else "Up")
                    mean_reversion = "Down" if predicted == "Up" else "Up"
                    mean_reversion_results.append(mean_reversion == actual)
            kraken_match[str(lookback)] = {
                "markets": len(observations),
                "trader_matches_kraken_momentum": sum(observations) / len(observations) if observations else None,
                "trader_matches_kraken_mean_reversion": 1-sum(observations) / len(observations) if observations else None,
                "mean_reversion_win_rate": sum(mean_reversion_results) / len(mean_reversion_results) if mean_reversion_results else None,
            }
    return {
        "source_limits": {
            "trades_rows": len(trades),
            "activity_rows": len(activity),
            "positions_rows": len(positions),
            "closed_positions_unique_rows": len(closed),
            "warning": "Public endpoints can cap rows; gross PnL excludes fees where the API does not expose them.",
        },
        "scope": {
            "unique_markets": len(markets),
            "bitcoin_15m_markets": len(bitcoin_15m),
            "other_markets": len(markets) - len(bitcoin_15m),
            "first_trade_utc": datetime.fromtimestamp(min(row["timestamp"] for row in trades), timezone.utc).isoformat(),
            "last_trade_utc": datetime.fromtimestamp(max(row["timestamp"] for row in trades), timezone.utc).isoformat(),
            "buy_trades": sum(row["side"] == "BUY" for row in trades),
            "sell_trades": sum(row["side"] == "SELL" for row in trades),
            "markets_with_both_outcomes": sum(len(row["outcomes"]) > 1 for row in markets),
        },
        "resolved_sample": {
            "markets": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(resolved) if resolved else None,
            "gross_pnl": sum(row["gross_pnl_known"] for row in resolved),
            "gross_profit": sum(row["gross_pnl_known"] for row in wins),
            "gross_loss": sum(row["gross_pnl_known"] for row in losses),
            "gross_return_on_buy_cost": sum(row["gross_pnl_known"] for row in resolved) / total_cost,
            "maximum_sequential_gross_drawdown": max_drawdown,
            "top_5_wins_share_of_gross_profit": sum(positive[:5]) / sum(positive),
            "known_net_pnl_partial": known_winner_net + loser_net,
            "known_net_pnl_covers_winners": len(closed),
        },
        "behavior": {
            "outcome_counts_by_market": Counter(row["outcomes"][0] for row in markets if len(row["outcomes"]) == 1),
            "buy_vwap": summary(row["buy_vwap"] for row in markets if row["buy_vwap"] is not None),
            "buy_cost_usdc": summary(row["buy_cost"] for row in markets),
            "buy_count_per_market": summary(row["buy_count"] for row in markets),
            "first_entry_offset_from_market_start_s": summary(
                row["first_entry_offset_s"] for row in markets if row["first_entry_offset_s"] is not None
            ),
            "last_entry_offset_from_market_start_s": summary(
                row["last_entry_offset_s"] for row in markets if row["last_entry_offset_s"] is not None
            ),
            "market_start_hour_utc": Counter(row["start_hour_utc"] for row in markets),
            "market_type_counts": categories,
            "resolved_win_buy_cost_usdc": summary(row["buy_cost"] for row in wins),
            "resolved_loss_buy_cost_usdc": summary(row["buy_cost"] for row in losses),
            "next_buy_cost_after_win_usdc": summary(cost_after_win),
            "next_buy_cost_after_loss_usdc": summary(cost_after_loss),
            "largest_10_positions_share_of_buy_cost": sum(
                sorted((row["buy_cost"] for row in resolved), reverse=True)[:10]
            ) / total_cost,
        },
        "fee_evidence": {
            "losing_positions_entry_fees_usdc": sum(float(row.get("entryFeesUsdc", 0)) for row in positions),
            "losing_positions_gross_initial_value_usdc": sum(float(row.get("grossInitialValue", 0)) for row in positions),
            "closed_winner_realized_pnl_usdc_api_sample": known_winner_net,
            "losing_positions_net_pnl_usdc": loser_net,
        },
        "kraken_direction_match_by_lookback_seconds": kraken_match,
        "markets": markets,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--output")
    parser.add_argument("--kraken-candles")
    args = parser.parse_args()
    report = analyze(args.directory, args.kraken_candles)
    text = json.dumps(report, indent=2, default=dict) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")
