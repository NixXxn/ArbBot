"""Choose which leg to buy first: the one more likely to rise in price."""
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


def _rise_score(
    book_ask: float,
    book_bid: float | None,
    ask_size: float,
    bid_size: float,
    other_ask: float,
) -> tuple[float, float]:
    """Score how likely this ask is to tick up soon; also return fill probability.

    Higher rise_score ⇒ buy this leg first so the hedge side is likelier to stay
    flat or get cheaper. Volatility alone is ignored.
    """
    spread = 0.05
    if book_bid is not None:
        spread = max(1e-4, book_ask - book_bid)

    # Thin ask + decent bid size ⇒ offers get lifted (price rises).
    ask_thin = 1.0 / max(ask_size, 1.0)
    bid_support = min(1.0, bid_size / max(ask_size, 1.0)) / (1.0 + spread * 10.0)

    # Tight spread at the touch ⇒ contested; asks get eaten quickly.
    tight = 1.0 / (1.0 + spread * 25.0)

    # Cheaper leg in the pair attracts arb / directional flow → more upside risk.
    relative_cheap = max(0.0, other_ask - book_ask)

    # Ask pressed near mid (vs wide ask-above-mid) is more fragile upward.
    mid = book_ask if book_bid is None else (book_ask + book_bid) / 2.0
    pressed = 1.0 - min(1.0, abs(book_ask - mid) / max(spread, 1e-4))

    rise = (
        0.30 * ask_thin
        + 0.25 * relative_cheap
        + 0.20 * tight
        + 0.15 * bid_support
        + 0.10 * pressed
    )
    fill_prob = min(1.0, ask_size / 15.0)
    return rise, fill_prob


def choose_leg_order(quote: PairQuote) -> LegChoice | None:
    """Always prefer the rising-risk leg first; skip if the hedge leg is too thin."""
    m = quote.market
    a, b = quote.book_a, quote.book_b
    if a.ask is None or b.ask is None:
        return None

    rise_a, fill_a = _rise_score(a.ask, a.bid, a.ask_size, a.bid_size, b.ask)
    rise_b, fill_b = _rise_score(b.ask, b.bid, b.ask_size, b.bid_size, a.ask)

    # Both legs must be fillable — never open unpaired risk on a thin counterleg.
    if fill_a < 0.10 or fill_b < 0.10 or min(a.ask_size, b.ask_size) < 2.0:
        return None

    if rise_a >= rise_b:
        first_key, second_key = "a", "b"
        reason = "rise_a"
        score_first, score_second = rise_a, rise_b
        fill_second = fill_b
    else:
        first_key, second_key = "b", "a"
        reason = "rise_b"
        score_first, score_second = rise_b, rise_a
        fill_second = fill_a

    # Do not flip order just to deepen the hedge — that would buy the falling-risk
    # leg first. If the hedge is weak, refuse the whole entry.
    if fill_second < 0.12:
        return None

    if first_key == "a":
        return LegChoice(
            first_outcome=m.outcome_a,
            second_outcome=m.outcome_b,
            first_token=m.token_a,
            second_token=m.token_b,
            first_ask=a.ask,
            second_ask=b.ask,
            first_size=a.ask_size,
            second_size=b.ask_size,
            score_first=score_first,
            score_second=score_second,
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
        score_first=score_first,
        score_second=score_second,
        reason=reason,
    )
