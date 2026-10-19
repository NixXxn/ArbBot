"""Match Polymarket markets to Kalshi tickers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from arbitrage.discovery import ArbMarket

from .config import CrossConfig, MappedPair
from .kalshi_client import KalshiMarket

_STOP = {
    "the", "a", "an", "will", "be", "to", "of", "in", "on", "for", "and", "or",
    "by", "at", "is", "are", "before", "after", "than", "vs", "versus",
}


def normalize_title(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in _STOP]
    return " ".join(tokens)


def title_score(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Token Jaccard boost for shared rare terms
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        ratio = 0.65 * ratio + 0.35 * jaccard
    return float(ratio)


@dataclass(frozen=True)
class MatchedPair:
    poly: ArbMarket
    kalshi: KalshiMarket
    score: float
    aligned: bool
    source: str  # mapped | auto


def match_markets(
    poly_markets: list[ArbMarket],
    kalshi_markets: list[KalshiMarket],
    cfg: CrossConfig,
) -> list[MatchedPair]:
    by_slug = {m.slug: m for m in poly_markets}
    by_ticker = {m.ticker: m for m in kalshi_markets}
    out: list[MatchedPair] = []
    used_poly: set[str] = set()
    used_kalshi: set[str] = set()

    for mapped in cfg.mapped_pairs:
        poly = by_slug.get(mapped.poly_slug)
        kalshi = by_ticker.get(mapped.kalshi_ticker)
        if poly is None or kalshi is None:
            continue
        out.append(
            MatchedPair(
                poly=poly, kalshi=kalshi, score=1.0,
                aligned=bool(mapped.aligned), source="mapped",
            )
        )
        used_poly.add(poly.slug)
        used_kalshi.add(kalshi.ticker)

    if not cfg.auto_match:
        return out

    max_delta_ms = float(cfg.max_end_delta_hours) * 3600.0 * 1000.0
    for poly in poly_markets:
        if poly.slug in used_poly:
            continue
        best: tuple[float, KalshiMarket] | None = None
        for kalshi in kalshi_markets:
            if kalshi.ticker in used_kalshi:
                continue
            if poly.end_ms and kalshi.end_ms:
                if abs(poly.end_ms - kalshi.end_ms) > max_delta_ms:
                    continue
            score = title_score(poly.question, kalshi.title)
            if score < cfg.min_match_score:
                continue
            if best is None or score > best[0]:
                best = (score, kalshi)
        if best is None:
            continue
        score, kalshi = best
        out.append(
            MatchedPair(
                poly=poly, kalshi=kalshi, score=score,
                aligned=True, source="auto",
            )
        )
        used_poly.add(poly.slug)
        used_kalshi.add(kalshi.ticker)
    out.sort(key=lambda row: row.score, reverse=True)
    return out
