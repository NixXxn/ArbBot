"""Detect Polymarket ↔ Kalshi complementary-leg edges."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from arbitrage.config import ArbConfig
from arbitrage.discovery import discover_markets
from arbitrage.quote import fetch_book

from .config import CrossConfig
from .kalshi_client import KalshiClient
from .match import MatchedPair, match_markets

log = logging.getLogger("arbot.cross.strategy")


@dataclass
class CrossOpportunity:
    pair_id: str
    poly_slug: str
    kalshi_ticker: str
    direction: str  # poly_yes_kalshi_no | kalshi_yes_poly_no
    poly_leg: str  # yes|no
    kalshi_leg: str
    poly_token: str
    poly_ask: float
    kalshi_ask: float
    cost: float
    edge: float
    shares: float
    match_score: float
    match_source: str
    question: str


@dataclass
class CrossIntent:
    kind: str  # enter
    opportunity: CrossOpportunity
    reason: str = ""


class CrossEngine:
    def __init__(
        self,
        cfg: CrossConfig,
        *,
        session: requests.Session | None = None,
        kalshi: KalshiClient | None = None,
        poly_cfg: ArbConfig | None = None,
    ):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.kalshi = kalshi or KalshiClient(cfg.kalshi_base_url, session=self.session)
        self.poly_cfg = poly_cfg or ArbConfig(
            scan_limit=cfg.scan_limit_poly,
            min_liquidity=100,
            min_volume_24h=50,
            min_ask=cfg.min_ask,
            max_ask=cfg.max_ask,
            min_ask_size=cfg.min_size,
            max_maker_ask_sum=2.0,
            paper_mode=True,
        )
        self.decisions: list[dict[str, Any]] = []
        self.last_matches: list[MatchedPair] = []
        # Quoted complementary legs from the latest scan (includes near-misses).
        self.last_candidates: list[dict[str, Any]] = []

    def _log(self, decision: str, reason: str, **extra: Any) -> None:
        row = {"at_ms": int(time.time() * 1000), "decision": decision, "reason": reason, **extra}
        self.decisions.append(row)
        self.decisions = self.decisions[-200:]
        log.info("cross %s: %s %s", decision, reason, {k: v for k, v in extra.items() if k != "reason"})

    def candidate_summary(self) -> dict[str, Any]:
        rows = list(self.last_candidates)
        edges = [float(r["edge"]) for r in rows if r.get("edge") is not None]
        return {
            "candidate_count": len(rows),
            "eligible_count": sum(1 for r in rows if r.get("eligible")),
            "best_spread_pct": (max(edges) * 100.0) if edges else None,
            "best_edge": max(edges) if edges else None,
        }

    def scan(self, *, cash: float, open_ids: set[str] | None = None) -> list[CrossIntent]:
        open_ids = open_ids or set()
        self.last_candidates = []
        poly_markets = discover_markets(self.poly_cfg, session=self.session)
        kalshi_markets = self.kalshi.list_markets(limit=self.cfg.scan_limit_kalshi)
        matches = match_markets(poly_markets, kalshi_markets, self.cfg)
        self.last_matches = matches
        if not matches:
            self._log("skip", "no_matches", poly=len(poly_markets), kalshi=len(kalshi_markets))
            return []

        intents: list[CrossIntent] = []
        rejects: dict[str, int] = {}
        remaining = max(0, self.cfg.max_open_pairs - len(open_ids))
        for matched in matches:
            pair_key = f"{matched.poly.slug}::{matched.kalshi.ticker}"
            if pair_key in open_ids:
                rejects["already_open"] = rejects.get("already_open", 0) + 1
                continue
            try:
                opp = self._quote_opportunity(matched, cash)
            except Exception as exc:
                rejects["quote_error"] = rejects.get("quote_error", 0) + 1
                log.debug("quote failed %s: %s", pair_key, exc)
                continue
            if opp is None:
                rejects["no_edge"] = rejects.get("no_edge", 0) + 1
                continue
            if opp.edge + 1e-9 < self.cfg.min_edge:
                rejects["edge_too_small"] = rejects.get("edge_too_small", 0) + 1
                continue
            if opp.cost > self.cfg.max_pair_cost + 1e-9:
                rejects["cost_too_high"] = rejects.get("cost_too_high", 0) + 1
                continue
            if len(intents) >= remaining:
                continue
            intents.append(CrossIntent(kind="enter", opportunity=opp, reason=opp.direction))
            self._log(
                "enter",
                opp.direction,
                slug=opp.poly_slug,
                ticker=opp.kalshi_ticker,
                cost=round(opp.cost, 4),
                edge=round(opp.edge, 4),
                spread_pct=round(opp.edge * 100.0, 2),
                score=round(opp.match_score, 3),
                source=opp.match_source,
            )
        if not intents:
            self._log(
                "skip",
                "no_cross_edge",
                rejects=rejects,
                matched=len(matches),
                candidates=len(self.last_candidates),
                poly=len(poly_markets),
                kalshi=len(kalshi_markets),
            )
        return intents

    def _quote_opportunity(self, matched: MatchedPair, cash: float) -> CrossOpportunity | None:
        cfg = self.cfg
        poly = matched.poly
        book_yes = fetch_book(poly.token_a, session=self.session)
        book_no = fetch_book(poly.token_b, session=self.session)
        kbook = self.kalshi.orderbook(matched.kalshi.ticker, depth=15)

        poly_yes_ask, poly_yes_size = book_yes.ask, book_yes.ask_size
        poly_no_ask, poly_no_size = book_no.ask, book_no.ask_size
        kalshi_yes_ask, kalshi_yes_size = kbook.yes_ask, kbook.yes_ask_size
        kalshi_no_ask, kalshi_no_size = kbook.no_ask, kbook.no_ask_size

        if matched.aligned:
            # Same-event YES/NO complement across venues.
            directions = [
                ("poly_yes_kalshi_no", "yes", "no", poly.token_a, poly_yes_ask, poly_yes_size, kalshi_no_ask, kalshi_no_size),
                ("kalshi_yes_poly_no", "no", "yes", poly.token_b, poly_no_ask, poly_no_size, kalshi_yes_ask, kalshi_yes_size),
            ]
        else:
            # Inverted mapping: Poly YES == Kalshi NO.
            directions = [
                ("poly_yes_kalshi_yes", "yes", "yes", poly.token_a, poly_yes_ask, poly_yes_size, kalshi_yes_ask, kalshi_yes_size),
                ("poly_no_kalshi_no", "no", "no", poly.token_b, poly_no_ask, poly_no_size, kalshi_no_ask, kalshi_no_size),
            ]

        best: CrossOpportunity | None = None
        for direction, poly_leg, kalshi_leg, token, p_ask, p_size, k_ask, k_size in directions:
            row: dict[str, Any] = {
                "slug": poly.slug,
                "kalshi_ticker": matched.kalshi.ticker,
                "direction": direction,
                "poly_leg": poly_leg,
                "kalshi_leg": kalshi_leg,
                "match_score": matched.score,
                "source": matched.source,
                "question": poly.question,
                "eligible": False,
                "reject": None,
            }
            if p_ask is None or k_ask is None:
                row["reject"] = "missing_ask"
                self.last_candidates.append(row)
                continue
            row["poly_ask"] = float(p_ask)
            row["kalshi_ask"] = float(k_ask)
            cost = float(p_ask) + float(k_ask) + float(cfg.fee_buffer)
            edge = 1.0 - cost
            row["cost"] = round(cost, 4)
            row["edge"] = round(edge, 4)
            row["spread_pct"] = round(edge * 100.0, 2)
            if not (cfg.min_ask <= p_ask <= cfg.max_ask and cfg.min_ask <= k_ask <= cfg.max_ask):
                row["reject"] = "ask_out_of_band"
                self.last_candidates.append(row)
                continue
            if (p_size or 0) < cfg.min_size or (k_size or 0) < cfg.min_size:
                row["reject"] = "size_too_small"
                self.last_candidates.append(row)
                continue
            shares = min(
                cash / max(cost, 1e-9),
                cfg.position_usd / max(cost, 1e-9),
                float(p_size or 0),
                float(k_size or 0),
            )
            row["shares"] = round(shares, 4)
            if shares * float(p_ask) < 2.0:
                row["reject"] = "notional_too_small"
                self.last_candidates.append(row)
                continue
            if edge + 1e-9 < cfg.min_edge:
                row["reject"] = "edge_too_small"
            elif cost > cfg.max_pair_cost + 1e-9:
                row["reject"] = "cost_too_high"
            else:
                row["eligible"] = True
            self.last_candidates.append(row)
            if not row["eligible"]:
                continue
            opp = CrossOpportunity(
                pair_id=f"{poly.slug}::{matched.kalshi.ticker}::{direction}",
                poly_slug=poly.slug,
                kalshi_ticker=matched.kalshi.ticker,
                direction=direction,
                poly_leg=poly_leg,
                kalshi_leg=kalshi_leg,
                poly_token=token,
                poly_ask=float(p_ask),
                kalshi_ask=float(k_ask),
                cost=cost,
                edge=edge,
                shares=shares,
                match_score=matched.score,
                match_source=matched.source,
                question=poly.question,
            )
            if best is None or opp.edge > best.edge:
                best = opp
        return best
