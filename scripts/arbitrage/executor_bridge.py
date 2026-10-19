"""Paper (and optional live) execution bridge for dual-leg arb."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .config import ArbConfig
from .fees import leg_fee
from .quote import fetch_book
from .strategy import ArbEngine, Intent

log = logging.getLogger("arbot.executor")


@dataclass
class FillResult:
    ok: bool
    shares: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    notional: float = 0.0
    order_id: str | None = None
    reason: str = ""


class BookClient(Protocol):
    def buy(
        self, token: str, shares: float, limit_price: float, *, fill_at_limit: bool = False
    ) -> FillResult: ...
    def sell(self, token: str, shares: float, limit_price: float) -> FillResult: ...


class PaperBookClient:
    """Paper fills: taker against live ask, or papertrader-style fill-at-limit."""

    def __init__(self, cfg: ArbConfig, session=None):
        self.cfg = cfg
        self.session = session
        self._n = 0

    def buy(
        self, token: str, shares: float, limit_price: float, *, fill_at_limit: bool = False
    ) -> FillResult:
        self._n += 1
        # Papertrader maker path: fill at posted limit with no ask walk.
        if fill_at_limit or (
            getattr(self.cfg, "paper_mode", True)
            and getattr(self.cfg, "paper_fak", True)
            and bool(getattr(self.cfg, "force_fill_at_limit", False))
        ):
            if shares <= 0 or limit_price <= 0:
                return FillResult(False, reason="bad_limit_fill")
            notional = shares * limit_price
            return FillResult(
                True,
                shares=shares,
                avg_price=limit_price,
                fee=0.0,
                notional=notional,
                order_id=f"paper-limit-{self._n}-{int(time.time()*1000)}",
            )

        try:
            book = fetch_book(token, session=self.session)
        except Exception as exc:
            return FillResult(False, reason=f"book_error:{exc}")
        if book.ask is None or book.ask > limit_price + 1e-9:
            return FillResult(False, reason="ask_above_limit")
        fill_shares = min(shares, book.ask_size)
        if fill_shares <= 0:
            return FillResult(False, reason="no_size")
        px = book.ask
        notional = fill_shares * px
        fee = leg_fee(notional, px, taker=True, cfg=self.cfg)
        return FillResult(
            True,
            shares=fill_shares,
            avg_price=px,
            fee=fee,
            notional=notional,
            order_id=f"paper-buy-{self._n}-{int(time.time()*1000)}",
        )

    def sell(self, token: str, shares: float, limit_price: float) -> FillResult:
        self._n += 1
        try:
            book = fetch_book(token, session=self.session)
        except Exception as exc:
            return FillResult(False, reason=f"book_error:{exc}")
        if book.bid is None or book.bid < 0.01:
            return FillResult(False, reason="no_bid")
        px = book.bid
        fill_shares = min(shares, book.bid_size if book.bid_size > 0 else shares)
        notional = fill_shares * px
        fee = leg_fee(notional, px, taker=True, cfg=self.cfg)
        return FillResult(
            True,
            shares=fill_shares,
            avg_price=px,
            fee=fee,
            notional=notional,
            order_id=f"paper-sell-{self._n}-{int(time.time()*1000)}",
        )


class LiveBookClient:
    """Real CLOB FOK buys/sells via live_executor. Never synthesizes fills."""

    def __init__(self, cfg: ArbConfig, env_path: str, *, client=None, loop: asyncio.AbstractEventLoop | None = None):
        self.cfg = cfg
        self.env_path = env_path
        self._client = client
        self._loop = loop or asyncio.new_event_loop()
        self._owns_loop = loop is None

    def connect(self, *, redemption: bool = True):
        from live_executor import connect

        self._client = self._run(connect(self.env_path, redemption=redemption))
        return self._client

    def close(self) -> None:
        if self._owns_loop and self._loop and not self._loop.is_closed():
            self._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def buy(
        self, token: str, shares: float, limit_price: float, *, fill_at_limit: bool = False
    ) -> FillResult:
        if fill_at_limit:
            return FillResult(False, reason="live_rejects_synthetic_fill")
        if self._client is None:
            return FillResult(False, reason="not_connected")
        from live_executor import ExecutionStopped, OrderNotSubmitted, buy_shares, encode_response

        from .fees import leg_fee as fee_fn

        try:
            response, got, spent = self._run(
                buy_shares(self._client, self.env_path, token, shares, limit_price)
            )
        except OrderNotSubmitted as exc:
            return FillResult(False, reason=str(exc) or "order_not_submitted")
        except ExecutionStopped as exc:
            return FillResult(False, reason=str(exc) or "execution_stopped")
        except Exception as exc:
            log.exception("live buy failed")
            return FillResult(False, reason=f"live_buy_error:{type(exc).__name__}")

        if not response.ok or got <= 0:
            code = getattr(response, "code", None) or "no_fill"
            return FillResult(False, reason=str(code), order_id=getattr(response, "order_id", None))
        avg = float(spent / got) if got > 0 and spent > 0 else float(limit_price)
        shares_f = float(got)
        notional = float(spent) if spent > 0 else shares_f * avg
        fee = fee_fn(notional, avg, taker=True, cfg=self.cfg)
        encoded = encode_response(response)
        return FillResult(
            True,
            shares=shares_f,
            avg_price=avg,
            fee=fee,
            notional=notional,
            order_id=encoded.get("order_id"),
        )

    def sell(self, token: str, shares: float, limit_price: float) -> FillResult:
        if self._client is None:
            return FillResult(False, reason="not_connected")
        from live_executor import ExecutionStopped, OrderNotSubmitted, encode_response, sell_position

        from .fees import leg_fee as fee_fn

        trigger = max(0.01, float(limit_price) if limit_price and limit_price > 0 else 0.01)
        try:
            response = self._run(
                sell_position(self._client, self.env_path, token, shares, trigger)
            )
        except OrderNotSubmitted as exc:
            return FillResult(False, reason=str(exc) or "order_not_submitted")
        except ExecutionStopped as exc:
            return FillResult(False, reason=str(exc) or "execution_stopped")
        except Exception as exc:
            log.exception("live sell failed")
            return FillResult(False, reason=f"live_sell_error:{type(exc).__name__}")

        if not response.ok:
            return FillResult(False, reason=str(getattr(response, "code", "sell_miss")),
                              order_id=getattr(response, "order_id", None))
        from live_executor import UNITS

        encoded = encode_response(response)
        # SELL: maker_amount ≈ shares (raw units), taker_amount ≈ proceeds USDC (raw).
        raw_shares = Decimal(str(getattr(response, "making_amount", 0) or 0))
        raw_proceeds = Decimal(str(getattr(response, "taking_amount", 0) or 0))
        shares_f = float(raw_shares / UNITS) if raw_shares > 0 else float(shares)
        proceeds = float(raw_proceeds / UNITS) if raw_proceeds > 0 else 0.0
        if proceeds <= 0 and shares_f > 0:
            proceeds = shares_f * trigger
        avg = proceeds / shares_f if shares_f > 0 else trigger
        fee = fee_fn(proceeds, avg, taker=True, cfg=self.cfg)
        return FillResult(
            True,
            shares=shares_f,
            avg_price=avg,
            fee=fee,
            notional=proceeds,
            order_id=encoded.get("order_id"),
        )

    def merge_pair(self, condition_id: str, shares: float) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "reason": "not_connected"}
        from live_executor import UNITS, ExecutionStopped, OrderNotSubmitted, merge_positions
        from decimal import ROUND_DOWN

        try:
            amount_units = int(
                (Decimal(str(shares)) * UNITS).to_integral_value(rounding=ROUND_DOWN)
            )
            result = self._run(
                merge_positions(
                    self._client, self.env_path, condition_id, amount=amount_units or "max"
                )
            )
            return {
                "ok": True,
                "status": getattr(result, "status", str(result)),
                "matched_shares": shares,
            }
        except (OrderNotSubmitted, ExecutionStopped) as exc:
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:
            log.exception("live merge failed")
            return {"ok": False, "reason": f"merge_error:{type(exc).__name__}"}

    def wallet_capital(self) -> float | None:
        if self._client is None:
            return None
        from live_executor import collateral_available

        try:
            return float(self._run(collateral_available(self._client)))
        except Exception:
            return None


class IntentExecutor:
    def __init__(self, engine: ArbEngine, client: BookClient):
        self.engine = engine
        self.client = client

    def execute(self, intent: Intent) -> dict[str, Any]:
        pair = intent.pair
        meta = intent.meta or {}
        fill_at_limit = bool(meta.get("paper_fill_at_limit"))
        live = not bool(getattr(self.engine.cfg, "paper_mode", True))

        if intent.kind == "enter_first" and pair is not None:
            fill = self.client.buy(
                intent.token, intent.shares, intent.limit_price, fill_at_limit=fill_at_limit
            )
            if not fill.ok or fill.shares <= 0:
                pair.status = "closed"
                pair.closed_at_ms = int(time.time() * 1000)
                pair.escalation_reason = fill.reason or "first_leg_miss"
                self.engine.store.log_event(pair, "first_miss", reason=pair.escalation_reason)
                return {"ok": False, "kind": intent.kind, "reason": fill.reason}
            self.engine.on_first_fill(pair, fill.shares, fill.avg_price, fill.fee, fill.order_id)
            # Keep planned second limit when paper fill-at-limit (maker path).
            if fill_at_limit and meta.get("second_limit") is not None:
                pair.max_second_price = float(meta["second_limit"])
                self.engine.store.upsert(pair)
            result = {"ok": True, "kind": intent.kind, "fill": fill.__dict__, "pair_id": pair.pair_id}

            if getattr(self.engine.cfg, "immediate_hedge", True) and pair.status == "awaiting_hedge":
                need = pair.unpaired_shares()
                max_px = pair.max_second_price
                if need > 0 and max_px is not None and max_px > 0:
                    hedge = self.client.buy(
                        pair.second.token, need, max_px, fill_at_limit=fill_at_limit
                    )
                    result["immediate_hedge"] = hedge.__dict__
                    if hedge.ok and hedge.shares > 0:
                        self.engine.on_second_fill(
                            pair, hedge.shares, hedge.avg_price, hedge.fee, hedge.order_id
                        )
                        result["hedged"] = True
                        if live and pair.status == "paired" and hasattr(self.client, "merge_pair"):
                            merge = self.client.merge_pair(
                                pair.condition_id, min(pair.first.shares, pair.second.shares)
                            )
                            result["merge"] = merge
                            if merge.get("ok"):
                                payout = float(merge.get("matched_shares") or 0)
                                self.engine.on_pair_exited(pair, payout, 0.0)
                                result["merged"] = True
                    else:
                        self.engine.store.log_event(
                            pair, "immediate_hedge_miss", reason=hedge.reason
                        )
                        # Unwind unpaired inventory (paper and live) — do not stack directional risk.
                        dump = self.client.sell(pair.first.token, pair.first.shares, 0.01)
                        if dump.ok:
                            self.engine.on_unpaired_closed(pair, dump.notional, dump.fee)
                            result["unpaired_unwound"] = True
            return result

        if intent.kind == "hedge_second" and pair is not None:
            fill = self.client.buy(
                intent.token, intent.shares, intent.limit_price, fill_at_limit=fill_at_limit
            )
            if not fill.ok or fill.shares <= 0:
                self.engine.store.log_event(pair, "hedge_miss", reason=fill.reason)
                return {"ok": False, "kind": intent.kind, "reason": fill.reason, "pair_id": pair.pair_id}
            self.engine.on_second_fill(pair, fill.shares, fill.avg_price, fill.fee, fill.order_id)
            return {"ok": True, "kind": intent.kind, "fill": fill.__dict__, "pair_id": pair.pair_id}

        if intent.kind == "close_unpaired" and pair is not None:
            fill = self.client.sell(intent.token, intent.shares, intent.limit_price)
            if not fill.ok:
                self.engine.store.log_event(pair, "close_miss", reason=fill.reason)
                return {"ok": False, "kind": intent.kind, "reason": fill.reason}
            self.engine.on_unpaired_closed(pair, fill.notional, fill.fee)
            return {"ok": True, "kind": intent.kind, "fill": fill.__dict__, "pair_id": pair.pair_id}

        if intent.kind == "exit_pair" and pair is not None:
            meta = intent.meta or {}
            f1 = self.client.sell(pair.first.token, intent.shares, float(meta.get("bid1") or 0.01))
            f2 = self.client.sell(pair.second.token, intent.shares, float(meta.get("bid2") or 0.01))
            proceeds = (f1.notional if f1.ok else 0) + (f2.notional if f2.ok else 0)
            fees = (f1.fee if f1.ok else 0) + (f2.fee if f2.ok else 0)
            if not f1.ok and not f2.ok:
                return {"ok": False, "kind": intent.kind, "reason": "exit_miss"}
            self.engine.on_pair_exited(pair, proceeds, fees)
            return {"ok": True, "kind": intent.kind, "proceeds": proceeds, "pair_id": pair.pair_id}

        return {"ok": False, "kind": intent.kind, "reason": "unhandled"}
