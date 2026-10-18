"""Reproduce the wallet study and chronological Kraken validation report."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics


def wilson(wins, count):
    if not count:
        return 0.0
    p, z = wins / count, 1.96
    return (p + z*z/(2*count) - z*math.sqrt(p*(1-p)/count + z*z/(4*count*count))) / (1 + z*z/count)


def quantiles(values):
    values = sorted(values)
    return {name: values[round((len(values)-1)*fraction)] for name, fraction in
            (("p10", .10), ("p25", .25), ("median", .50), ("p75", .75), ("p90", .90))} if values else {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("trader-943-analysis.json"))
    parser.add_argument("--asset", choices=("btc", "eth"), default="btc")
    args = parser.parse_args()
    trades = json.loads((args.directory / "trades.json").read_text())
    activity = json.loads((args.directory / "activity.json").read_text())
    positions = json.loads((args.directory / "positions.json").read_text())
    stats_path = args.directory / "profile-stats.json"
    stats = json.loads(stats_path.read_text()).get("data") if stats_path.exists() else None
    candles = json.loads(args.candles.read_text())["candles"]
    opens = {row["time"] // 1000: float(row["open"]) for row in candles}
    closes = {row["time"] // 1000: float(row["close"]) for row in candles}

    by_condition = defaultdict(list)
    for trade in trades:
        by_condition[trade["conditionId"]].append(trade)
    redeem_outcome = {row["conditionId"]: row.get("outcome") for row in activity
                      if row.get("type") == "REDEEM" and row.get("outcome") in ("Up", "Down")}
    cashflow = defaultdict(float)
    for row in activity:
        if row.get("type") != "TRADE":
            cashflow[row.get("type")] += float(row.get("usdcSize", 0))

    markets = []
    validation = []
    for condition, rows in by_condition.items():
        rows.sort(key=lambda row: row["timestamp"])
        buys = [row for row in rows if row["side"] == "BUY"]
        sells = [row for row in rows if row["side"] == "SELL"]
        directional = [row for row in buys if row.get("outcome") in ("Up", "Down")]
        if not buys:
            continue
        outcome_cost = {outcome: sum(float(row["size"])*float(row["price"])
                                     for row in directional if row["outcome"] == outcome)
                        for outcome in ("Up", "Down")}
        outcomes = {row["outcome"] for row in directional}
        winner, label_source = redeem_outcome.get(condition), "observed_redeem"
        # A resolved single-sided holding with no sale or redemption has no winning
        # token to redeem. This inference is reported separately from direct labels.
        if winner not in ("Up", "Down") and len(outcomes) == 1 and not sells:
            only = next(iter(outcomes))
            winner, label_source = ("Down" if only == "Up" else "Up"), "inferred_single_side_loss"
        slug = rows[0].get("slug", "")
        start_text = slug.rsplit("-", 1)[-1]
        start = int(start_text) if start_text.isdigit() else None
        cost = sum(float(row["size"])*float(row["price"]) for row in buys)
        markets.append({"condition": condition, "slug": slug, "buy_cost": cost,
                        "buy_count": len(buys), "sell_count": len(sells),
                        "both_outcomes": len(outcomes) > 1, "winner": winner,
                        "label_source": label_source,
                        "first_outcome": directional[0]["outcome"] if directional else None,
                        "largest_cost_outcome": max(outcome_cost, key=outcome_cost.get) if directional else None,
                        "first_offset_s": directional[0]["timestamp"] - start if directional and start else None,
                        "last_offset_s": directional[-1]["timestamp"] - start if directional and start else None})

        if not (slug.startswith(f"{args.asset}-updown-5m-") and start is not None and winner in ("Up", "Down")):
            continue
        price, previous, opening = closes.get(start+180), closes.get(start+120), opens.get(start)
        if None in (price, previous, opening) or price == opening or (price-opening)*(previous-opening) <= 0:
            continue
        movement = math.log(price/opening)*10_000
        prediction = "Up" if movement > 0 else "Down"
        validation.append({"start": start, "move_bp": abs(movement), "won": prediction == winner,
                           "label_source": label_source})

    labeled = [row for row in markets if row["winner"] in ("Up", "Down")]
    direct = [row for row in labeled if row["label_source"] == "observed_redeem"]
    validation.sort(key=lambda row: row["start"])
    split = round(len(validation) * .67)
    train, test = validation[:split], validation[split:]

    def evaluate(rows, threshold=5.0):
        selected = [row for row in rows if row["move_bp"] >= threshold]
        wins = sum(row["won"] for row in selected)
        days = (rows[-1]["start"] - rows[0]["start"]) / 86400 if len(rows) > 1 else 0
        return {"eligible_labeled_markets": len(rows), "signals": len(selected), "wins": wins,
                "win_rate": wins/len(selected) if selected else None,
                "wilson_95_lower": wilson(wins, len(selected)),
                "signals_per_calendar_day_in_wallet_sample": len(selected)/days if days else None,
                "direct_redeem_labels": sum(row["label_source"] == "observed_redeem" for row in selected),
                "inferred_loss_labels": sum(row["label_source"] != "observed_redeem" for row in selected)}

    def buckets(rows):
        result = []
        for lower, upper in ((5.0, 7.5), (7.5, None)):
            selected = [row for row in rows if row["move_bp"] >= lower
                        and (upper is None or row["move_bp"] < upper)]
            wins = sum(row["won"] for row in selected)
            result.append({"min_move_bp": lower, "max_move_bp": upper,
                           "samples": len(selected), "wins": wins,
                           "probability_lower": wilson(wins, len(selected))})
        return result

    buys = [row for row in trades if row["side"] == "BUY"]
    sells = [row for row in trades if row["side"] == "SELL"]
    buy_cost = sum(float(row["size"])*float(row["price"]) for row in buys)
    sell_value = sum(float(row["size"])*float(row["price"]) for row in sells)
    report = {
        "wallet": trades[0]["proxyWallet"],
        "data": {"fills": len(trades), "buy_fills": len(buys), "sell_fills": len(sells),
                 "unique_markets": len(markets),
                 "first_trade_utc": datetime.fromtimestamp(min(row["timestamp"] for row in trades), timezone.utc).isoformat(),
                 "last_trade_utc": datetime.fromtimestamp(max(row["timestamp"] for row in trades), timezone.utc).isoformat(),
                 "market_types": Counter(row["slug"].rsplit("-", 1)[0] for row in markets)},
        "official_profile_stats": stats,
        "cashflow_crosscheck": {"buy_cost_from_public_fills": buy_cost,
                                 "sell_receipts_from_public_fills": sell_value,
                                 "non_trade_activity_usdc": dict(cashflow),
                                 "approximate_public_ledger_net": sell_value + sum(cashflow.values()) - buy_cost,
                                 "warning": "The activity ledger and v2 profile statistics use different attribution and fee fields."},
        "observed_behavior": {
            "markets_buying_both_outcomes": sum(row["both_outcomes"] for row in markets),
            "markets_with_any_sell": sum(row["sell_count"] > 0 for row in markets),
            "markets_with_observed_redemption": len({row["conditionId"] for row in activity if row.get("type") == "REDEEM"}),
            "buy_cost_usdc": quantiles(row["buy_cost"] for row in markets),
            "buy_fills_per_market": quantiles(row["buy_count"] for row in markets),
            "first_entry_offset_s": quantiles(row["first_offset_s"] for row in markets if row["first_offset_s"] is not None and 0 <= row["first_offset_s"] <= 900),
            "last_entry_offset_s": quantiles(row["last_offset_s"] for row in markets if row["last_offset_s"] is not None and 0 <= row["last_offset_s"] <= 900),
            "first_selected_outcome_win_rate_labeled": sum(row["first_outcome"] == row["winner"] for row in labeled)/len(labeled),
            "largest_cost_outcome_win_rate_labeled": sum(row["largest_cost_outcome"] == row["winner"] for row in labeled)/len(labeled),
            "direct_winner_labels": len(direct), "total_labels_including_inferred_losses": len(labeled),
            "position_rows_currently_showing_zero_value": sum(float(row.get("curPrice", -1)) == 0 for row in positions),
            "position_rows_gross_initial_value": sum(float(row.get("grossInitialValue", 0)) for row in positions),
        },
        "replica_rule": {"asset": args.asset,
                         "description": f"At 55-60 seconds remaining in {args.asset.upper()} 5m, follow Kraken only when the move from the interval open is at least 5bp, the prior completed minute has the same sign, and the matching Polymarket ask is 0.68-0.77 with fee-adjusted edge.",
                         "train_first_67_percent": evaluate(train),
                         "chronological_holdout_last_33_percent": evaluate(test),
                         "train_move_probability_buckets": buckets(train),
                         "split_timestamp_utc": datetime.fromtimestamp(test[0]["start"], timezone.utc).isoformat() if test else None,
                         "execution": "one fixed-size entry, no opposite-side averaging, no martingale, hold to resolution"},
        "limits": [
            "Public fills reveal actions, not the trader's private signal or resting-order intent.",
            "Loss labels without a redemption are inferred only for single-sided, unsold positions; direct and inferred counts are separated.",
            "The wallet selected which markets to trade, so this is not an all-market randomized backtest.",
            "One-minute Kraken candles cannot recreate sub-minute fills, order-book queues, latency, fees, or executable asks.",
            "The deployed ask range has only sparse historical price proxies; live profitability is not established.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2, default=dict) + "\n")
    print(json.dumps({"fills": len(trades), "markets": len(markets),
                      "official_economic_pnl": (stats or {}).get("all_time_pnl", {}).get("economic_pnl"),
                      "train": report["replica_rule"]["train_first_67_percent"],
                      "test": report["replica_rule"]["chronological_holdout_last_33_percent"]}, indent=2))


if __name__ == "__main__":
    main()
