"""Choose which leg to buy first using momentum, spread, depth, and fill odds."""
from __future__ import annotations

from dataclasses import dataclass

from .quote import PairQuote


@dataclass(frozen=True)
class LegChoice:
    first_outcome: str
    second_outcome: str
    first_token: str
    second_token: str
    first_ask: float
    second_ask: float
    first_size: float
    second_size: float
    score_first: float
    score_second: float
    reason: str


def _urgency(
    book_ask: float, book_bid: float | None, ask_size: float, other_ask: float
) -> tuple[float, float]:
    """Return (urgency_to_buy_first, counterleg_fill_probability).

    Urgency blends spread tightness, ask depth, relative cheapness, and mid proximity.
    Volatility alone is not used.
    """
    spread = 0.05
    if book_bid is not None and book_ask is not None:
        spread = max(1e-4, book_ask - book_bid)
    depth_score = 1.0 / max(ask_size, 1.0)
    tight = 1.0 / (1.0 + spread * 20.0)
    cheap = max(0.0, other_ask - book_ask)
    mid = book_ask if book_bid is None else (book_ask + book_bid) / 2.0
    near_mid = 1.0 - min(1.0, abs(book_ask - mid) / max(spread, 1e-4))
    fill_prob = min(1.0, ask_size / 20.0)
    urgency = 0.35 * depth_score + 0.25 * tight + 0.25 * cheap + 0.15 * near_mid
    return urgency, fill_prob


def choose_leg_order(quote: PairQuote) -> LegChoice | None:
    m = quote.market
    a, b = quote.book_a, quote.book_b
    if a.ask is None or b.ask is None:
        return None
    urg_a, fill_a = _urgency(a.ask, a.bid, a.ask_size, b.ask)
    urg_b, fill_b = _urgency(b.ask, b.bid, b.ask_size, a.ask)

    # Both legs must look fillable; otherwise refuse rather than open unpaired risk.
    if fill_a < 0.08 or fill_b < 0.08 or min(a.ask_size, b.ask_size) < 1.0:
        return None

    # First leg = higher urgency (price more likely to rise). Require the *other*
    # leg to still have acceptable fill probability so pair completion is plausible.
    if urg_a >= urg_b:
        if fill_b < 0.15 and fill_a >= 0.15:
            # Counterleg too thin — flip if this side is deeper
            first = ("b", urg_b, fill_b)
            second = ("a", urg_a, fill_a)
            reason = "flip_for_counterleg_depth"
        else:
            first = ("a", urg_a, fill_a)
            second = ("b", urg_b, fill_b)
            reason = "urgency_a"
    else:
        if fill_a < 0.15 and fill_b >= 0.15:
            first = ("a", urg_a, fill_a)
            second = ("b", urg_b, fill_b)
            reason = "flip_for_counterleg_depth"
        else:
            first = ("b", urg_b, fill_b)
            second = ("a", urg_a, fill_a)
            reason = "urgency_b"

    # Hard gate: both legs must remain executable; refuse if counterleg fill odds tiny.
    if second[2] < 0.08:
        return None

    if first[0] == "a":
        return LegChoice(
            first_outcome=m.outcome_a,
            second_outcome=m.outcome_b,
            first_token=m.token_a,
            second_token=m.token_b,
            first_ask=a.ask,
            second_ask=b.ask,
            first_size=a.ask_size,
            second_size=b.ask_size,
            score_first=first[1],
            score_second=second[1],
            reason=reason,
        )
    return LegChoice(
        first_outcome=m.outcome_b,
        second_outcome=m.outcome_a,
        first_token=m.token_b,
        second_token=m.token_a,
        first_ask=b.ask,
        second_ask=a.ask,
        first_size=b.ask_size,
        second_size=a.ask_size,
        score_first=first[1],
        score_second=second[1],
        reason=reason,
    )
