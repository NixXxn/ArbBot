#!/usr/bin/env python3
"""Live runner for Polymarket ↔ Kalshi cross-venue arbitrage."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from arbitrage.config import ArbConfig
from arbitrage.executor_bridge import LiveBookClient
from cross_venue.config import load_cross_config
from cross_venue.executor import CrossExecutor
from cross_venue.kalshi_client import KalshiClient, KalshiOrderError
from cross_venue.strategy import CrossEngine
from live_executor import ExecutionStopped, enabled_env, save
from run_arb_paper import append_event, atomic_json
from run_cross_paper import load_state

log = logging.getLogger("arbot.cross.live")


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / "live-stop-requested").exists()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = Path(argv[0] if argv else os.environ.get("BOT_DATA_DIR", "data")).resolve()
    state_path = Path(argv[1]) if len(argv) >= 2 else data_dir / "replica-live-cross-state.json"
    env_path = argv[2] if len(argv) >= 3 else os.environ.get("BOT_ENV_PATH", ".env")
    try:
        seconds = float(argv[3]) if len(argv) >= 4 else 31536000.0
    except ValueError:
        seconds = 31536000.0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    data_dir.mkdir(parents=True, exist_ok=True)

    # Load .env into process for Kalshi + Poly helpers.
    env_file = Path(env_path).expanduser()
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    try:
        enabled_env(env_path)
    except ExecutionStopped as exc:
        save(state_path, {
            "mode": "live", "strategy": "cross_venue", "stage": "requires_attention",
            "reason": str(exc), "at_ms": int(time.time() * 1000),
        })
        log.error("live gated: %s", exc)
        return 2

    config_path = Path(__file__).resolve().parents[1] / "config" / "cross_venue.yaml"
    cfg = load_cross_config(config_path)
    cfg.paper_mode = False

    portfolio_path = data_dir / "replica-live-cross-portfolio.json"
    journal_path = data_dir / "replica-live-cross.jsonl"
    state = {
        "mode": "live",
        "strategy": "cross_venue",
        "stage": "connect",
        "live_execution": "cross_clob_kalshi",
        "started_at_ms": int(time.time() * 1000),
    }
    save(state_path, state)

    kalshi = KalshiClient(cfg.kalshi_base_url)
    if not kalshi.authenticated:
        state.update(stage="requires_attention", reason="kalshi_auth_missing")
        save(state_path, state)
        return 2

    poly_cfg = ArbConfig(paper_mode=False, paper_fak=False)
    poly = LiveBookClient(poly_cfg, env_path)
    try:
        poly.connect(redemption=False)
    except ExecutionStopped as exc:
        state.update(stage="requires_attention", reason=f"poly_connect:{exc}")
        save(state_path, state)
        return 2

    try:
        kalshi_cash = kalshi.balance_dollars()
    except KalshiOrderError as exc:
        state.update(stage="requires_attention", reason=f"kalshi_balance:{exc}")
        save(state_path, state)
        poly.close()
        return 2

    poly_cash = poly.wallet_capital() or 0.0
    capital = min(float(kalshi_cash), float(poly_cash) if poly_cash > 0 else float(kalshi_cash))
    state.update(
        stage="monitor",
        portfolio={
            "capital": capital, "equity": capital, "available": capital,
            "kalshi_balance": kalshi_cash, "poly_balance": poly_cash,
            "open_exposure": 0, "max_exposure": capital * 0.30, "exposure_rate": 0.30,
        },
    )
    save(state_path, state)

    engine = CrossEngine(cfg, kalshi=kalshi)
    executor = CrossExecutor(cfg, kalshi, poly_client=poly, poly_cfg=poly_cfg, env_path=env_path)
    persisted = load_state(portfolio_path)
    trades = list(persisted.get("trades") or [])
    realized = float(persisted.get("realized_pnl") or 0.0)

    append_event(journal_path, {
        "type": "cross_live_start",
        "capital": capital,
        "kalshi_balance": kalshi_cash,
        "poly_balance": poly_cash,
        "config": cfg.to_dict(),
        "at_ms": int(time.time() * 1000),
    })

    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            if stop_requested(data_dir):
                state["stage"] = "stop_requested"
                save(state_path, state)
                break

            try:
                kalshi_cash = kalshi.balance_dollars()
            except KalshiOrderError:
                pass
            poly_cash = poly.wallet_capital() or poly_cash
            capital = min(float(kalshi_cash), float(poly_cash) if poly_cash > 0 else float(kalshi_cash))
            cash = max(0.0, min(capital * 0.30, cfg.position_usd * cfg.max_open_pairs))

            open_ids = {str(t.get("pair_id")) for t in trades if t.get("status") == "open"}
            intents = engine.scan(cash=cash, open_ids=open_ids)
            for intent in intents:
                if stop_requested(data_dir):
                    break
                result = executor.execute(intent)
                append_event(journal_path, {
                    "type": "cross_live_exec",
                    "ok": result.ok,
                    "reason": result.reason,
                    "direction": intent.opportunity.direction,
                    "shares": result.shares,
                    "cost": result.cost,
                    "net_pnl": result.net_pnl,
                    "unwound": result.unwound,
                    "poly": result.poly_fill,
                    "kalshi": result.kalshi_fill,
                    "at_ms": int(time.time() * 1000),
                })
                if result.ok:
                    trades.append({
                        "strategy": "cross_venue",
                        "status": "settled",
                        "pair_id": intent.opportunity.pair_id,
                        "poly_slug": intent.opportunity.poly_slug,
                        "kalshi_ticker": intent.opportunity.kalshi_ticker,
                        "direction": intent.opportunity.direction,
                        "shares": result.shares,
                        "cost": result.cost,
                        "net_pnl": result.net_pnl,
                        "live": True,
                        "at_ms": int(time.time() * 1000),
                    })
                    realized += result.net_pnl
                    log.info(
                        "live cross fill %s shares=%.2f net=%.2f",
                        intent.opportunity.direction, result.shares, result.net_pnl,
                    )
                else:
                    log.warning("live cross miss: %s", result.reason)

            settled = [t for t in trades if t.get("status") == "settled"]
            wins = [t for t in settled if float(t.get("net_pnl") or 0) > 0]
            summary = engine.candidate_summary()
            candidates = sorted(
                engine.last_candidates,
                key=lambda r: float(r.get("spread_pct") if r.get("spread_pct") is not None else -999),
                reverse=True,
            )[:60]
            stats = {
                "trades": len(trades),
                "settled": len(settled),
                "open": 0,
                "wins": len(wins),
                "win_rate": (len(wins) / len(settled)) if settled else None,
                "pnl": realized,
                "roi": (realized / capital) if capital else None,
                "candidates": summary["candidate_count"],
                "eligible": summary["eligible_count"],
                "best_spread_pct": summary["best_spread_pct"],
            }
            portfolio = {
                "updated_at_ms": int(time.time() * 1000),
                "mode": "live",
                "strategy": "cross_venue",
                "capital": capital,
                "equity": capital + realized,  # approx; balances refreshed above
                "kalshi_balance": kalshi_cash,
                "poly_balance": poly_cash,
                "available": capital,
                "open_exposure": 0.0,
                "max_exposure": capital * 0.30,
                "realized_pnl": realized,
                "trades": trades[-500:],
                "strategy_stats": {"cross_venue": stats, "combined": stats},
                "candidate_count": summary["candidate_count"],
                "eligible_count": summary["eligible_count"],
                "best_spread_pct": summary["best_spread_pct"],
                "candidates": candidates,
                "pairs": [
                    {
                        "pair_id": c.get("slug", "") + "::" + c.get("kalshi_ticker", ""),
                        "slug": c.get("slug"),
                        "kalshi_ticker": c.get("kalshi_ticker"),
                        "status": "eligible" if c.get("eligible") else (c.get("reject") or "quoted"),
                        "direction": c.get("direction"),
                        "poly_ask": c.get("poly_ask"),
                        "kalshi_ask": c.get("kalshi_ask"),
                        "cost": c.get("cost"),
                        "edge": c.get("edge"),
                        "spread_pct": c.get("spread_pct"),
                        "match_score": c.get("match_score"),
                        "source": c.get("source"),
                        "question": c.get("question"),
                        "shares": c.get("shares"),
                    }
                    for c in candidates[:40]
                ],
                "pnl_breakdown": {
                    "locked_pair_profit": 0.0,
                    "expected_profit": 0.0,
                    "unpaired_directional_risk": 0.0,
                    "realized_pnl": realized,
                },
                "cross_config": cfg.to_dict(),
                "recent_decisions": engine.decisions[-40:],
                "kalshi_authenticated": True,
            }
            # Prefer live wallet balances for equity display.
            try:
                portfolio["equity"] = min(kalshi.balance_dollars(), poly.wallet_capital() or kalshi_cash)
                portfolio["capital"] = portfolio["equity"]
            except Exception:
                pass
            atomic_json(portfolio_path, portfolio)
            # Surface under the durable live portfolio name when this is the sole live strategy.
            atomic_json(data_dir / "replica-portfolio-live.json", portfolio)
            state.update(
                stage="monitor",
                portfolio=portfolio,
                strategy_stats=stats,
                updated_at_ms=int(time.time() * 1000),
            )
            save(state_path, state)
            append_event(journal_path, {
                "type": "health",
                "strategy": "cross_venue",
                "live": True,
                "capital": portfolio.get("capital"),
                "matches": len(engine.last_matches),
                "candidates": summary["candidate_count"],
                "eligible": summary["eligible_count"],
                "best_spread_pct": summary["best_spread_pct"],
                "at_ms": int(time.time() * 1000),
            })
            time.sleep(max(2.0, cfg.poll_interval_seconds))
    except Exception as exc:
        state.update(
            stage="requires_attention",
            reason=str(exc),
            error_type=type(exc).__name__,
            at_ms=int(time.time() * 1000),
        )
        save(state_path, state)
        log.exception("cross live crashed")
        return 1
    finally:
        if state.get("stage") == "monitor":
            state["stage"] = "stopped"
            save(state_path, state)
        try:
            poly.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
