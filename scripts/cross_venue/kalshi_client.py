"""Kalshi REST client (public market data + optional RSA/Ed25519 auth)."""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("arbot.cross.kalshi")

DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiOrderError(RuntimeError):
    pass


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    title: str
    event_ticker: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    close_time: str | None
    volume_24h: float
    end_ms: int | None = None


@dataclass(frozen=True)
class KalshiBook:
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_bid_size: float
    yes_ask_size: float
    no_bid_size: float
    no_ask_size: float


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN guard


def _parse_end_ms(close_time: str | None) -> int | None:
    if not close_time:
        return None
    try:
        from datetime import datetime, timezone

        text = close_time.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def load_private_key_material():
    """Load Kalshi private key from env. Never logs key bytes."""
    key_id = (os.environ.get("KALSHI_KEY_ID") or "").strip()
    raw = (os.environ.get("KALSHI_PK") or os.environ.get("KALSHI_PRIVATE_KEY") or "").strip()
    path = (os.environ.get("KALSHI_PRIVATE_KEY_PATH") or "").strip()
    pem_text = None
    if path:
        pem_text = Path(path).expanduser().read_text(encoding="utf-8")
    elif raw:
        candidate = Path(raw).expanduser()
        if candidate.exists() and candidate.is_file():
            pem_text = candidate.read_text(encoding="utf-8")
        else:
            pem_text = raw.replace("\\n", "\n")
    if not key_id or not pem_text:
        return None, None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.backends import default_backend

        data = pem_text.encode("utf-8") if "BEGIN" in pem_text else None
        if data is None:
            # Try base64 DER / raw
            try:
                der = base64.b64decode(pem_text)
                key = serialization.load_der_private_key(der, password=None, backend=default_backend())
                return key_id, key
            except Exception:
                # 32-byte hex/ed25519 seed
                try:
                    seed = bytes.fromhex(pem_text)
                    if len(seed) == 32:
                        return key_id, Ed25519PrivateKey.from_private_bytes(seed)
                except Exception:
                    pass
                raise ValueError(
                    "KALSHI_PK must be a PEM private key, a path to a .pem file, "
                    "or base64/hex key material"
                )
        key = serialization.load_pem_private_key(data, password=None, backend=default_backend())
        return key_id, key
    except ImportError as exc:
        raise RuntimeError("cryptography package required for Kalshi auth") from exc


