"""Unit tests for Arbot fee-aware arbitrage (paper/sim, no live orders)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from arbitrage.config import ArbConfig, load_arb_config
from arbitrage.discovery import ArbMarket
from arbitrage.executor_bridge import FillResult, IntentExecutor
from arbitrage.fees import max_second_leg_price, pair_net
from arbitrage.leg_order import choose_leg_order
from arbitrage.pair_state import PairStore
from arbitrage.quote import LegBook, PairQuote
from arbitrage.strategy import ArbEngine, Intent


@pytest.fixture
def cfg() -> ArbConfig:
    return ArbConfig(
        max_pair_cost=0.94,
        min_edge=0.025,
        fee_buffer=0.01,
        safety_margin=0.005,
        taker_fee_rate=0.01,
        slippage_buffer=0.002,
        hedge_timeout_seconds=5.0,
        max_open_pairs=4,
        max_unpaired_notional=100.0,
        position_usd=45.0,
        min_ask_size=1.0,
    )


def test_load_config_defaults():
    path = ROOT / "config" / "arbitrage.yaml"
    loaded = load_arb_config(path)
    assert loaded.max_pair_cost == 0.94
    assert loaded.max_maker_ask_sum == 1.04
    assert loaded.paper_taker_cap == 0.995
    assert loaded.paper_mode is True
    assert loaded.live_maker_enabled is False
    assert loaded.min_edge == 0.025
    assert loaded.safety_margin == 0.005
    assert loaded.hedge_timeout_seconds == 120
    assert loaded.immediate_hedge is True
    assert loaded.max_concurrent_unpaired == 4


def test_paper_entry_matches_papertrader_looseness(cfg):
    from arbitrage.fees import entry_ok

    cfg.paper_mode = True
    # Paper takes ask-sum up to ~0.995 with half min_edge.
    ok = entry_ok(0.48, 0.50, 10.0, cfg=cfg)
    assert ok.ok
    assert abs(ok.net - 0.2) < 1e-9
    bad = entry_ok(0.50, 0.50, 10.0, cfg=cfg)
    assert not bad.ok


def test_pair_net_fee_aware(cfg):
    cfg.paper_mode = False
    cfg.paper_relax_fees = False
    ok = pair_net(0.40, 0.50, 10.0, cfg=cfg)
    assert ok.guaranteed_payout == 10.0
    assert ok.total_cost > 9.0
    assert ok.net < 1.0
    bad = pair_net(0.48, 0.48, 10.0, cfg=cfg)
    assert not bad.ok


def test_max_second_from_fill(cfg):
    cfg.paper_mode = True
    max2 = max_second_leg_price(0.42, cfg=cfg)
    assert abs(max2 - (0.995 - 0.42)) < 1e-9
    tight = max_second_leg_price(0.90, cfg=cfg)
    assert tight < max_second_leg_price(0.40, cfg=cfg)


def _market() -> ArbMarket:
    return ArbMarket(
        condition_id="cond1",
        slug="test-yes-no",
        question="Test?",
        outcome_a="Yes",
        outcome_b="No",
        token_a="tokA",
        token_b="tokB",
        liquidity=1000,
        volume_24h=500,
        preferred=True,
    )


def test_leg_order_buys_rising_leg_first():
    m = _market()
    # Yes: thin ask + bid support + cheaper → more likely to rise. No: deep ask.
    quote = PairQuote(
        market=m,
        book_a=LegBook(bid=0.39, ask=0.40, bid_size=50, ask_size=3, spread=0.01, mid=0.395, fetched_ms=int(time.time()*1000)),
        book_b=LegBook(bid=0.48, ask=0.50, bid_size=50, ask_size=80, spread=0.02, mid=0.49, fetched_ms=int(time.time()*1000)),
        pair_ask=0.90,
        pair_bid=0.87,
        executable_shares=3,
        age_ms=10,
    )
    choice = choose_leg_order(quote)
    assert choice is not None
    assert choice.first_outcome == "Yes"
    assert choice.second_outcome == "No"
    assert choice.reason.startswith("rise_")


def test_leg_order_still_picks_when_one_side_thin():
    m = _market()
    quote = PairQuote(
        market=m,
        book_a=LegBook(bid=0.40, ask=0.41, bid_size=10, ask_size=50, spread=0.01, mid=0.405, fetched_ms=0),
        book_b=LegBook(bid=0.48, ask=0.49, bid_size=1, ask_size=0.5, spread=0.01, mid=0.485, fetched_ms=0),
        pair_ask=0.90,
        pair_bid=0.88,
        executable_shares=0.5,
        age_ms=0,
    )
    choice = choose_leg_order(quote)
    assert choice is not None


def test_maker_paper_path_plans_fill_at_limit(tmp_path, cfg):
    """Papertrader maker window: ask_sum ~1.03 still yields equal-share limit fills."""
    cfg.paper_mode = True
    cfg.paper_fak = True
    cfg.max_pair_cost = 0.94
    cfg.max_maker_ask_sum = 1.04
    cfg.min_edge = 0.025
    engine = ArbEngine(cfg, PairStore(tmp_path / "pairs.json"), discover_fn=lambda c: [])
    m = _market()
    quote = PairQuote(
        market=m,
        book_a=LegBook(bid=0.50, ask=0.52, bid_size=40, ask_size=40, spread=0.02, mid=0.51, fetched_ms=0),
        book_b=LegBook(bid=0.49, ask=0.51, bid_size=40, ask_size=40, spread=0.02, mid=0.50, fetched_ms=0),
        pair_ask=1.03,
        pair_bid=0.99,
        executable_shares=40,
        age_ms=0,
    )
    choice = choose_leg_order(quote)
    assert choice is not None
    plan = engine._plan_entry(quote, choice, pair_budget=45.0)
    assert plan is not None
    assert plan.get("reject") is None
    assert plan["mode"] == "maker_paper"
    assert plan["paper_fill_at_limit"] is True
    assert plan["limit_first"] + plan["limit_second"] <= cfg.max_pair_cost + 1e-9
    assert plan["shares"] > 0


def test_paper_fill_at_limit_buys_without_live_ask(tmp_path, cfg):
    cfg.paper_mode = True
    cfg.immediate_hedge = True
    store = PairStore(tmp_path / "pairs.json")
    engine = ArbEngine(cfg, store, discover_fn=lambda c: [])
    from arbitrage.pair_state import LegFill
    from arbitrage.executor_bridge import PaperBookClient, IntentExecutor
    from arbitrage.strategy import Intent

    pair = store.new_pair(
        condition_id="c", slug="s",
        first=LegFill(outcome="Yes", token="tokA"),
        second=LegFill(outcome="No", token="tokB"),
        target_shares=10, expected_net=0.5,
    )
    client = PaperBookClient(cfg)
    ex = IntentExecutor(engine, client)
    result = ex.execute(Intent(
        kind="enter_first", pair=pair, shares=10, limit_price=0.47, token="tokA", outcome="Yes",
        meta={"paper_fill_at_limit": True, "second_limit": 0.47},
    ))
    assert result["ok"]
    assert result.get("hedged") is True
    assert pair.status == "paired"
    assert abs(pair.first.avg_price - 0.47) < 1e-9
    assert abs(pair.second.avg_price - 0.47) < 1e-9


def test_live_plan_uses_resting_maker_window(tmp_path, cfg):
    cfg.paper_mode = False
    cfg.paper_fak = False
    cfg.live_maker_enabled = True
    cfg.max_pair_cost = 0.94
    cfg.max_maker_ask_sum = 1.04
    cfg.min_edge = 0.025
    engine = ArbEngine(cfg, PairStore(tmp_path / "pairs.json"), discover_fn=lambda c: [])
    m = _market()
    quote = PairQuote(
        market=m,
        book_a=LegBook(bid=0.50, ask=0.52, bid_size=40, ask_size=40, spread=0.02, mid=0.51, fetched_ms=0),
        book_b=LegBook(bid=0.49, ask=0.51, bid_size=40, ask_size=40, spread=0.02, mid=0.50, fetched_ms=0),
        pair_ask=1.03,
        pair_bid=0.99,
        executable_shares=40,
        age_ms=0,
    )
    choice = choose_leg_order(quote)
    plan = engine._plan_entry(quote, choice, pair_budget=45.0)
    assert plan is not None
    assert plan.get("reject") is None
    assert plan["mode"] == "maker_live"
    assert plan["resting_limit"] is True
    assert plan["paper_fill_at_limit"] is False
    assert plan["limit_first"] + plan["limit_second"] <= cfg.max_pair_cost + 1e-9


def test_live_plan_rejects_when_maker_disabled(tmp_path, cfg):
    cfg.paper_mode = False
    cfg.live_maker_enabled = False
    cfg.max_pair_cost = 0.94
    engine = ArbEngine(cfg, PairStore(tmp_path / "pairs.json"), discover_fn=lambda c: [])
    m = _market()
    quote = PairQuote(
        market=m,
        book_a=LegBook(bid=0.50, ask=0.52, bid_size=40, ask_size=40, spread=0.02, mid=0.51, fetched_ms=0),
        book_b=LegBook(bid=0.49, ask=0.51, bid_size=40, ask_size=40, spread=0.02, mid=0.50, fetched_ms=0),
        pair_ask=1.03,
        pair_bid=0.99,
        executable_shares=40,
        age_ms=0,
    )
    choice = choose_leg_order(quote)
    plan = engine._plan_entry(quote, choice, pair_budget=45.0)
    assert plan is not None
    assert plan.get("reject") in ("ask_sum_too_high", "not_taker", "pair_cost_above_cap", "net_below_min_edge")


def test_live_client_rejects_synthetic_fill_at_limit(cfg):
    from arbitrage.executor_bridge import LiveBookClient

    cfg.paper_mode = False
    client = LiveBookClient(cfg, ".env", client=object())
    fill = client.buy("tok", 10, 0.47, fill_at_limit=True)
    assert not fill.ok
    assert fill.reason == "live_rejects_synthetic_fill"
    client.close()


class ScriptedClient:
    def __init__(self, fills: list[FillResult]):
        self.fills = list(fills)
        self.calls = []

    def buy(self, token, shares, limit_price, *, fill_at_limit: bool = False, resting: bool = False):
        self.calls.append(("buy", token, shares, limit_price, fill_at_limit, resting))
        return self.fills.pop(0)

    def sell(self, token, shares, limit_price):
        self.calls.append(("sell", token, shares, limit_price))
        return self.fills.pop(0)


def test_partial_first_sizes_second(tmp_path, cfg):
    cfg.immediate_hedge = False
    store = PairStore(tmp_path / "pairs.json")
    engine = ArbEngine(cfg, store, discover_fn=lambda c: [])
    pair = store.new_pair(
        condition_id="c",
        slug="s",
        first=__import__("arbitrage.pair_state", fromlist=["LegFill"]).LegFill(outcome="Yes", token="tokA"),
        second=__import__("arbitrage.pair_state", fromlist=["LegFill"]).LegFill(outcome="No", token="tokB"),
        target_shares=20,
        expected_net=0.5,
    )
    client = ScriptedClient([
        FillResult(True, shares=7.5, avg_price=0.40, fee=0.03, notional=3.0, order_id="o1"),
        FillResult(True, shares=7.5, avg_price=0.50, fee=0.04, notional=3.75, order_id="o2"),
    ])
    ex = IntentExecutor(engine, client)
    r1 = ex.execute(Intent(kind="enter_first", pair=pair, shares=20, limit_price=0.42, token="tokA", outcome="Yes"))
    assert r1["ok"]
    assert pair.status == "awaiting_hedge"
    assert pair.first.shares == 7.5
    assert pair.max_second_price is not None
    # Hedge only the filled amount
    r2 = ex.execute(Intent(kind="hedge_second", pair=pair, shares=pair.unpaired_shares(), limit_price=pair.max_second_price, token="tokB", outcome="No"))
    assert r2["ok"]
    assert pair.status == "paired"
    assert pair.second.shares == 7.5
    assert pair.locked_profit is not None


def test_immediate_hedge_after_first_fill(tmp_path, cfg):
    cfg.immediate_hedge = True
    store = PairStore(tmp_path / "pairs.json")
    engine = ArbEngine(cfg, store, discover_fn=lambda c: [])
    from arbitrage.pair_state import LegFill

    pair = store.new_pair(
        condition_id="c", slug="s",
        first=LegFill(outcome="Yes", token="tokA"),
        second=LegFill(outcome="No", token="tokB"),
        target_shares=10, expected_net=0.2,
    )
    client = ScriptedClient([
        FillResult(True, shares=10.0, avg_price=0.40, fee=0.04, notional=4.0, order_id="o1"),
        FillResult(True, shares=10.0, avg_price=0.48, fee=0.05, notional=4.8, order_id="o2"),
    ])
    ex = IntentExecutor(engine, client)
    result = ex.execute(Intent(kind="enter_first", pair=pair, shares=10, limit_price=0.42, token="tokA", outcome="Yes"))
    assert result["ok"]
    assert result.get("hedged") is True
    assert pair.status == "paired"
    assert pair.second.shares == 10.0
    assert len(client.calls) == 2


def test_hedge_rejects_above_cap_then_timeout_close(tmp_path, cfg):
    store = PairStore(tmp_path / "pairs.json")
    now = {"t": 1_000.0}

    def clock():
        return now["t"]

    engine = ArbEngine(cfg, store, discover_fn=lambda c: [], clock=clock)
    from arbitrage.pair_state import LegFill

    pair = store.new_pair(
        condition_id="c", slug="s",
        first=LegFill(outcome="Yes", token="tokA"),
        second=LegFill(outcome="No", token="tokB"),
        target_shares=10, expected_net=0.2,
    )
    engine.on_first_fill(pair, 10.0, 0.45, 0.05, "o1")
    assert pair.status == "awaiting_hedge"
    cap = pair.max_second_price
    assert cap is not None and cap < 0.55

    # Mock fetch_book via monkeypatch on strategy module used by engine
    import arbitrage.strategy as strat_mod

    class FakeBook:
        ask = 0.60  # above cap
        bid = 0.44
        bid_size = 10
        ask_size = 10

    strat_mod.fetch_book = lambda token, session=None: FakeBook()
    intents = engine.scan_hedges_and_exits()
    assert intents == [] or all(i.kind != "hedge_second" for i in intents)

    now["t"] += cfg.hedge_timeout_seconds + 1
    intents = engine.scan_hedges_and_exits()
    assert any(i.kind == "close_unpaired" for i in intents)


def test_restart_persists_unpaired(tmp_path, cfg):
    path = tmp_path / "pairs.json"
    store = PairStore(path)
    from arbitrage.pair_state import LegFill

    pair = store.new_pair(
        condition_id="c", slug="s",
        first=LegFill(outcome="Yes", token="tokA"),
        second=LegFill(outcome="No", token="tokB"),
        target_shares=5, expected_net=0.1,
    )
    engine = ArbEngine(cfg, store, discover_fn=lambda c: [])
    engine.on_first_fill(pair, 5.0, 0.40, 0.02, "o1")
    store2 = PairStore(path)
    loaded = store2.get(pair.pair_id)
    assert loaded is not None
    assert loaded.status == "awaiting_hedge"
    assert loaded.first.shares == 5.0
    assert loaded.unpaired_shares() == 5.0


def test_no_duplicate_second_overfill(tmp_path, cfg):
    store = PairStore(tmp_path / "pairs.json")
    engine = ArbEngine(cfg, store, discover_fn=lambda c: [])
    from arbitrage.pair_state import LegFill

    pair = store.new_pair(
        condition_id="c", slug="s",
        first=LegFill(outcome="Yes", token="tokA"),
        second=LegFill(outcome="No", token="tokB"),
        target_shares=10, expected_net=0.1,
    )
    engine.on_first_fill(pair, 10.0, 0.40, 0.02, "o1")
    engine.on_second_fill(pair, 10.0, 0.50, 0.02, "o2")
    engine.on_second_fill(pair, 10.0, 0.50, 0.02, "o3")  # duplicate
    assert pair.second.shares == 10.0
    assert pair.status == "paired"


def test_missing_liquidity_skips_entry(tmp_path, cfg):
    store = PairStore(tmp_path / "pairs.json")
    m = _market()

    def discover(_):
        return [m]

    import arbitrage.strategy as strat_mod

    strat_mod.quote_pair = lambda market, cfg, session=None: (None, "ask_size_too_small")
    engine = ArbEngine(cfg, store, discover_fn=discover)
    intents = engine.scan_entries(cash=1000)
    assert intents == []
