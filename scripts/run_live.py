"""Rust feed/signal engine with one bounded live entry through a persistent Python SDK."""
import argparse
import asyncio
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import time

from live_executor import ExecutionStopped, buy, connect, encode_response, save, sell_increment, settle, token_balance


def validate_signal(signal, now_ms):
    if not signal["slug"].startswith("btc-updown-5m-") or signal["outcome"] not in ("Up", "Down"):
        raise ExecutionStopped("Unexpected market or outcome")
    if not 0 <= now_ms-signal["received_ms"] <= 500:
        raise ExecutionStopped("Signal is stale")
    if not 2000 < signal["end_ms"]-now_ms <= 30000:
        raise ExecutionStopped("Outside the live entry window")


async def run(args):
    if not args.live and not args.preflight:
        raise ExecutionStopped("Add --live explicitly; use run-paper.sh for paper trading")
    budget = Decimal(args.max_spend)
    if not budget.is_finite() or not Decimal("1.25") <= budget <= Decimal("10"):
        raise ExecutionStopped("Maximum session spend must be $1.25-$10")
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse reuse of any session that could already have posted an order.
    if state_path.exists():
        old = json.loads(state_path.read_text())
        if old.get("buy_submission_intents", 0):
            raise ExecutionStopped("This state already contains a buy intent; inspect the position and use a new state for a deliberately new session")
    lock = open(str(state_path)+".lock", "a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = {"mode": "live", "max_spend": str(budget), "buy_submission_intents": 0,
        "stage": "connect", "round_trip": args.round_trip}
    client = None
    process = None
    try:
        client = await connect(args.env)
        if args.preflight:
            state["stage"] = "preflight_complete"
            print(json.dumps(state, indent=2))
            return True
        state["stage"] = "monitor"
        save(state_path, state)
        process = await asyncio.create_subprocess_exec(str(Path(args.binary).resolve()),
            "--signals-only", "--seconds", str(args.seconds), "--journal", str(args.journal),
            stdout=asyncio.subprocess.PIPE)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                print(line.decode().strip(), flush=True)
                continue
            if event.get("type") != "signal":
                continue
            signal = event["signal"]
            try:
                validate_signal(signal, time.time()*1000)
            except ExecutionStopped:
                continue
            baseline = await token_balance(client, signal["token"])
            if baseline != 0:
                continue  # Never mix a new entry with existing holdings.
            state.update(signal=signal, baseline_shares=str(baseline),
                buy_submission_intents=1, stage="buy_submission")
            save(state_path, state)  # Even a timeout consumes this session's one buy intent.
            response = await buy(client, args.env, signal["token"], budget, Decimal(str(signal["ask"]))+Decimal("0.01"),
                deadline_ms=signal["end_ms"], signal_received_ms=signal["received_ms"])
            state["buy"] = encode_response(response)
            save(state_path, state)
            if not response.ok:
                state["stage"] = "rejected"
                break
            state["stage"] = "settlement"
            save(state_path, state)
            state["buy_settlement_hashes"] = await settle(client, response)
            await asyncio.sleep(2)
            state["position_shares"] = str(await token_balance(client, signal["token"]))
            if args.round_trip:
                state["stage"] = "sell"
                save(state_path, state)
                state["sell_attempts"] = await sell_increment(client, signal["token"], baseline)
                state["remaining_shares"] = str(await token_balance(client, signal["token"]))
                state["stage"] = "round_trip_finished"
            else:
                state["stage"] = "position_held"
            break  # Exactly one buy attempt per durable session; restart never resets it.
        if state["stage"] == "monitor":
            if await process.wait() != 0:
                raise ExecutionStopped("The Rust signal process failed; inspect its stderr")
            state["stage"] = "no_eligible_signal"
    except Exception as exc:
        state["error_type"] = type(exc).__name__
        if isinstance(exc, ExecutionStopped):
            state["reason"] = str(exc)
        state["requires_attention"] = bool(state["buy_submission_intents"])
    finally:
        save(state_path, state)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        if client:
            await client.close()
        lock.close()
    print(json.dumps(state, indent=2))
    return not state.get("error_type") and state.get("stage") != "rejected"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--preflight", action="store_true", help="Authenticate and check restrictions without starting the signal engine or submitting orders")
    p.add_argument("--env", default=".env")
    p.add_argument("--max-spend", default="2", help="All-in budget per session, which has at most one buy intent")
    p.add_argument("--seconds", default=3600, type=int)
    p.add_argument("--round-trip", action="store_true", help="Try selling the fill immediately after settlement")
    p.add_argument("--binary", default="target/release/leadlag")
    p.add_argument("--journal", default="logs/live-signals.jsonl")
    p.add_argument("--state", default="logs/live-state.json")
    args = p.parse_args()
    try:
        success = asyncio.run(run(args))
    except (ExecutionStopped, BlockingIOError) as exc:
        print(type(exc).__name__ + ": " + str(exc))
        success = False
    raise SystemExit(0 if success else 1)
