"""Pure portfolio controls used by paper and funded all-strategy modes."""
from decimal import Decimal, ROUND_DOWN


D = Decimal
STRATEGY_EXPOSURE_RATE = D("0.15")
LOSS_STREAK_LIMIT = 3
LOSS_COOLDOWN_MS = 15 * 60 * 1000


def loss_cooldown(records, strategy, now_ms):
    """Return remaining cooldown after three consecutive realized losses."""
    settled = []
    for row in records:
        if row.get("strategy") != strategy or row.get("net_pnl") is None:
            continue
        try:
            timestamp = int(row.get("settled_at_ms") or 0)
            pnl = D(str(row["net_pnl"]))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if timestamp > 0 and pnl.is_finite():
            settled.append((timestamp, pnl))
    settled.sort(key=lambda item: item[0])
    recent = settled[-LOSS_STREAK_LIMIT:]
    if len(recent) < LOSS_STREAK_LIMIT or any(pnl >= 0 for _, pnl in recent):
        return 0
    return max(0, LOSS_COOLDOWN_MS - (int(now_ms) - recent[-1][0]))


def recovery_budget(records, strategy, base=D("5"), step=D("2"), cap=D("10")):
    """One bounded step-up after a realized loss; reset after a win."""
    base = D(str(base))
    step = D(str(step))
    cap = D(str(cap))
    if not base.is_finite() or base <= 0 or not step.is_finite() or step < 1 or not cap.is_finite():
        return base if base.is_finite() and base > 0 else D("5")
    settled = []
    for row in records:
        if row.get("strategy") != strategy or row.get("net_pnl") is None:
            continue
        try:
            timestamp = int(row.get("settled_at_ms") or 0)
            pnl = D(str(row["net_pnl"]))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if timestamp > 0 and pnl.is_finite():
            settled.append((timestamp, pnl))
    if not settled:
        return min(base, cap)
    settled.sort(key=lambda item: item[0])
    if settled[-1][1] > 0:
        return min(base, cap)
    return min(base * step, cap)


def balanced_reservation(capital, current_strategy_exposure, requested, minimum):
    """Cap one strategy at 15%, while still allowing one executable minimum order."""
    capital = D(str(capital))
    current = D(str(current_strategy_exposure))
    requested = D(str(requested))
    minimum = D(str(minimum))
    if any(not value.is_finite() for value in (capital, current, requested, minimum)):
        return D(0)
    if capital <= 0 or current < 0 or requested <= 0 or minimum <= 0:
        return D(0)
    limit = (capital * STRATEGY_EXPOSURE_RATE).quantize(D("0.01"), rounding=ROUND_DOWN)
    if current == 0:
        limit = max(limit, minimum)
    allowed = max(D(0), limit-current)
    result = min(requested, allowed).quantize(D("0.01"), rounding=ROUND_DOWN)
    return result if result >= minimum else D(0)
