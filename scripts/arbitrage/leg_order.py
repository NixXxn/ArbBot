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
) -> float:
    """Higher ⇒ buy this leg first (more likely to tick up). Volatility alone ignored."""
    spread = 0.05
    if book_bid is not None:
        spread = max(1e-4, book_ask - book_bid)
    ask_thin = 1.0 / max(ask_size, 1.0)
    bid_support = min(1.0, bid_size / max(ask_size, 1.0)) / (1.0 + spread * 10.0)
    tight = 1.0 / (1.0 + spread * 25.0)
    relative_cheap = max(0.0, other_ask - book_ask)
    mid = book_ask if book_bid is None else (book_ask + book_bid) / 2.0
    pressed = 1.0 - min(1.0, abs(book_ask - mid) / max(spread, 1e-4))
    return (
        0.30 * ask_thin
        + 0.25 * relative_cheap
        + 0.20 * tight
        + 0.15 * bid_support
        + 0.10 * pressed
    )


def choose_leg_order(quote: PairQuote) -> LegChoice | None:
    """Always pick an order when both asks exist — never skip solely on fill-prob.

    Depth/size gates belong in strategy sizing; papertrader never refused a quote
    for 'unsafe pair completion'.
    """
    m = quote.market
    a, b = quote.book_a, quote.book_b
    if a.ask is None or b.ask is None:
        return None

    rise_a = _rise_score(a.ask, a.bid, a.ask_size, a.bid_size, b.ask)
    rise_b = _rise_score(b.ask, b.bid, b.ask_size, b.bid_size, a.ask)

    if rise_a >= rise_b:
        return LegChoice(
            first_outcome=m.outcome_a,
            second_outcome=m.outcome_b,
            first_token=m.token_a,
            second_token=m.token_b,
            first_ask=a.ask,
            second_ask=b.ask,
            first_size=a.ask_size,
            second_size=b.ask_size,
            score_first=rise_a,
            score_second=rise_b,
            reason="rise_a",
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
        score_first=rise_b,
        score_second=rise_a,
        reason="rise_b",
    )
