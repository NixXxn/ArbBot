"""Paper (and optional live) execution bridge for dual-leg arb."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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


class IntentExecutor:
    def __init__(self, engine: ArbEngine, client: BookClient):
        self.engine = engine
        self.client = client

    def execute(self, intent: Intent) -> dict[str, Any]:
        pair = intent.pair
        meta = intent.meta or {}
        fill_at_limit = bool(meta.get("paper_fill_at_limit"))

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
                    else:
                        self.engine.store.log_event(
                            pair, "immediate_hedge_miss", reason=hedge.reason
                        )
                        # Paper: do not leave unpaired inventory stacking — unwind first leg.
                        if getattr(self.engine.cfg, "paper_mode", True):
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
