"""Order-book quotes for both legs."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import ArbConfig
from .discovery import ArbMarket

log = logging.getLogger("arbot.quote")
CLOB_BOOK = "https://clob.polymarket.com/book"


@dataclass(frozen=True)
class LegBook:
    bid: float | None
    ask: float | None
    bid_size: float
    ask_size: float
    spread: float | None
    mid: float | None
    fetched_ms: int


@dataclass(frozen=True)
class PairQuote:
    market: ArbMarket
    book_a: LegBook
    book_b: LegBook
    pair_ask: float
    pair_bid: float | None
    executable_shares: float
    age_ms: int


def _levels(side: list[Any]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in side or []:
        try:
            if isinstance(row, dict):
                price = float(row.get("price"))
                size = float(row.get("size") or row.get("amount") or 0)
            else:
                price = float(row.price)
                size = float(getattr(row, "size", 0) or 0)
            if price > 0 and size > 0:
                out.append((price, size))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def fetch_book(token: str, *, session: requests.Session | None = None) -> LegBook:
    sess = session or requests.Session()
    now = int(time.time() * 1000)
    response = sess.get(CLOB_BOOK, params={"token_id": token}, timeout=10,
                        headers={"User-Agent": "Arbot/1.0"})
    response.raise_for_status()
    data = response.json()
    bids = _levels(data.get("bids") or [])
    asks = _levels(data.get("asks") or [])
    bid = max((p for p, _ in bids), default=None)
    ask = min((p for p, _ in asks), default=None)
    bid_size = sum(s for p, s in bids if bid is not None and p == bid)
    ask_size = sum(s for p, s in asks if ask is not None and p == ask)
    spread = (ask - bid) if ask is not None and bid is not None else None
    mid = ((ask + bid) / 2.0) if ask is not None and bid is not None else None
    return LegBook(
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        spread=spread, mid=mid, fetched_ms=now,
    )


def quote_pair(
    market: ArbMarket,
    cfg: ArbConfig,
    *,
    session: requests.Session | None = None,
    books: tuple[LegBook, LegBook] | None = None,
) -> tuple[PairQuote | None, str | None]:
    try:
        if books is None:
            book_a = fetch_book(market.token_a, session=session)
            book_b = fetch_book(market.token_b, session=session)
        else:
            book_a, book_b = books
    except Exception as exc:
        log.debug("book fetch %s: %s", market.slug, exc)
        return None, "book_error"

    now = int(time.time() * 1000)
    age = max(now - book_a.fetched_ms, now - book_b.fetched_ms)
    if age > cfg.max_quote_age_ms:
        return None, "stale_quote"

    if book_a.ask is None or book_b.ask is None:
        return None, "no_ask"
    if book_a.ask_size < cfg.min_ask_size or book_b.ask_size < cfg.min_ask_size:
        return None, "ask_size_too_small"
    if book_a.ask < cfg.min_ask or book_b.ask < cfg.min_ask:
        return None, "ask_too_cheap"
    if book_a.ask > cfg.max_ask or book_b.ask > cfg.max_ask:
        return None, "ask_too_expensive"

    pair_ask = book_a.ask + book_b.ask
    # Papertrader quotes up to max_maker_ask_sum; entry gate decides taker vs skip.
    quote_cap = float(getattr(cfg, "max_maker_ask_sum", cfg.max_pair_cost) or cfg.max_pair_cost)
    if pair_ask > quote_cap + 1e-9:
        return None, "ask_sum_too_high"

    pair_bid = None
    if book_a.bid is not None and book_b.bid is not None:
        pair_bid = book_a.bid + book_b.bid

    return (
        PairQuote(
            market=market,
            book_a=book_a,
            book_b=book_b,
            pair_ask=pair_ask,
            pair_bid=pair_bid,
            executable_shares=min(book_a.ask_size, book_b.ask_size),
            age_ms=age,
        ),
        None,
    )
