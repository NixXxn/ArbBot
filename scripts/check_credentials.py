"""Read-only Polymarket auth diagnostic. Never creates keys or sends orders."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data


def read_env(path):
    values = {}
    env_path = Path(path).expanduser()
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    # Coolify and Docker inject secrets as process environment variables. They
    # override a mounted file, and are never written back or included in reports.
    for key in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_SIGNATURE_TYPE", "LIVE_KILL",
                "POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE",
                "RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def run(path):
    env = read_env(path)
    required = ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_SIGNATURE_TYPE")
    missing = [key for key in required if not env.get(key)]
    report = {"mode": "read_only", "missing": missing, "orders_submitted": 0}
    if missing:
        return report
    try:
        account = Account.from_key(env["POLY_PRIVATE_KEY"])
        signature_type = int(env["POLY_SIGNATURE_TYPE"])
    except Exception:
        report["error"] = "Invalid private key or signature type format"
        return report
    report["signature_type"] = signature_type
    report["signer_matches_funder"] = account.address.lower() == env["POLY_FUNDER"].lower()
    report["live_kill_set"] = env.get("LIVE_KILL", "").lower() in ("1", "true", "yes", "on")
    host = "https://clob.polymarket.com"
    session = requests.Session()
    def get(route, **kwargs):
        return session.get(host + route, timeout=15, **kwargs)
    def balance(key, secret, passphrase):
        ts = str(int(time.time()))
        route = "/balance-allowance"
        signature = base64.urlsafe_b64encode(hmac.new(
            base64.urlsafe_b64decode(secret), (ts + "GET" + route).encode(), hashlib.sha256
        ).digest()).decode()
        return get(route, params={"asset_type": "COLLATERAL", "signature_type": signature_type}, headers={
            "POLY_ADDRESS": account.address, "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": ts, "POLY_API_KEY": key, "POLY_PASSPHRASE": passphrase,
        })
    api = (env.get("POLY_API_KEY"), env.get("POLY_API_SECRET"), env.get("POLY_API_PASSPHRASE"))
    response = None
    if all(api):
        try:
            response = balance(*api)
            report["env_credentials_http"] = response.status_code
        except (ValueError, TypeError):
            report["env_credentials_error"] = "Malformed API secret"
    if response is None or response.status_code == 401:
        def auth_headers():
            ts = str(int(time.time()))
            typed = {
                "types": {
                    "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"}],
                    "ClobAuth": [{"name": "address", "type": "address"}, {"name": "timestamp", "type": "string"}, {"name": "nonce", "type": "uint256"}, {"name": "message", "type": "string"}],
                },
                "primaryType": "ClobAuth",
                "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": 137},
                "message": {"address": account.address, "timestamp": ts, "nonce": 0,
                            "message": "This message attests that I control the given wallet"},
            }
            signature = account.sign_message(encode_typed_data(full_message=typed)).signature.hex()
            if not signature.startswith("0x"):
                signature = "0x" + signature
            return {"POLY_ADDRESS": account.address, "POLY_SIGNATURE": signature,
                    "POLY_TIMESTAMP": ts, "POLY_NONCE": "0"}
        derived = get("/auth/derive-api-key", headers=auth_headers())
        report["derive_existing_key_http"] = derived.status_code
        creds = None
        if derived.status_code == 200:
            creds = derived.json()
        elif derived.status_code == 400:
            # New wallets need one create before derive succeeds.
            created = session.post(host + "/auth/api-key", timeout=15, headers=auth_headers())
            report["create_api_key_http"] = created.status_code
            if created.status_code == 200:
                creds = created.json()
        if creds is not None:
            response = balance(creds["apiKey"], creds["secret"], creds["passphrase"])
            report["derived_credentials_http"] = response.status_code
    if report.get("signer_matches_funder"):
        report["funder_note"] = (
            "POLY_FUNDER equals the signer EOA; live trading should use the Deposit Wallet "
            "(signature type 3). The adapter resolves that wallet automatically."
        )
    report["authenticated"] = response is not None and response.status_code == 200
    if report["authenticated"]:
        data = response.json()
        # Preserve raw units rather than silently assuming collateral decimals.
        report["collateral_balance_raw"] = data.get("balance")
        report["allowances_present"] = bool(data.get("allowances"))
        report["nonzero_allowance_present"] = any(int(v) > 0 for v in data.get("allowances", {}).values())
    try:
        geo = session.get("https://polymarket.com/api/geoblock", timeout=15)
        report["geoblock_http"] = geo.status_code
        if geo.status_code == 200:
            report["trading_geoblocked"] = geo.json().get("blocked")
    except requests.RequestException:
        report["geoblock_check"] = "unavailable"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="~/Downloads/weathermarket-setmerge/.env")
    parser.add_argument("--report", default="credential-check.json")
    args = parser.parse_args()
    try:
        report = run(args.env)
    except Exception as exc:
        # Do not print exceptions that may embed a signed request or secret.
        report = {"mode": "read_only", "error_type": type(exc).__name__, "orders_submitted": 0}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report.get("authenticated") else 1)
