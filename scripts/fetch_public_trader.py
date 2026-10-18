"""Download a wallet's public Polymarket history without API offset truncation."""
import argparse
import json
from pathlib import Path
import time

import requests


BASE = "https://data-api.polymarket.com"


def get(path, **params):
    for attempt in range(8):
        response = requests.get(BASE + path, params=params, timeout=30)
        if response.status_code != 429:
            break
        retry_after = response.headers.get("Retry-After")
        time.sleep(float(retry_after) if retry_after else min(30, 2 ** attempt))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected response from {path}")
    return payload


def get_object(path, **params):
    for attempt in range(8):
        response = requests.get(BASE + path, params=params, timeout=30)
        if response.status_code != 429:
            break
        time.sleep(min(30, 2 ** attempt))
    response.raise_for_status()
    return response.json()


def history(path, user, limit, **extra):
    """Walk backwards in time; offsets alone are capped at 10,000 rows."""
    rows, end = [], int(time.time()) + 1
    while True:
        page = get(path, user=user, limit=limit, offset=0, start=1, end=end, **extra)
        if not page:
            break
        rows.extend(page)
        oldest = min(int(row["timestamp"]) for row in page)
        if len(page) < limit or oldest <= 1:
            break
        end = oldest - 1
        time.sleep(0.5)
    unique = {}
    for row in rows:
        key = (row.get("transactionHash"), row.get("type"), row.get("asset"),
               row.get("side"), row.get("timestamp"), row.get("size"), row.get("price"))
        unique[key] = row
    return sorted(unique.values(), key=lambda row: int(row.get("timestamp", 0)))


def offset_history(path, user, limit=500):
    rows = []
    for offset in range(0, 10_001, limit):
        page = get(path, user=user, limit=limit, offset=offset,
                   sortBy="TIMESTAMP", sortDirection="DESC")
        rows.extend(page)
        if len(page) < limit:
            break
        time.sleep(0.5)
    unique = {row.get("conditionId", json.dumps(row, sort_keys=True)): row for row in rows}
    return list(unique.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wallet")
    parser.add_argument("--output-root", default="research/data")
    args = parser.parse_args()
    wallet = args.wallet.lower()
    if not (wallet.startswith("0x") and len(wallet) == 42):
        raise SystemExit("wallet must be a 0x-prefixed 20-byte address")
    directory = Path(args.output_root) / wallet
    directory.mkdir(parents=True, exist_ok=True)

    datasets = {
        # takerOnly defaults to true; false is essential when studying order style.
        "trades.json": history("/trades", wallet, 10_000, takerOnly="false"),
        "activity.json": history("/activity", wallet, 500),
        "positions.json": get("/positions", user=wallet, limit=500, offset=0),
        "closed-positions.json": offset_history("/closed-positions", wallet),
    }
    for name, rows in datasets.items():
        (directory / name).write_text(json.dumps(rows, separators=(",", ":")) + "\n")
    stats = get_object("/v2/user-stats", user=wallet)
    (directory / "profile-stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    manifest = {
        "wallet": wallet,
        "fetched_at": int(time.time()),
        "counts": {name: len(rows) for name, rows in datasets.items()},
        "first_trade": datasets["trades.json"][0].get("timestamp") if datasets["trades.json"] else None,
        "last_trade": datasets["trades.json"][-1].get("timestamp") if datasets["trades.json"] else None,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
