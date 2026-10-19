#!/usr/bin/env python3
"""Paper runner for Polymarket ↔ Kalshi cross-venue arbitrage."""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cross_venue.config import load_cross_config
from cross_venue.strategy import CrossEngine

log = logging.getLogger("arbot.cross.paper")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2)
        handle.write("\n")
    temporary.replace(path)


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= 8 * 1024 * 1024:
        older = path.with_name(path.name + ".2")
        previous = path.with_name(path.name + ".1")
        older.unlink(missing_ok=True)
        if previous.exists():
            previous.replace(older)
        path.replace(previous)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"trades": [], "open": [], "realized_pnl": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"trades": [], "open": [], "realized_pnl": 0.0}
    except (OSError, ValueError):
        return {"trades": [], "open": [], "realized_pnl": 0.0}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = Path(argv[0] if argv else "data").resolve()
    budget = 1000.0
    seconds = 31536000.0
    if len(argv) >= 6:
        try:
            seconds = float(argv[3])
        except ValueError:
            pass
        try:
            budget = float(argv[5])
        except ValueError:
            pass
    elif len(argv) >= 2:
        try:
            budget = float(argv[1])
        except ValueError:
            pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(__file__).resolve().parents[1] / "config" / "cross_venue.yaml"
    cfg = load_cross_config(config_path)
    cfg.paper_mode = True

    portfolio_path = data_dir / "replica-paper-cross-portfolio.json"
    journal_path = data_dir / "replica-paper-cross.jsonl"
    state = load_state(portfolio_path)
    trades = list(state.get("trades") or [])
    open_rows = list(state.get("open") or [])
    realized = float(state.get("realized_pnl") or 0.0)

    engine = CrossEngine(cfg)
    append_event(journal_path, {
        "type": "cross_start",
        "budget": budget,
        "kalshi_auth": engine.kalshi.authenticated,
        "config": cfg.to_dict(),
        "at_ms": int(time.time() * 1000),
    })

    deadline = time.time() + seconds
    while time.time() < deadline:
        open_ids = {str(r.get("pair_id")) for r in open_rows}
        open_notional = sum(float(r.get("cost") or 0) * float(r.get("shares") or 0) for r in open_rows)
        cash = max(0.0, budget + realized - open_notional)

        intents = engine.scan(cash=cash, open_ids=open_ids)
        for intent in intents:
            opp = intent.opportunity
            # Paper fill both legs at asks (cross-venue complementary).
            payout = opp.shares * 1.0
            cost = opp.shares * (opp.poly_ask + opp.kalshi_ask)
            fee = opp.shares * cfg.fee_buffer
            net = payout - cost - fee
            row = {
                "pair_id": opp.pair_id,
                "strategy": "cross_venue",
                "status": "settled",
                "poly_slug": opp.poly_slug,
                "kalshi_ticker": opp.kalshi_ticker,
                "direction": opp.direction,
                "shares": opp.shares,
                "poly_ask": opp.poly_ask,
                "kalshi_ask": opp.kalshi_ask,
                "cost": cost + fee,
                "net_pnl": net,
                "edge": opp.edge,
                "match_score": opp.match_score,
                "match_source": opp.match_source,
                "question": opp.question,
                "at_ms": int(time.time() * 1000),
                "paper": True,
            }
            trades.append(row)
            realized += net
            append_event(journal_path, {"type": "cross_fill", **row})
            log.info(
                "paper cross fill %s edge=%.3f net=%.2f score=%.2f",
                opp.direction, opp.edge, net, opp.match_score,
            )

        settled = [t for t in trades if t.get("status") == "settled"]
        wins = [t for t in settled if float(t.get("net_pnl") or 0) > 0]
        pnl = sum(float(t.get("net_pnl") or 0) for t in settled)
        stats = {
            "trades": len(trades),
            "settled": len(settled),
            "open": len(open_rows),
            "wins": len(wins),
            "win_rate": (len(wins) / len(settled)) if settled else None,
            "pnl": pnl,
            "roi": None,
        }
        equity = budget + realized
        portfolio = {
            "updated_at_ms": int(time.time() * 1000),
            "strategy": "cross_venue",
            "budget": budget,
            "equity": equity,
            "capital": equity,
            "open_exposure": open_notional,
            "max_exposure": equity * 0.30,
            "available": max(0.0, equity - open_notional),
            "realized_pnl": realized,
            "trades": trades[-500:],
            "open": open_rows,
            "strategy_stats": {"cross_venue": stats, "combined": stats},
            "pairs": [
                {
                    "pair_id": m.poly.slug + "::" + m.kalshi.ticker,
                    "slug": m.poly.slug,
                    "kalshi_ticker": m.kalshi.ticker,
                    "status": "matched",
                    "match_score": m.score,
                    "source": m.source,
                    "question": m.poly.question,
                }
                for m in engine.last_matches[:40]
            ],
            "pnl_breakdown": {
                "locked_pair_profit": 0.0,
                "expected_profit": 0.0,
                "unpaired_directional_risk": 0.0,
                "realized_pnl": realized,
            },
            "cross_config": cfg.to_dict(),
            "recent_decisions": engine.decisions[-40:],
            "kalshi_authenticated": engine.kalshi.authenticated,
            "engine_health": {
                "cross_venue": {
                    "signals": len(engine.decisions),
                    "matches": len(engine.last_matches),
                    "status": "scanning",
                    "kalshi_auth": engine.kalshi.authenticated,
                }
            },
        }
        atomic_json(portfolio_path, portfolio)
        append_event(journal_path, {
            "type": "health",
            "strategy": "cross_venue",
            "matches": len(engine.last_matches),
            "kalshi_auth": engine.kalshi.authenticated,
            "equity": equity,
            "at_ms": int(time.time() * 1000),
        })
        time.sleep(max(2.0, cfg.poll_interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
