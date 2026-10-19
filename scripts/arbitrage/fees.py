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
    if getattr(cfg, "paper_mode", True) and getattr(cfg, "paper_relax_fees", True):
        # Match papertrader paper path: no separate fee drag on the entry gate.
        return 0.0
    rate = cfg.taker_fee_rate if taker else cfg.maker_fee_rate
    base = max(0.0, notional) * max(0.0, rate)
    buffer = max(0.0, notional) * max(0.0, cfg.fee_buffer) * 0.5
    return base + buffer


def paper_taker_cap(cfg: ArbConfig) -> float:
    """Papertrader: paper takes gross ask-sum up to ~0.995."""
    return max(float(cfg.max_pair_cost), float(getattr(cfg, "paper_taker_cap", 0.995) or 0.995))


def entry_ok(price1: float, price2: float, shares: float, *, cfg: ArbConfig) -> FeeBreakdown:
    """Entry gate aligned with papertrader paper vs live fee handling."""
    shares = max(0.0, float(shares))
    pair = float(price1) + float(price2)
    n1 = shares * float(price1)
    n2 = shares * float(price2)
    paper = bool(getattr(cfg, "paper_mode", True))
    if paper:
        # papertrader: fee_buffer=0 on gate, taker_cap≈0.995, min_edge * 0.5
        cap = paper_taker_cap(cfg)
        need_edge = cfg.min_edge * 0.5
        ok = pair <= cap + 1e-9 and (1.0 - pair) + 1e-9 >= need_edge
        reason = "" if ok else ("ask_sum_too_high" if pair > cap + 1e-9 else "edge_too_small")
        net = shares * (1.0 - pair)
        return FeeBreakdown(
            leg1_notional=n1, leg2_notional=n2, leg1_fee=0.0, leg2_fee=0.0,
            slippage=0.0, total_cost=n1 + n2, guaranteed_payout=shares,
            net=net, ok=ok, reason=reason,
        )
    return pair_net(price1, price2, shares, cfg=cfg)


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
    slip = 0.0 if getattr(cfg, "paper_mode", True) else (n1 + n2) * max(0.0, cfg.slippage_buffer)
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
    """Max affordable second-leg price from actual first fill."""
    fill1 = float(fill1_avg)
    if fill1 <= 0 or fill1 >= 1:
        return 0.0
    if getattr(cfg, "paper_mode", True):
        # Papertrader-style: complete if sum stays under paper taker cap.
        cap = paper_taker_cap(cfg)
        return max(0.0, round(cap - fill1, 4))
    fee1 = leg_fee(fill1, fill1, taker=True, cfg=cfg)
    required = cfg.min_edge + cfg.safety_margin
    rate2 = cfg.taker_fee_rate if taker2 else cfg.maker_fee_rate
    denom = 1.0 + rate2 + cfg.slippage_buffer + 0.5 * cfg.fee_buffer
    if denom <= 0:
        return 0.0
    residual = 1.0 - fill1 - fee1 - required
    max_p = residual / denom
    max_p = min(max_p, cfg.max_pair_cost - fill1)
    return max(0.0, round(max_p, 4))
