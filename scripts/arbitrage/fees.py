"""Fee-aware net profit for equal-share YES/NO pairs."""
from __future__ import annotations

from dataclasses import dataclass

from .config import ArbConfig


@dataclass(frozen=True)
class FeeBreakdown:
    leg1_notional: float
    leg2_notional: float
    leg1_fee: float
    leg2_fee: float
    slippage: float
    total_cost: float
    guaranteed_payout: float
    net: float
    ok: bool
    reason: str = ""


def leg_fee(notional: float, price: float, *, taker: bool, cfg: ArbConfig) -> float:
    """Approximate Polymarket-style fee on notional; floor with fee_buffer share."""
    rate = cfg.taker_fee_rate if taker else cfg.maker_fee_rate
    # Quadratic-ish drag near mid prices is common; keep a simple rate * notional model
    # and always add fee_buffer proportionally so paper/live share the same gate.
    base = max(0.0, notional) * max(0.0, rate)
    buffer = max(0.0, notional) * max(0.0, cfg.fee_buffer) * 0.5
    return base + buffer


def pair_net(
    price1: float,
    price2: float,
    shares: float,
    *,
    cfg: ArbConfig,
    taker1: bool = True,
    taker2: bool = True,
) -> FeeBreakdown:
    shares = max(0.0, float(shares))
    n1 = shares * float(price1)
    n2 = shares * float(price2)
    f1 = leg_fee(n1, price1, taker=taker1, cfg=cfg)
    f2 = leg_fee(n2, price2, taker=taker2, cfg=cfg)
    slip = (n1 + n2) * max(0.0, cfg.slippage_buffer)
    total = n1 + n2 + f1 + f2 + slip
    payout = shares * 1.0
    net = payout - total
    required = cfg.min_edge * shares + cfg.safety_margin * shares
    ok = net + 1e-9 >= required and (price1 + price2) <= cfg.max_pair_cost + 1e-9
    reason = ""
    if not ok:
        if (price1 + price2) > cfg.max_pair_cost + 1e-9:
            reason = "pair_cost_above_cap"
        else:
            reason = "net_below_min_edge"
    return FeeBreakdown(
        leg1_notional=n1,
        leg2_notional=n2,
        leg1_fee=f1,
        leg2_fee=f2,
        slippage=slip,
        total_cost=total,
        guaranteed_payout=payout,
        net=net,
        ok=ok,
        reason=reason,
    )


def max_second_leg_price(
    fill1_avg: float,
    *,
    cfg: ArbConfig,
    taker2: bool = True,
) -> float:
    """Max affordable second-leg price so net still clears min_edge + safety after fees.

    For one share: payout 1 = fill1 + fee1 + p2 + fee2 + slip + required_net
    Approximate fees as rate * notional + fee_buffer share.
    """
    fill1 = float(fill1_avg)
    if fill1 <= 0 or fill1 >= 1:
        return 0.0
    # Solve roughly: 1 - fill1 - fee(fill1) - p2*(1+rate2+slip/2) - fee_buffer/2 - min_edge - safety >= 0
    fee1 = leg_fee(fill1, fill1, taker=True, cfg=cfg)
    required = cfg.min_edge + cfg.safety_margin
    rate2 = cfg.taker_fee_rate if taker2 else cfg.maker_fee_rate
    denom = 1.0 + rate2 + cfg.slippage_buffer + 0.5 * cfg.fee_buffer
    if denom <= 0:
        return 0.0
    residual = 1.0 - fill1 - fee1 - required
    max_p = residual / denom
    # Also respect hard pair cap from fill1.
    max_p = min(max_p, cfg.max_pair_cost - fill1)
    return max(0.0, round(max_p, 4))
