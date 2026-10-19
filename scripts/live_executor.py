"""Deposit Wallet execution through the official SDK. No automatic order retries."""
import asyncio
from decimal import Decimal, ROUND_DOWN
import json
import logging
import os
from pathlib import Path
import tempfile
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from polymarket import ApiKeyCreds, AsyncSecureClient
from polymarket.environments import PRODUCTION

from check_credentials import read_env

UNITS = Decimal(1_000_000)


class ExecutionStopped(RuntimeError):
    pass


class OrderNotSubmitted(ExecutionStopped):
    """A deterministic local guard rejected the buy before post_order."""


def enabled_env(path):
    env = read_env(path)
    if env.get("LIVE_KILL", "").lower() in ("1", "true", "yes", "on"):
        raise ExecutionStopped("LIVE_KILL is enabled")
    for name in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_SIGNATURE_TYPE"):
        if not env.get(name):
            raise ExecutionStopped(f"Missing {name}")
    if env["POLY_SIGNATURE_TYPE"] != "3":
        raise ExecutionStopped("This live adapter has only been validated for Deposit Wallet signature type 3")
    return env


def clob_auth_headers(private_key):
    """EIP-712 ClobAuth headers for CLOB L1 key creation / derivation."""
    account = Account.from_key(private_key)
    ts = str(int(time.time()))
    typed = {"types": {
        "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"}],
        "ClobAuth": [{"name": "address", "type": "address"}, {"name": "timestamp", "type": "string"}, {"name": "nonce", "type": "uint256"}, {"name": "message", "type": "string"}],
    }, "primaryType": "ClobAuth", "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": 137},
        "message": {"address": account.address, "timestamp": ts, "nonce": 0,
            "message": "This message attests that I control the given wallet"}}
    sig = account.sign_message(encode_typed_data(full_message=typed)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return {"POLY_ADDRESS": account.address, "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts, "POLY_NONCE": "0"}, account


def derive_existing(env):
    """Return signer-bound API credentials, creating them once if this wallet is new."""
    headers, _account = clob_auth_headers(env["POLY_PRIVATE_KEY"])
    response = requests.get("https://clob.polymarket.com/auth/derive-api-key",
                            timeout=15, headers=headers)
    if response.status_code == 400:
        # Brand-new signers have nothing to derive until the first create succeeds.
        created = requests.post("https://clob.polymarket.com/auth/api-key",
                                timeout=15, headers=clob_auth_headers(env["POLY_PRIVATE_KEY"])[0])
        if created.status_code != 200:
            raise ExecutionStopped(f"API-key creation returned HTTP {created.status_code}")
        return ApiKeyCreds.model_validate(created.json())
    if response.status_code != 200:
        raise ExecutionStopped(f"Existing-key derivation returned HTTP {response.status_code}")
    return ApiKeyCreds.model_validate(response.json())


def trading_wallet(env):
    """Resolve the Deposit Wallet the SDK should trade through.

    POLY_FUNDER should be the Deposit Wallet address. When operators paste the EOA
    (signer == funder), omit wallet so the SDK defaults to that signer's Deposit Wallet
    instead of treating the account as a zero-balance EOA.
    """
    account = Account.from_key(env["POLY_PRIVATE_KEY"])
    funder = env["POLY_FUNDER"]
    if funder.lower() == account.address.lower():
        return None
    return funder


async def connect(path, *, redemption=False):
    env = enabled_env(path)
    logging.disable(logging.CRITICAL)  # Never log request headers, signatures, or keys.
    geo = await asyncio.to_thread(requests.get, "https://polymarket.com/api/geoblock", timeout=15)
    if geo.status_code != 200 or geo.json().get("blocked") is not False:
        raise ExecutionStopped("Geoblock check failed or trading is blocked")
    credentials = await asyncio.to_thread(derive_existing, env)
    api_key = None
    if redemption:
        from polymarket.auth import RelayerApiKey
        for name in ("RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS"):
            if not env.get(name):
                raise ExecutionStopped(f"Missing {name} for resolution redemption")
        api_key = RelayerApiKey(key=env["RELAYER_API_KEY"], address=env["RELAYER_API_KEY_ADDRESS"])
    return await AsyncSecureClient.create(
        private_key=env["POLY_PRIVATE_KEY"],
        wallet=trading_wallet(env),
        credentials=credentials,
        api_key=api_key,
    )


def best(book):
    if not book.bids or not book.asks:
        raise ExecutionStopped("Two-sided liquidity is required")
    bid = max(book.bids, key=lambda x: x.price)
    ask = min(book.asks, key=lambda x: x.price)
    if bid.price <= 0 or ask.price < bid.price:
        raise ExecutionStopped("Invalid or crossed book")
    return bid, ask


def encode_response(response):
    if not response.ok:
        # Error text can contain echoed signed payloads; retain only the code.
        return {"ok": False, "code": response.code}
    return {"ok": True, "order_id": response.order_id, "status": response.status,
        "making_amount": str(response.making_amount), "taking_amount": str(response.taking_amount),
        "trade_ids": list(response.trade_ids), "transaction_hashes": list(response.transactions_hashes)}


async def token_balance(client, token):
    data = await client.get_balance_allowance(asset_type="CONDITIONAL", token_id=token)
    return Decimal(data.balance) / UNITS


async def settle(client, response):
    if not response.ok:
        raise ExecutionStopped("Order did not report a fill; no automatic resubmission")
    # Some accepted market orders include transaction hashes before trade_ids arrive.
    if not response.trade_ids and not response.transactions_hashes:
        order = await client.get_order(order_id=response.order_id)
        if order.associate_trades:
            response = response.model_copy(update={"trade_ids": order.associate_trades})
    hashes = await client.wait_for_order_fill_settlement(response, timeout_s=60)
    if not hashes:
        raise ExecutionStopped("Settlement did not return a confirmed transaction")
    for tx_hash in hashes:
        deadline = time.monotonic()+60
        while True:
            rpc = await asyncio.to_thread(requests.post, PRODUCTION._config.rpc_url,
                json={"jsonrpc":"2.0", "id":1, "method":"eth_getTransactionReceipt", "params":[tx_hash]}, timeout=15)
            rpc.raise_for_status()
            receipt = rpc.json().get("result")
            if receipt:
                if receipt.get("status") != "0x1":
                    raise ExecutionStopped("Settlement transaction reverted")
                break
            if time.monotonic() >= deadline:
                raise ExecutionStopped("Settlement receipt is still pending")
            await asyncio.sleep(1)
    return list(hashes)


async def place_limit_buy(client, env_path, token, price, size, *, expiration=None):
    """Post one GTC-style limit BUY; caller polls for fills / partial fills."""
    enabled_env(env_path)
    price = Decimal(str(price))
    size = Decimal(str(size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if not price.is_finite() or not size.is_finite() or not 0 < price < 1 or size <= 0:
        raise OrderNotSubmitted("Invalid limit price or size")
    book = await client.get_order_book(token_id=token)
    if size < book.min_order_size:
        raise OrderNotSubmitted("Limit size below market minimum")
    enabled_env(env_path)
    return await client.place_limit_order(
        token_id=token, price=str(price), size=str(size), side="BUY",
        post_only=False, expiration=expiration)


async def wait_limit_fill(client, order_id, token, baseline, target_shares, *, timeout_s=90.0):
    """Poll until filled shares reach target, or return the partial fill on timeout."""
    target = Decimal(str(target_shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    baseline = Decimal(str(baseline))
    deadline = time.monotonic() + timeout_s
    best = Decimal(0)
    while time.monotonic() < deadline:
        balance = await token_balance(client, token)
        filled = max(Decimal(0), balance - baseline).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        best = max(best, filled)
        if filled >= target:
            return filled
        try:
            order = await client.get_order(order_id=order_id)
            matched = Decimal(str(getattr(order, "size_matched", 0) or 0))
            best = max(best, matched.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
            status = str(getattr(order, "status", "") or "").lower()
            if status in ("canceled", "cancelled", "expired") and best > 0:
                return best
            if status in ("canceled", "cancelled", "expired"):
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    return best


async def merge_positions(client, env_path, condition_id, amount="max"):
    """Merge equal YES+NO into USDC via CTF mergePositions (gasless when relayer configured)."""
    enabled_env(env_path)
    if not condition_id:
        raise OrderNotSubmitted("Missing condition_id for merge")
    handle = await client.merge_positions(condition_id=condition_id, amount=amount)
    result = await handle.wait()
    return result


async def buy(client, env_path, token, budget, max_price, *, deadline_ms=None, signal_received_ms=None):
    enabled_env(env_path)
    budget = Decimal(str(budget))
    if not budget.is_finite() or budget < Decimal("1.25"):
        raise OrderNotSubmitted("Budget must be finite and at least $1.25")
    # Leave 20% room below the caller's all-in cap. Never retry a POST on errors.
    amount = (budget * Decimal("0.8")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    book = await client.get_order_book(token_id=token)
    bid, ask = best(book)
    max_price = Decimal(str(max_price))
    if ask.price > max_price or ask.price - bid.price > Decimal("0.02"):
        raise OrderNotSubmitted("Price or spread exceeded the entry guard")
    if (amount / max_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN) < book.min_order_size:
        raise OrderNotSubmitted("Budget cannot cover the minimum share quantity")
    if ask.size * ask.price < amount:
        raise OrderNotSubmitted("Insufficient top-of-book depth for the test")
    signed = await client.create_market_order(token_id=token, side="BUY", amount=str(amount),
        max_spend=str(budget * Decimal("0.9")), max_price=str(max_price), order_type="FOK")
    if Decimal(signed.maker_amount) / UNITS > amount:
        raise OrderNotSubmitted("Signed order exceeded the notional limit")
    enabled_env(env_path)
    if deadline_ms is not None and time.time()*1000 >= deadline_ms-1000:
        raise OrderNotSubmitted("Market expiry is too close")
    if signal_received_ms is not None and not 0 <= time.time()*1000-signal_received_ms <= 1000:
        raise OrderNotSubmitted("Signal became stale before order submission")
    return await client.post_order(signed)


async def buy_shares(client, env_path, token, shares, max_price):
    """FOK buy for an exact share target (arb legs). One POST; no retries."""
    enabled_env(env_path)
    shares = Decimal(str(shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    max_price = Decimal(str(max_price))
    if not shares.is_finite() or not max_price.is_finite() or shares <= 0 or not 0 < max_price < 1:
        raise OrderNotSubmitted("Invalid share quantity or max price")
    book = await client.get_order_book(token_id=token)
    bid, ask = best(book)
    if ask.price > max_price:
        raise OrderNotSubmitted("ask_above_limit")
    fill_shares = min(shares, ask.size).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if fill_shares < book.min_order_size:
        raise OrderNotSubmitted("size_below_minimum")
    amount = (fill_shares * ask.price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    max_spend = (fill_shares * max_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if amount < Decimal("0.01") or max_spend < amount:
        raise OrderNotSubmitted("notional_too_small")
    baseline = await token_balance(client, token)
    signed = await client.create_market_order(
        token_id=token, side="BUY", amount=str(amount),
        max_spend=str(max_spend), max_price=str(max_price), order_type="FOK")
    if Decimal(signed.maker_amount) / UNITS > max_spend:
        raise OrderNotSubmitted("Signed order exceeded the notional limit")
    enabled_env(env_path)
    response = await client.post_order(signed)
    if not response.ok:
        return response, Decimal(0), Decimal(0)
    await settle(client, response)
    after = await token_balance(client, token)
    got = max(Decimal(0), after - baseline).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if got <= 0 and response.taking_amount:
        got = (Decimal(response.taking_amount) / UNITS).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    spent = Decimal(0)
    if response.making_amount:
        spent = (Decimal(response.making_amount) / UNITS).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if spent <= 0 and got > 0:
        spent = (got * ask.price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return response, got, spent


async def collateral_available(client):
    balance = await client.get_balance_allowance(asset_type="COLLATERAL")
    return Decimal(balance.balance) / UNITS


async def sell_increment(client, token, baseline, attempts=3):
    """Sell only shares added above baseline; never resubmit an ambiguous POST."""
    results = []
    for _ in range(attempts):
        balance = await token_balance(client, token)
        shares = max(Decimal(0), balance - baseline).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if shares <= 0:
            break
        book = await client.get_order_book(token_id=token)
        bid, _ = best(book)
        if shares < book.min_order_size:
            results.append({"ok": False, "code": "remaining_below_market_minimum", "shares": str(shares)})
            break
        min_price = max(book.tick_size, bid.price - book.tick_size * 2)
        signed = await client.create_market_order(token_id=token, side="SELL", shares=str(shares),
            min_price=str(min_price), order_type="FOK")
        response = await client.post_order(signed)
        result = encode_response(response)
        results.append(result)
        if response.ok:
            result["settled_transaction_hashes"] = await settle(client, response)
            await asyncio.sleep(2)
        elif response.code not in ("unmatched", "fok_order_not_filled", "fok_order_not_fillable"):
            break
        else:
            await asyncio.sleep(1)
    return results


async def sell_position(client, env_path, token, shares, trigger_price):
    """Submit one protected FOK exit for an exact known position."""
    enabled_env(env_path)
    shares=Decimal(str(shares)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
    trigger=Decimal(str(trigger_price))
    if not shares.is_finite() or not trigger.is_finite() or shares <= 0 or not 0 < trigger < 1:
        raise OrderNotSubmitted('Invalid exit quantity or trigger')
    book=await client.get_order_book(token_id=token);bid,_=best(book)
    eligible_depth=sum((level.size for level in book.bids if level.price>=trigger),Decimal(0))
    if bid.price < trigger or shares < book.min_order_size or eligible_depth < shares:
        raise OrderNotSubmitted('Exit book no longer meets the trigger')
    min_price=max(trigger,book.tick_size)
    signed=await client.create_market_order(token_id=token,side='SELL',shares=str(shares),
        min_price=str(min_price),order_type='FOK')
    signed_shares=Decimal(signed.maker_amount)/UNITS
    signed_proceeds=Decimal(signed.taker_amount)/UNITS
    if signed_shares > shares or signed_proceeds < shares*min_price:
        raise OrderNotSubmitted('Signed exit fell below the protected proceeds')
    enabled_env(env_path)
    return await client.post_order(signed)


def save(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(report, file, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
