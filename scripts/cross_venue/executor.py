"""Execute cross-venue opportunities on Polymarket + Kalshi (paper or live)."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from arbitrage.config import ArbConfig
from arbitrage.executor_bridge import FillResult, LiveBookClient, PaperBookClient

from .config import CrossConfig
from .kalshi_client import KalshiClient, KalshiOrderError
from .strategy import CrossIntent, CrossOpportunity

log = logging.getLogger("arbot.cross.executor")


@dataclass
class CrossFillResult:
    ok: bool
    opportunity: CrossOpportunity
    poly_fill: dict[str, Any] | None = None
    kalshi_fill: dict[str, Any] | None = None
    shares: float = 0.0
    cost: float = 0.0
    net_pnl: float = 0.0
    reason: str = ""
    unwound: bool = False
    live: bool = False


class CrossExecutor:
    def __init__(
        self,
        cfg: CrossConfig,
        kalshi: KalshiClient,
        *,
        poly_client: PaperBookClient | LiveBookClient | None = None,
        poly_cfg: ArbConfig | None = None,
        env_path: str = ".env",
    ):
        self.cfg = cfg
        self.kalshi = kalshi
        self.env_path = env_path
        self.poly_cfg = poly_cfg or ArbConfig(paper_mode=cfg.paper_mode)
        self.poly = poly_client
        if self.poly is None:
            if cfg.paper_mode:
                self.poly = PaperBookClient(self.poly_cfg)
            else:
                self.poly = LiveBookClient(self.poly_cfg, env_path)
                self.poly.connect(redemption=False)

    def execute(self, intent: CrossIntent) -> CrossFillResult:
        opp = intent.opportunity
        if self.cfg.paper_mode:
            return self._paper_fill(opp)
        return self._live_fill(opp)

    def _paper_fill(self, opp: CrossOpportunity) -> CrossFillResult:
        shares = float(opp.shares)
        cost = shares * (opp.poly_ask + opp.kalshi_ask)
        fee = shares * float(self.cfg.fee_buffer)
        net = shares - cost - fee
        return CrossFillResult(
            ok=True,
            opportunity=opp,
            poly_fill={"shares": shares, "avg_price": opp.poly_ask, "paper": True},
            kalshi_fill={"shares": shares, "avg_price": opp.kalshi_ask, "paper": True},
            shares=shares,
            cost=cost + fee,
            net_pnl=net,
            live=False,
        )

    def _live_fill(self, opp: CrossOpportunity) -> CrossFillResult:
        # Whole contracts on Kalshi; size to the integer floor.
        count = int(math.floor(opp.shares))
        if count < 1:
            return CrossFillResult(ok=False, opportunity=opp, reason="size_below_one_contract", live=True)

        # Sequence: fill first venue, then hedge second; unwind on hedge miss.
        buy_poly_first = opp.direction.startswith("poly_")
        try:
            if buy_poly_first:
                poly_fill = self._buy_poly(opp.poly_token, count, opp.poly_ask)
                if not poly_fill.ok or poly_fill.shares <= 0:
                    return CrossFillResult(
                        ok=False, opportunity=opp, reason=poly_fill.reason or "poly_miss",
                        poly_fill=poly_fill.__dict__, live=True,
                    )
                filled = max(1, int(math.floor(poly_fill.shares)))
                kalshi_fill = self._buy_kalshi(opp.kalshi_ticker, opp.kalshi_leg, filled, opp.kalshi_ask)
                if not kalshi_fill.get("ok"):
                    dump = self.poly.sell(opp.poly_token, poly_fill.shares, 0.01)
                    return CrossFillResult(
                        ok=False, opportunity=opp, reason=kalshi_fill.get("reason") or "kalshi_miss",
                        poly_fill=poly_fill.__dict__, kalshi_fill=kalshi_fill,
                        unwound=bool(dump.ok), live=True,
                    )
            else:
                kalshi_fill = self._buy_kalshi(opp.kalshi_ticker, opp.kalshi_leg, count, opp.kalshi_ask)
                if not kalshi_fill.get("ok"):
                    return CrossFillResult(
                        ok=False, opportunity=opp,
                        reason=kalshi_fill.get("reason") or "kalshi_miss",
                        kalshi_fill=kalshi_fill, live=True,
                    )
                filled = int(kalshi_fill["filled"])
                poly_fill = self._buy_poly(opp.poly_token, filled, opp.poly_ask)
                if not poly_fill.ok or poly_fill.shares <= 0:
                    try:
                        self.kalshi.sell_ioc(
                            opp.kalshi_ticker, opp.kalshi_leg, filled,
                            max(0.01, min(0.99, float(kalshi_fill.get("avg_price") or opp.kalshi_ask) - 0.05)),
                        )
                        unwound = True
                    except KalshiOrderError:
                        unwound = False
                    return CrossFillResult(
                        ok=False, opportunity=opp, reason=poly_fill.reason or "poly_miss",
                        poly_fill=poly_fill.__dict__, kalshi_fill=kalshi_fill,
                        unwound=unwound, live=True,
                    )

            matched = min(float(poly_fill.shares), float(kalshi_fill.get("filled") or 0))
            if matched <= 0:
                return CrossFillResult(
                    ok=False, opportunity=opp, reason="zero_matched",
                    poly_fill=poly_fill.__dict__, kalshi_fill=kalshi_fill, live=True,
                )
            cost = matched * (
                float(poly_fill.avg_price) + float(kalshi_fill.get("avg_price") or opp.kalshi_ask)
            )
            fee = matched * float(self.cfg.fee_buffer)
            net = matched - cost - fee
            return CrossFillResult(
                ok=True,
                opportunity=opp,
                poly_fill=poly_fill.__dict__,
                kalshi_fill=kalshi_fill,
                shares=matched,
                cost=cost + fee,
                net_pnl=net,
                live=True,
            )
        except Exception as exc:
            log.exception("cross live fill failed")
            return CrossFillResult(ok=False, opportunity=opp, reason=f"live_error:{type(exc).__name__}", live=True)

    def _buy_poly(self, token: str, shares: float, limit: float) -> FillResult:
        return self.poly.buy(token, float(shares), float(limit), fill_at_limit=False, resting=False)

    def _buy_kalshi(self, ticker: str, side: str, count: int, limit: float) -> dict[str, Any]:
        try:
            result = self.kalshi.buy_ioc(ticker, side, count, limit)
            if not result.get("ok"):
                result["reason"] = "kalshi_ioc_unfilled"
            return result
        except KalshiOrderError as exc:
            return {"ok": False, "reason": str(exc), "filled": 0}