class KalshiClient:
    def __init__(self, base_url: str = DEFAULT_BASE, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.key_id = None
        self._private_key = None
        try:
            self.key_id, self._private_key = load_private_key_material()
        except Exception as exc:
            log.warning("Kalshi auth not loaded: %s", type(exc).__name__)
            self.key_id, self._private_key = None, None

    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self._private_key)

    def _sign_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        ts = str(int(time.time() * 1000))
        path_no_query = path.split("?", 1)[0]
        # Full path from API root including /trade-api/v2...
        if path_no_query.startswith("http"):
            from urllib.parse import urlparse

            path_no_query = urlparse(path_no_query).path
        message = f"{ts}{method.upper()}{path_no_query}".encode("utf-8")
        key = self._private_key
        if isinstance(key, Ed25519PrivateKey):
            signature = key.sign(message)
        else:
            signature = key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        auth: bool = False,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        # Sign path must include /trade-api/v2 prefix when base already has it.
        sign_path = path
        if "/trade-api/v2" in self.base_url and not path.startswith("/trade-api/v2"):
            # base_url already ends with /trade-api/v2; sign that full API path.
            sign_path = "/trade-api/v2" + path
        headers = {"Accept": "application/json", "User-Agent": "Arbot-CrossVenue/1.0"}
        need_auth = auth or json_body is not None or method.upper() in ("POST", "PUT", "DELETE", "PATCH")
        if need_auth:
            if not self.authenticated:
                raise KalshiOrderError("Kalshi credentials missing (KALSHI_KEY_ID / KALSHI_PK)")
            headers.update(self._sign_headers(method, sign_path))
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = self.session.request(
            method, url, params=params, json=json_body, headers=headers, timeout=30
        )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise KalshiOrderError(f"Kalshi HTTP {response.status_code}: {detail}")
        if not response.content:
            return {}
        return response.json()

    def balance_dollars(self) -> float:
        data = self.request("GET", "/portfolio/balance", auth=True)
        if data.get("balance_dollars") is not None:
            return float(data["balance_dollars"])
        # Legacy field is integer cents.
        return float(data.get("balance") or 0) / 100.0

    def buy_ioc(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Immediate-or-cancel buy of YES or NO contracts. Returns fill summary."""
        side = side.lower().strip()
        if side not in ("yes", "no"):
            raise KalshiOrderError("side must be yes or no")
        count = int(count)
        if count < 1:
            raise KalshiOrderError("count must be >= 1")
        if not 0 < price < 1:
            raise KalshiOrderError("price must be in (0,1)")
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "time_in_force": "immediate_or_cancel",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        px = f"{float(price):.4f}"
        if side == "yes":
            body["yes_price_dollars"] = px
        else:
            body["no_price_dollars"] = px
        data = self.request("POST", "/portfolio/orders", json_body=body, auth=True)
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        filled = int(float(order.get("fill_count") or order.get("fill_count_fp") or 0))
        remaining = int(float(order.get("remaining_count") or order.get("remaining_count_fp") or 0))
        avg = float(price)
        # Prefer explicit fill price fields when present.
        for key in ("yes_price_dollars", "no_price_dollars", "yes_price", "no_price"):
            if order.get(key) is not None:
                try:
                    raw = float(order[key])
                    avg = raw / 100.0 if raw > 1.0 else raw
                    break
                except (TypeError, ValueError):
                    pass
        return {
            "ok": filled > 0,
            "order_id": order.get("order_id") or order.get("id"),
            "client_order_id": body["client_order_id"],
            "filled": filled,
            "remaining": remaining,
            "avg_price": avg,
            "side": side,
            "ticker": ticker,
            "raw": order,
        }

    def sell_ioc(self, ticker: str, side: str, count: int, price: float) -> dict[str, Any]:
        """IOC sell to unwind (side = held contract side)."""
        side = side.lower().strip()
        count = int(count)
        if count < 1:
            raise KalshiOrderError("count must be >= 1")
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "count": count,
            "time_in_force": "immediate_or_cancel",
            "client_order_id": str(uuid.uuid4()),
        }
        px = f"{max(0.01, float(price)):.4f}"
        if side == "yes":
            body["yes_price_dollars"] = px
        else:
            body["no_price_dollars"] = px
        data = self.request("POST", "/portfolio/orders", json_body=body, auth=True)
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        filled = int(float(order.get("fill_count") or order.get("fill_count_fp") or 0))
        return {
            "ok": filled > 0,
            "order_id": order.get("order_id") or order.get("id"),
            "filled": filled,
            "avg_price": float(price),
            "raw": order,
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/portfolio/orders/{order_id}", auth=True)

    def list_markets(self, *, limit: int = 200, status: str = "open") -> list[KalshiMarket]:
        out: list[KalshiMarket] = []
        cursor = None
        while len(out) < limit:
            params: dict[str, Any] = {
                "limit": min(200, limit - len(out)),
                "status": status,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            data = self.request("GET", "/markets", params=params)
            rows = data.get("markets") or []
            for row in rows:
                ticker = str(row.get("ticker") or "")
                title = str(row.get("title") or row.get("yes_sub_title") or ticker)
                if not ticker:
                    continue
                close_time = row.get("close_time") or row.get("expected_expiration_time")
                out.append(
                    KalshiMarket(
                        ticker=ticker,
                        title=title,
                        event_ticker=str(row.get("event_ticker") or ""),
                        yes_bid=_f(row.get("yes_bid_dollars")),
                        yes_ask=_f(row.get("yes_ask_dollars")),
                        no_bid=_f(row.get("no_bid_dollars")),
                        no_ask=_f(row.get("no_ask_dollars")),
                        close_time=str(close_time) if close_time else None,
                        volume_24h=float(row.get("volume_24h_fp") or 0),
                        end_ms=_parse_end_ms(str(close_time) if close_time else None),
                    )
                )
            cursor = data.get("cursor")
            if not cursor or not rows:
                break
        return out[:limit]

    def orderbook(self, ticker: str, *, depth: int = 10) -> KalshiBook:
        data = self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        book = data.get("orderbook_fp") or data.get("orderbook") or {}
        yes_levels = book.get("yes_dollars") or book.get("yes") or []
        no_levels = book.get("no_dollars") or book.get("no") or []

        def best_bid(levels) -> tuple[float | None, float]:
            best_p, best_s = None, 0.0
            for level in levels or []:
                try:
                    if isinstance(level, (list, tuple)) and len(level) >= 2:
                        price, size = float(level[0]), float(level[1])
                    elif isinstance(level, dict):
                        price = float(level.get("price") or level.get("price_dollars"))
                        size = float(level.get("size") or level.get("quantity") or level.get("count") or 0)
                    else:
                        continue
                    if best_p is None or price > best_p:
                        best_p, best_s = price, size
                except (TypeError, ValueError):
                    continue
            return best_p, best_s

        yes_bid, yes_bid_size = best_bid(yes_levels)
        no_bid, no_bid_size = best_bid(no_levels)
        # Binary complement: yes ask ≈ 1 - no bid
        yes_ask = (1.0 - no_bid) if no_bid is not None else None
        no_ask = (1.0 - yes_bid) if yes_bid is not None else None
        yes_ask_size = no_bid_size
        no_ask_size = yes_bid_size
        return KalshiBook(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=yes_bid_size,
            yes_ask_size=yes_ask_size,
            no_bid_size=no_bid_size,
            no_ask_size=no_ask_size,
        )
