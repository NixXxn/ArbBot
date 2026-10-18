"""Entry / hedge / exit decisions for fee-aware binary arbitrage."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .config import ArbConfig
from .discovery import ArbMarket, discover_markets
from .fees import max_second_leg_price, pair_net
from .leg_order import LegChoice, choose_leg_order
from .pair_state import LegFill, PairRecord, PairStore
from .quote import PairQuote, fetch_book, quote_pair
from .risk import EscalationAction, RiskSnapshot, escalation_for, snapshot

log = logging.getLogger("arbot.strategy")


@dataclass
class Intent:
    kind: str  # enter_first | hedge_second | close_unpaired | exit_pair | skip
    pair: PairRecord | None = None
    market: ArbMarket | None = None
    choice: LegChoice | None = None
    quote: PairQuote | None = None
    shares: float = 0.0
    limit_price: float = 0.0
    token: str = ""
    outcome: str = ""
    reason: str = ""
    meta: dict[str, Any] | None = None


class ArbEngine:
    def __init__(
        self,
        cfg: ArbConfig,
        store: PairStore,
        *,
        session: requests.Session | None = None,
        discover_fn: Callable[[ArbConfig], list[ArbMarket]] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.session = session or requests.Session()
        self.discover_fn = discover_fn or (lambda c: discover_markets(c, session=self.session))
        self.clock = clock or time.time
        self.decisions: list[dict[str, Any]] = []

    def _log(self, decision: str, reason: str, **extra: Any) -> None:
        row = {"at_ms": int(self.clock() * 1000), "decision": decision, "reason": reason, **extra}
        self.decisions.append(row)
        self.decisions = self.decisions[-200:]
        log.info("arb %s: %s %s", decision, reason, {k: v for k, v in extra.items() if k != "reason"})

    def risk(self) -> RiskSnapshot:
        return snapshot(self.store, self.cfg)

    def scan_entries(self, *, cash: float) -> list[Intent]:
        risk = self.risk()
        if not risk.entries_allowed:
            self._log("skip", risk.block_reason or "risk_block", **risk.__dict__)
            return []

        markets = self.discover_fn(self.cfg)
        claimed = self.store.active_slugs()
        intents: list[Intent] = []
        remaining_slots = max(0, self.cfg.max_open_pairs - risk.open_pairs)
        if remaining_slots <= 0:
            return []

        pair_budget = min(self.cfg.position_usd, self.cfg.max_position_usd, cash)
        if remaining_slots > 1:
            pair_budget = min(pair_budget, cash / remaining_slots)
        if pair_budget < self.cfg.min_position_usd * 2:
            self._log("skip", "insufficient_cash", cash=cash)
            return []

        rejects: dict[str, int] = {}
        for market in markets:
            if len(intents) >= remaining_slots:
                break
            if market.slug in claimed or market.condition_id in claimed:
                rejects["already_in"] = rejects.get("already_in", 0) + 1
                continue
            quote, reject = quote_pair(market, self.cfg, session=self.session)
            if quote is None:
                rejects[reject or "no_quote"] = rejects.get(reject or "no_quote", 0) + 1
                continue
            choice = choose_leg_order(quote)
            if choice is None:
                rejects["unsafe_pair_completion"] = rejects.get("unsafe_pair_completion", 0) + 1
                self._log(
                    "skip",
                    "unsafe_pair_completion",
                    slug=market.slug,
                    pair_ask=quote.pair_ask,
                )
                continue

            # Both legs must be executable at target size with fee-aware net.
            shares = min(
                pair_budget / max(quote.pair_ask, 1e-9),
                quote.executable_shares,
                choice.first_size,
                choice.second_size,
            )
            if shares * choice.first_ask < self.cfg.min_position_usd:
                rejects["size_too_small"] = rejects.get("size_too_small", 0) + 1
                continue
            fees = pair_net(choice.first_ask, choice.second_ask, shares, cfg=self.cfg)
            if not fees.ok:
                rejects[fees.reason or "fee_gate"] = rejects.get(fees.reason or "fee_gate", 0) + 1
                continue

            # Pre-check second-leg affordability after a worst-case first fill at ask.
            max2 = max_second_leg_price(choice.first_ask, cfg=self.cfg)
            if choice.second_ask > max2 + 1e-9:
                rejects["second_leg_unaffordable"] = rejects.get("second_leg_unaffordable", 0) + 1
                continue

            pair = self.store.new_pair(
                condition_id=market.condition_id,
                slug=market.slug,
                first=LegFill(outcome=choice.first_outcome, token=choice.first_token),
                second=LegFill(outcome=choice.second_outcome, token=choice.second_token),
                target_shares=shares,
                expected_net=fees.net,
            )
            pair.max_second_price = max2
            self.store.log_event(
                pair,
                "entry_planned",
                leg_order=choice.reason,
                first=choice.first_outcome,
                second=choice.second_outcome,
                first_ask=choice.first_ask,
                second_ask=choice.second_ask,
                shares=shares,
                expected_net=fees.net,
                max_second=max2,
            )
            self._log(
                "enter",
                f"leg_order={choice.reason} first={choice.first_outcome}",
                slug=market.slug,
                expected_net=round(fees.net, 4),
                pair_ask=quote.pair_ask,
                shares=round(shares, 4),
            )
            intents.append(
                Intent(
                    kind="enter_first",
                    pair=pair,
                    market=market,
                    choice=choice,
                    quote=quote,
                    shares=shares,
                    limit_price=choice.first_ask,
                    token=choice.first_token,
                    outcome=choice.first_outcome,
                    reason=f"first_leg {choice.first_outcome} via {choice.reason}",
                    meta={"expected_net": fees.net, "max_second": max2, "fees": fees.__dict__},
                )
            )
            claimed.add(market.slug)
            claimed.add(market.condition_id)

        if not intents and rejects:
            self._log("skip", "no_arb_window", rejects=rejects, scanned=len(markets))
        return intents

    def scan_hedges_and_exits(self) -> list[Intent]:
        intents: list[Intent] = []
        for pair in list(self.store.open_pairs()):
            if pair.status == "pending_first":
                # Stale pending without fill — close planning slot.
                age = (int(self.clock() * 1000) - pair.opened_at_ms) / 1000.0
                if age > self.cfg.hedge_timeout_seconds:
                    pair.status = "closed"
                    pair.closed_at_ms = int(self.clock() * 1000)
                    pair.escalation_reason = "first_leg_never_filled"
                    self.store.log_event(pair, "closed", reason=pair.escalation_reason)
                continue

            if pair.status in ("awaiting_hedge", "escalating"):
                intents.extend(self._hedge_or_escalate(pair))
                continue

            if pair.status == "paired":
                exit_intent = self._maybe_exit_pair(pair)
                if exit_intent:
                    intents.append(exit_intent)
        return intents

    def _hedge_or_escalate(self, pair: PairRecord) -> list[Intent]:
        need = pair.unpaired_shares()
        if need <= 1e-9:
            pair.status = "paired"
            pair.paired_at_ms = int(self.clock() * 1000)
            matched = min(pair.first.shares, pair.second.shares)
            total_cost = pair.first.notional + pair.second.notional + pair.first.fee + pair.second.fee
            pair.locked_profit = matched * 1.0 - total_cost
            pair.realized_net = None
            self.store.log_event(pair, "paired", locked_profit=pair.locked_profit)
            return []

        try:
            book = fetch_book(pair.second.token, session=self.session)
            second_ask = book.ask
        except Exception:
            second_ask = None

        # Recompute max second price from actual first fill.
        if pair.first.avg_price > 0:
            pair.max_second_price = max_second_leg_price(pair.first.avg_price, cfg=self.cfg)
            self.store.upsert(pair)

        action: EscalationAction = escalation_for(pair, self.cfg, second_ask=second_ask)
        if action.action == "close_first":
            pair.status = "escalating"
            pair.escalation_reason = action.reason
            self.store.log_event(pair, "escalate_close", reason=action.reason, age=pair.unpaired_age_seconds())
            self._log("escalate", action.reason, slug=pair.slug, pair_id=pair.pair_id)
            close_px = 0.01
            try:
                first_book = fetch_book(pair.first.token, session=self.session)
                if first_book.bid is not None:
                    close_px = first_book.bid
            except Exception:
                pass
            return [
                Intent(
                    kind="close_unpaired",
                    pair=pair,
                    shares=need if need > 0 else pair.first.shares,
                    token=pair.first.token,
                    outcome=pair.first.outcome,
                    limit_price=close_px,
                    reason=action.reason,
                )
            ]

        limit = action.max_price
        if limit is None or limit <= 0:
            return []
        if second_ask is not None and second_ask > limit + 1e-9:
            # Not executable at cap yet — wait or escalate on timeout only.
            if pair.unpaired_age_seconds() or 0 >= self.cfg.hedge_timeout_seconds:
                pair.status = "escalating"
                self.store.log_event(pair, "escalate_unaffordable", ask=second_ask, max=limit)
                return [
                    Intent(
                        kind="close_unpaired",
                        pair=pair,
                        shares=pair.first.shares,
                        token=pair.first.token,
                        outcome=pair.first.outcome,
                        limit_price=0.01,
                        reason="second_leg_above_cap_timeout",
                    )
                ]
            self._log("wait_hedge", "ask_above_cap", slug=pair.slug, ask=second_ask, max=limit)
            return []

        use_price = min(limit, second_ask if second_ask is not None else limit)
        # Final fee check with actual fill1.
        check = pair_net(pair.first.avg_price, use_price, need, cfg=self.cfg)
        if not check.ok and (pair.unpaired_age_seconds() or 0) < self.cfg.hedge_timeout_seconds:
            self._log("wait_hedge", check.reason or "net_fail", slug=pair.slug)
            return []

        self.store.log_event(
            pair,
            "hedge_intent",
            limit=use_price,
            shares=need,
            max_second=pair.max_second_price,
        )
        return [
            Intent(
                kind="hedge_second",
                pair=pair,
                shares=need,
                limit_price=use_price,
                token=pair.second.token,
                outcome=pair.second.outcome,
                reason=f"hedge within cap {use_price:.4f}",
                meta={"expected_net": check.net},
            )
        ]

    def _maybe_exit_pair(self, pair: PairRecord) -> Intent | None:
        # Mark-to-market exit when both bids lock profit, else hold (resolution / merge elsewhere).
        try:
            book1 = fetch_book(pair.first.token, session=self.session)
            book2 = fetch_book(pair.second.token, session=self.session)
        except Exception:
            return None
        if book1.bid is None or book2.bid is None:
            return None
        matched = min(pair.first.shares, pair.second.shares)
        if matched <= 0:
            return None
        mtm = matched * (book1.bid + book2.bid)
        cost = pair.first.notional + pair.second.notional + pair.first.fee + pair.second.fee
        bid_sum = book1.bid + book2.bid
        profit_ok = cost > 0 and mtm >= cost * (1.0 + self.cfg.min_pair_profit_pct)
        sum_ok = bid_sum + 1e-9 >= self.cfg.pair_bid_sum_exit
        if not (profit_ok or sum_ok):
            return None
        self.store.log_event(pair, "exit_pair", bid_sum=bid_sum, mtm=mtm, cost=cost)
        return Intent(
            kind="exit_pair",
            pair=pair,
            shares=matched,
            reason=f"pair_exit bid_sum={bid_sum:.3f}",
            meta={"bid1": book1.bid, "bid2": book2.bid, "mtm": mtm, "cost": cost},
        )

    def on_first_fill(self, pair: PairRecord, shares: float, avg_price: float, fee: float, order_id: str | None) -> None:
        pair.first.shares = shares
        pair.first.avg_price = avg_price
        pair.first.fee = fee
        pair.first.notional = shares * avg_price
        pair.first.order_id = order_id
        pair.first_filled_at_ms = int(self.clock() * 1000)
        pair.status = "awaiting_hedge"
        pair.max_second_price = max_second_leg_price(avg_price, cfg=self.cfg)
        self.store.log_event(
            pair,
            "first_filled",
            shares=shares,
            avg_price=avg_price,
            fee=fee,
            max_second=pair.max_second_price,
            order_id=order_id,
        )

    def on_second_fill(self, pair: PairRecord, shares: float, avg_price: float, fee: float, order_id: str | None) -> None:
        # Align to actually filled amounts — no oversize second leg.
        fill = min(shares, pair.first.shares - pair.second.shares)
        if fill <= 0:
            self.store.log_event(pair, "second_fill_ignored", reason="already_matched")
            return
        # Weighted average if partial top-ups
        prev_sh = pair.second.shares
        prev_n = pair.second.notional
        pair.second.shares = prev_sh + fill
        pair.second.avg_price = ((prev_n + fill * avg_price) / pair.second.shares) if pair.second.shares else avg_price
        pair.second.fee += fee
        pair.second.notional = prev_n + fill * avg_price
        pair.second.order_id = order_id
        pair.hedge_order_id = order_id
        matched = min(pair.first.shares, pair.second.shares)
        if matched + 1e-9 >= pair.first.shares:
            pair.status = "paired"
            pair.paired_at_ms = int(self.clock() * 1000)
            total_cost = pair.first.notional + pair.second.notional + pair.first.fee + pair.second.fee
            pair.locked_profit = matched * 1.0 - total_cost
        self.store.log_event(
            pair,
            "second_filled",
            shares=fill,
            avg_price=avg_price,
            fee=fee,
            status=pair.status,
            locked_profit=pair.locked_profit,
        )

    def on_unpaired_closed(self, pair: PairRecord, proceeds: float, fee: float) -> None:
        cost = pair.first.notional + pair.first.fee
        pair.realized_net = proceeds - fee - cost
        pair.locked_profit = None
        pair.status = "closed"
        pair.closed_at_ms = int(self.clock() * 1000)
        self.store.log_event(pair, "unpaired_closed", proceeds=proceeds, realized_net=pair.realized_net)

    def on_pair_exited(self, pair: PairRecord, proceeds: float, fees: float) -> None:
        cost = pair.first.notional + pair.second.notional + pair.first.fee + pair.second.fee
        pair.realized_net = proceeds - fees - cost
        pair.locked_profit = None
        pair.status = "closed"
        pair.closed_at_ms = int(self.clock() * 1000)
        self.store.log_event(pair, "pair_closed", proceeds=proceeds, realized_net=pair.realized_net)
