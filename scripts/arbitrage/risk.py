"""Configurable risk limits and escalation decisions."""
from __future__ import annotations

from dataclasses import dataclass

from .config import ArbConfig
from .pair_state import PairRecord, PairStore


@dataclass(frozen=True)
class RiskSnapshot:
    open_pairs: int
    unpaired_notional: float
    capital_locked: float
    entries_allowed: bool
    block_reason: str | None = None


def snapshot(store: PairStore, cfg: ArbConfig) -> RiskSnapshot:
    opens = store.open_pairs()
    unpaired = 0.0
    locked = 0.0
    unpaired_count = 0
    for pair in opens:
        locked += pair.first.notional + pair.second.notional
        if pair.status in ("awaiting_hedge", "escalating", "pending_first"):
            unpaired_count += 1
            unpaired += pair.first.notional + max(0.0, pair.first.shares - pair.second.shares) * (
                pair.max_second_price or 0.0
            )
    reason = None
    allowed = True
    max_unpaired = int(getattr(cfg, "max_concurrent_unpaired", 2) or 2)
    if len(opens) >= cfg.max_open_pairs:
        allowed, reason = False, "max_open_pairs"
    elif unpaired_count >= max_unpaired:
        allowed, reason = False, "max_concurrent_unpaired"
    elif unpaired >= cfg.max_unpaired_notional:
        allowed, reason = False, "max_unpaired_notional"
    elif locked >= cfg.max_capital_locked:
        allowed, reason = False, "max_capital_locked"
    return RiskSnapshot(
        open_pairs=len(opens),
        unpaired_notional=unpaired,
        capital_locked=locked,
        entries_allowed=allowed,
        block_reason=reason,
    )


@dataclass(frozen=True)
class EscalationAction:
    action: str  # update_hedge | cancel_hedge | close_first | none
    reason: str
    max_price: float | None = None


def escalation_for(pair: PairRecord, cfg: ArbConfig, *, second_ask: float | None) -> EscalationAction:
    age = pair.unpaired_age_seconds()
    if age is None:
        return EscalationAction("none", "not_unpaired")
    max_px = pair.max_second_price
    if second_ask is not None and max_px is not None and second_ask <= max_px + 1e-9:
        if age < cfg.hedge_timeout_seconds:
            return EscalationAction("update_hedge", "ask_within_cap", max_price=min(second_ask, max_px))
    if age < cfg.hedge_timeout_seconds:
        if max_px is not None:
            return EscalationAction("update_hedge", "waiting_within_timeout", max_price=max_px)
        return EscalationAction("none", "waiting")
    # Timed out — close unpaired within loss bound rather than claim a guaranteed hedge.
    if pair.first.avg_price > 0 and max_px is not None:
        # Est. loss vs locked pair if we dump at bid roughly max_unpaired_loss away
        return EscalationAction("close_first", "hedge_timeout", max_price=max_px)
    return EscalationAction("close_first", "hedge_timeout_no_cap")
