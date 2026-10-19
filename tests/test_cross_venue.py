"""Unit tests for Polymarket ↔ Kalshi cross-venue matching and edges."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from arbitrage.discovery import ArbMarket
from cross_venue.config import CrossConfig, MappedPair, load_cross_config
from cross_venue.executor import CrossExecutor
from cross_venue.kalshi_client import KalshiBook, KalshiClient, KalshiMarket
from cross_venue.match import match_markets, normalize_title, title_score
from cross_venue.strategy import CrossEngine, CrossIntent, CrossOpportunity


def test_normalize_and_score():
    assert "bitcoin" in normalize_title("Will Bitcoin hit $100k?")
    assert title_score("Fed cuts rates in October", "Fed cuts rates in October") == 1.0
    assert title_score("Fed cuts rates in October", "Will the Fed cut rates in October?") > 0.7


def test_mapped_pair_preferred():
    cfg = CrossConfig(auto_match=True, min_match_score=0.99)
    cfg.mapped_pairs = [MappedPair(poly_slug="fed-cut", kalshi_ticker="KXFED-1", aligned=True)]
    poly = [
        ArbMarket("c1", "fed-cut", "Fed cuts rates", "Yes", "No", "t1", "t2", 1000, 100, True, None),
        ArbMarket("c2", "other", "Totally unrelated", "Yes", "No", "t3", "t4", 1000, 100, False, None),
    ]
    kalshi = [
        KalshiMarket("KXFED-1", "Fed cuts rates", "EV", 0.4, 0.42, 0.55, 0.58, None, 10.0, None),
        KalshiMarket("OTHER", "Soccer game", "EV2", 0.1, 0.2, 0.7, 0.8, None, 1.0, None),
    ]
    matches = match_markets(poly, kalshi, cfg)
    assert any(m.source == "mapped" and m.kalshi.ticker == "KXFED-1" for m in matches)


def test_cross_opportunity_picks_cheaper_direction(monkeypatch):
    cfg = CrossConfig(min_edge=0.02, fee_buffer=0.01, max_pair_cost=0.99, min_size=1.0)
    engine = CrossEngine(cfg)

    poly = ArbMarket("c", "same-event", "Same event question", "Yes", "No", "yesTok", "noTok", 1e4, 1e3, True, None)
    kalshi = KalshiMarket("KXSAME", "Same event question", "EV", 0.4, 0.41, 0.5, 0.52, None, 100.0, None)
    matched = match_markets([poly], [kalshi], CrossConfig(auto_match=True, min_match_score=0.5))[0]

    def fake_book(token, session=None):
        if token == "yesTok":
            return SimpleNamespace(ask=0.40, ask_size=50.0, bid=0.39, bid_size=50.0)
        return SimpleNamespace(ask=0.62, ask_size=50.0, bid=0.61, bid_size=50.0)

    monkeypatch.setattr("cross_venue.strategy.fetch_book", fake_book)
    engine.kalshi.orderbook = lambda ticker, depth=10: KalshiBook(
        yes_bid=0.40, yes_ask=0.41, no_bid=0.50, no_ask=0.52,
        yes_bid_size=40, yes_ask_size=40, no_bid_size=40, no_ask_size=40,
    )
    opp = engine._quote_opportunity(matched, cash=100.0)
    assert opp is not None
    # poly yes 0.40 + kalshi no 0.52 + fee 0.01 = 0.93 → edge 0.07
    assert opp.direction == "poly_yes_kalshi_no"
    assert opp.cost < 0.95
    assert opp.edge >= 0.05


def test_load_cross_config_yaml(tmp_path):
    path = tmp_path / "cross.yaml"
    path.write_text(
        "cross_venue:\n  min_edge: 0.04\n  mapped_pairs:\n"
        "    - poly_slug: a\n      kalshi_ticker: B\n      aligned: false\n"
    )
    cfg = load_cross_config(path)
    assert cfg.min_edge == 0.04
    assert len(cfg.mapped_pairs) == 1
    assert cfg.mapped_pairs[0].kalshi_ticker == "B"
    assert cfg.mapped_pairs[0].aligned is False


def test_dashboard_accepts_cross_strategy():
    import dashboard_server

    assert dashboard_server.normalize_strategy("cross_venue") == "cross_venue"
    assert dashboard_server.normalize_strategy("combined") == "combined"
    assert dashboard_server.active_feeds("combined") == ("arbitrage", "cross_venue")


def test_kalshi_buy_ioc_payload(monkeypatch):
    from cross_venue.kalshi_client import KalshiClient

    client = KalshiClient()
    client.key_id = "kid"
    client._private_key = object()  # mark authenticated
    captured = {}

    def fake_request(method, path, *, params=None, json_body=None, auth=False):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"order": {"order_id": "o1", "fill_count": 3, "remaining_count": 0, "yes_price_dollars": "0.4100"}}

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(type(client), "authenticated", property(lambda self: True))
    out = client.buy_ioc("TICK", "yes", 3, 0.41)
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/orders"
    assert captured["body"]["time_in_force"] == "immediate_or_cancel"
    assert captured["body"]["yes_price_dollars"] == "0.4100"
    assert out["filled"] == 3
    assert out["ok"] is True


def test_cross_executor_paper():
    cfg = CrossConfig(paper_mode=True, fee_buffer=0.01)
    opp = CrossOpportunity(
        pair_id="p::k::poly_yes_kalshi_no",
        poly_slug="p", kalshi_ticker="k", direction="poly_yes_kalshi_no",
        poly_leg="yes", kalshi_leg="no", poly_token="t",
        poly_ask=0.4, kalshi_ask=0.5, cost=0.91, edge=0.09, shares=10,
        match_score=1.0, match_source="mapped", question="q",
    )
    # Explicit paper poly client so we don't open network sockets.
    from arbitrage.config import ArbConfig
    from arbitrage.executor_bridge import PaperBookClient

    ex = CrossExecutor(
        cfg, KalshiClient(), poly_client=PaperBookClient(ArbConfig(paper_mode=True)),
    )
    result = ex.execute(CrossIntent(kind="enter", opportunity=opp))
    assert result.ok
    assert result.shares == 10
    # settlement $1 − (poly+kalshi asks) − fee_buffer
    assert abs(result.net_pnl - (10 - 10 * 0.9 - 0.1)) < 1e-9
