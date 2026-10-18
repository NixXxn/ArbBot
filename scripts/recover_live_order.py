"""Reconcile and unwind a recorded test buy; this tool can never submit a BUY."""
import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
from polymarket.models.clob.order_response import AcceptedOrder

from live_executor import ExecutionStopped, connect, save, sell_increment, settle, token_balance


async def recover(args):
    report = json.loads(Path(args.report).read_text())
    if not report.get("buy", {}).get("ok"):
        raise ExecutionStopped("No accepted buy is recorded; reconcile the order before selling")
    baseline = Decimal(report["baseline_shares"])
    client = await connect(args.env)
    try:
        buy = report["buy"]
        response = AcceptedOrder(order_id=buy["order_id"],status=buy["status"],
            making_amount=buy["making_amount"],taking_amount=buy["taking_amount"],
            trade_ids=tuple(buy["trade_ids"]),transactions_hashes=tuple(buy["transaction_hashes"]))
        report["buy_settlement_hashes"] = await settle(client,response)
        report["shares_after_buy"] = str(await token_balance(client,report["token"]))
        report["stage"] = "reconciled"
        save(args.report,report)
        if args.execute:
            report["stage"] = "sell_recovery"
            save(args.report,report)
            report.setdefault("sell_attempts",[]).extend(await sell_increment(client,report["token"],baseline))
            remaining = await token_balance(client,report["token"])
            after = await client.get_balance_allowance(asset_type="COLLATERAL")
            report.update(remaining_test_shares=str(max(Decimal(0),remaining-baseline)),
                collateral_after_raw=after.balance,
                collateral_change=str(Decimal(after.balance-report["collateral_before_raw"])/Decimal(1_000_000)))
            report["success"] = any(s.get("settled_transaction_hashes") for s in report["sell_attempts"]) and remaining-baseline < Decimal("0.01")
            report["stage"] = "complete" if report["success"] else "inventory_requires_attention"
            if report["success"]:
                report.pop("error_type",None);report.pop("reason",None)
        save(args.report,report)
        return report
    finally:
        await client.close()


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--report",required=True)
    p.add_argument("--env",default="~/Downloads/weathermarket-setmerge/.env")
    p.add_argument("--execute",action="store_true",help="Sell only the recorded buy's remaining shares")
    args=p.parse_args()
    try:
        result=asyncio.run(recover(args));print(json.dumps(result,indent=2))
    except Exception as e:
        print(json.dumps({"error_type":type(e).__name__,"reason":str(e) if isinstance(e,ExecutionStopped) else "Inspect the saved report; no buy was submitted"}));raise SystemExit(1)
