"""One bounded real BUY and an attempt to unwind only its resulting inventory."""
import argparse
import asyncio
from decimal import Decimal
from datetime import datetime
import json
from pathlib import Path
import time

import requests
from live_executor import (ExecutionStopped, best, buy, connect, encode_response,
    save, sell_increment, settle, token_balance)


async def choose(client, budget):
    markets = []
    for interval in (300, 900):
        start = int(time.time()) // interval * interval
        end = start + interval
        if end - time.time() < 120:
            continue
        slug = f"btc-updown-{interval // 60}m-{start}"
        result = await asyncio.to_thread(requests.get, "https://gamma-api.polymarket.com/markets",
            params={"slug": slug}, timeout=15)
        if result.status_code == 200:
            markets.extend((m, end, 0) for m in result.json())
    # A longer-lived BTC market may fit a $2 test when five-minute minimum sizes do not.
    result = await asyncio.to_thread(requests.get, "https://gamma-api.polymarket.com/markets",
        params={"active":"true", "closed":"false", "order":"volume24hr", "ascending":"false", "limit":100}, timeout=15)
    if result.status_code == 200:
        for m in result.json():
            if "bitcoin" not in m.get("question", "").lower() or float(m.get("volume24hr", 0)) < 50000:
                continue
            end = int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp())
            markets.append((m, end, 1))
    candidates = []
    for market, end, priority in markets:
        if market.get("closed") or not market.get("acceptingOrders") or end-time.time()<120:
            continue
        ids = json.loads(market["clobTokenIds"])
        outcomes = json.loads(market["outcomes"])
        prices = json.loads(market.get("outcomePrices", "[]"))
        for i, (token, outcome) in enumerate(zip(ids, outcomes)):
            if len(prices) == len(ids) and not Decimal("0.08") <= Decimal(prices[i]) <= Decimal("0.3"):
                continue
            book = await client.get_order_book(token_id=token)
            try:
                bid, ask = best(book)
            except ExecutionStopped:
                continue
            cap = ask.price + book.tick_size
            amount = budget * Decimal("0.8")
            if (Decimal("0.1") <= ask.price <= Decimal("0.3")
                    and ask.price - bid.price <= Decimal("0.02")
                    and amount / cap >= book.min_order_size
                    and ask.size * ask.price >= amount
                    and bid.size >= amount / ask.price
                    and await token_balance(client, token) == 0):
                candidates.append((priority, ask.price-bid.price, market["slug"], token, outcome, cap, end))
    if not candidates:
        raise ExecutionStopped("No liquid BTC outcome fits the $2 cap, minimum size, spread, and >=120s expiry guards")
    _, spread, slug, token, outcome, cap, end = min(candidates)
    return spread, slug, token, outcome, cap, end


async def run(args):
    budget = Decimal(args.max_spend)
    if not budget.is_finite() or not Decimal("1.25") <= budget <= Decimal("2"):
        raise ExecutionStopped("This test is capped at the user-authorized $2")
    if Path(args.report).exists() and args.execute:
        raise ExecutionStopped("Report already exists; inspect it rather than repeating a real buy")
    report = {"mode": "live_test" if args.execute else "preflight", "authorized_cap": str(budget),
        "buy_posts": 0, "success": False, "stage": "connect"}
    client = None
    token = None
    baseline = Decimal(0)
    bought = False
    try:
        client = await connect(args.env)
        report["stage"] = "discover"
        _, slug, token, outcome, cap, end = await choose(client, budget)
        report.update(slug=slug, token=token, outcome=outcome, max_price=str(cap), end_timestamp=end)
        baseline = await token_balance(client, token)
        before = await client.get_balance_allowance(asset_type="COLLATERAL")
        report["collateral_before_raw"] = before.balance
        report["baseline_shares"] = str(baseline)
        save(args.report, report)
        if not args.execute:
            report.update(stage="preflight_complete", success=True)
            return report
        report.update(stage="buy_submission", buy_posts=1)
        save(args.report, report)  # Durable intent before POST; never automatic buy retries.
        response = await buy(client, args.env, token, budget, cap)
        report["buy"] = encode_response(response)
        save(args.report, report)
        if not response.ok:
            raise ExecutionStopped("Buy rejected; no second buy will be submitted")
        bought = True
        report["stage"] = "buy_settlement"
        report["buy_settlement_hashes"] = await settle(client, response)
        save(args.report, report)
        for _ in range(10):
            current = await token_balance(client, token)
            if current > baseline:
                break
            await asyncio.sleep(2)
        report["shares_after_buy"] = str(current)
        if current <= baseline:
            raise ExecutionStopped("Settled buy not reflected in conditional balance yet")
        report["stage"] = "sell"
        save(args.report, report)
        report["sell_attempts"] = await sell_increment(client, token, baseline)
        remaining = await token_balance(client, token)
        after = await client.get_balance_allowance(asset_type="COLLATERAL")
        report.update(remaining_test_shares=str(max(Decimal(0), remaining-baseline)),
            collateral_after_raw=after.balance,
            collateral_change=str(Decimal(after.balance-before.balance)/Decimal(1_000_000)))
        report["success"] = any(s.get("settled_transaction_hashes") for s in report["sell_attempts"]) and remaining-baseline < Decimal("0.01")
        report["stage"] = "complete" if report["success"] else "inventory_requires_attention"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, ExecutionStopped):
            report["reason"] = str(exc)
        # If order submission was ambiguous, preserve its intent and stop rather than buy again.
        if client and token and report["buy_posts"]:
            try:
                report["remaining_test_shares"] = str(max(Decimal(0), await token_balance(client, token)-baseline))
            except Exception:
                report["remaining_test_shares"] = "unknown"
        report["buy_accepted"] = bought
    finally:
        save(args.report, report)
        if client:
            await client.close()
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="~/Downloads/weathermarket-setmerge/.env")
    p.add_argument("--max-spend", default="2")
    p.add_argument("--execute", action="store_true", help="Submit the real buy and attempt its unwind")
    p.add_argument("--report", default="live-order-test.json")
    args = p.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        report = {"success": False, "error_type": type(exc).__name__}
        if isinstance(exc, ExecutionStopped):
            report["reason"] = str(exc)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report.get("success") else 1)
