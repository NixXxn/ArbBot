#!/usr/bin/env python3
"""Arbot live runner: fee-aware YES/NO arb with real CLOB FOK fills + merge."""
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

from arbitrage.config import ArbConfig, load_arb_config
from arbitrage.executor_bridge import IntentExecutor, LiveBookClient
from arbitrage.pair_state import PairStore
from arbitrage.strategy import ArbEngine
from live_executor import ExecutionStopped, save
from run_arb_paper import (
    append_event,
    atomic_json,
    load_trades,
    metrics,
    pair_dashboard_rows,
    pnl_breakdown,
)
from statistics_resets import load as load_resets
from statistics_resets import visible as stats_visible

log = logging.getLogger("arbot.live")


def apply_overlay(base: ArbConfig, data_dir: Path) -> ArbConfig:
    overlay_path = data_dir / "arb-settings.json"
    if not overlay_path.exists():
        return base
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        if isinstance(overlay, dict):
            base.update({k: v for k, v in overlay.items() if k != "updated_at_ms"})
    except (OSError, ValueError):
        pass
    return base


def build_live_portfolio(
    cfg: ArbConfig,
    capital: float,
    store: PairStore,
    trades: list[dict],
    decisions: list[dict],
    *,
    resets_path: Path | None,
) -> dict:
    baselines = load_resets(resets_path) if resets_path else {}
    visible = [t for t in trades if stats_visible(t, baselines)]
    arb_stats = metrics([t for t in visible if t.get("strategy") == "arbitrage"])
    breakdown = pnl_breakdown(store)
    exposure = sum((p.first.notional + p.second.notional) for p in store.open_pairs())
    equity = float(capital)
    return {
        "started_at_ms": int(time.time() * 1000),
        "updated_at_ms": int(time.time() * 1000),
        "mode": "live",
        "strategy": "arbitrage",
        "exposure_rate": 0.30,
        "capital": equity,
        "equity": equity,
        "starting_capital": equity,
        "open_exposure": exposure,
        "max_exposure": max(0.0, equity * 0.30),
        "available": max(0.0, equity - exposure),
        "trades": trades[-500:],
        "strategy_stats": {"arbitrage": arb_stats, "combined": arb_stats},
        "pairs": pair_dashboard_rows(store),
        "pnl_breakdown": breakdown,
        "arb_config": cfg.to_dict(),
        "recent_decisions": decisions[-40:],
        "engine_health": {"arbitrage": {"signals": len(decisions), "status": "live_scanning"}},
        "risk_controls": {"mode": "arb_live_limits", "skips": {}},
    }


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / "live-stop-requested").exists()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # data_dir [state_path] [env_path] [seconds]
    data_dir = Path(argv[0] if argv else os.environ.get("BOT_DATA_DIR", "data")).resolve()
    state_path = Path(argv[1]) if len(argv) >= 2 else data_dir / "replica-live-state.json"
    env_path = argv[2] if len(argv) >= 3 else os.environ.get("BOT_ENV_PATH", ".env")
    try:
        seconds = float(argv[3]) if len(argv) >= 4 else 31536000.0
    except ValueError:
        seconds = 31536000.0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(__file__).resolve().parents[1] / "config" / "arbitrage.yaml"
    resets_path = data_dir / "replica-stat-resets.json"
    portfolio_path = data_dir / "replica-portfolio-live.json"
    journal_path = data_dir / "replica-live-arbitrage.jsonl"
    pairs_path = data_dir / "arb-pairs-live.json"

    cfg = apply_overlay(load_arb_config(config_path), data_dir)
    cfg.paper_mode = False
    cfg.paper_fak = False
    cfg.paper_relax_fees = False

    state = {
        "mode": "live",
        "strategy": "arbitrage",
        "stage": "connect",
        "live_execution": "clob",
        "started_at_ms": int(time.time() * 1000),
    }
    save(state_path, state)

    client = LiveBookClient(cfg, env_path)
    try:
        client.connect(redemption=True)
    except ExecutionStopped as exc:
        state.update(stage="requires_attention", reason=str(exc), error_type=type(exc).__name__)
        save(state_path, state)
        log.error("live connect failed: %s", exc)
        return 2

    capital = client.wallet_capital()
    if capital is None or capital <= 0:
        state.update(stage="requires_attention", reason="wallet_collateral_unavailable")
        save(state_path, state)
        client.close()
        return 2

    state.update(stage="monitor", portfolio={
        "capital": capital, "equity": capital, "available": capital,
        "open_exposure": 0, "max_exposure": capital * 0.30, "exposure_rate": 0.30,
    })
    save(state_path, state)

    store = PairStore(pairs_path)
    trades = load_trades(portfolio_path)
    unpaired = [p for p in store.open_pairs() if p.status in ("awaiting_hedge", "escalating", "pending_first")]
    if unpaired:
        log.warning("live restart with %s open unpaired/pending pair(s) — hedges first", len(unpaired))

    engine = ArbEngine(cfg, store)
    executor = IntentExecutor(engine, client)
    last_overlay_mtime = 0.0

    append_event(journal_path, {
        "type": "arbot_live_start",
        "strategy": "arbitrage",
        "capital": capital,
        "config": cfg.to_dict(),
        "at_ms": int(time.time() * 1000),
    })

    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            if stop_requested(data_dir):
                state["stage"] = "stop_requested"
                save(state_path, state)
                log.info("live stop requested")
                break

            overlay_path = data_dir / "arb-settings.json"
            try:
                mtime = overlay_path.stat().st_mtime if overlay_path.exists() else 0.0
            except OSError:
                mtime = 0.0
            if mtime > last_overlay_mtime:
                last_overlay_mtime = mtime
                engine.cfg = apply_overlay(load_arb_config(config_path), data_dir)
                engine.cfg.paper_mode = False
                engine.cfg.paper_fak = False
                engine.cfg.paper_relax_fees = False
                client.cfg = engine.cfg
                append_event(journal_path, {
                    "type": "arb_settings_reloaded",
                    "config": engine.cfg.to_dict(),
                    "at_ms": int(time.time() * 1000),
                })

            wallet = client.wallet_capital()
            if wallet is not None:
                capital = wallet

            for intent in engine.scan_hedges_and_exits():
                result = executor.execute(intent)
                append_event(journal_path, {"type": "arb_exec", **result, "at_ms": int(time.time() * 1000)})
                if result.get("ok") and intent.kind in ("close_unpaired", "exit_pair") and intent.pair:
                    trades.append({
                        "strategy": "arbitrage",
                        "status": "settled",
                        "slug": intent.pair.slug,
                        "outcome": "pair",
                        "shares": intent.shares,
                        "price": intent.pair.first.avg_price,
                        "cost": intent.pair.first.notional + intent.pair.second.notional,
                        "fee": intent.pair.first.fee + intent.pair.second.fee,
                        "net_pnl": intent.pair.realized_net or 0.0,
                        "at_ms": int(time.time() * 1000),
                        "kind": intent.kind,
                        "live": True,
                    })

            risk = engine.risk()
            # Live cash = wallet collateral minus open notionals (conservative).
            open_n = sum(p.first.notional + p.second.notional for p in store.open_pairs())
            cash = max(0.0, float(capital) - open_n)
            # Cap live sizing at 30% of wallet.
            cash = min(cash, float(capital) * 0.30)

            if risk.entries_allowed and not stop_requested(data_dir):
                for intent in engine.scan_entries(cash=cash):
                    result = executor.execute(intent)
                    append_event(journal_path, {"type": "arb_exec", **result, "at_ms": int(time.time() * 1000)})
                    if result.get("ok") and intent.pair:
                        trades.append({
                            "strategy": "arbitrage",
                            "status": "open" if intent.pair.status != "closed" else "settled",
                            "slug": intent.pair.slug,
                            "outcome": intent.outcome,
                            "shares": intent.pair.first.shares,
                            "price": intent.pair.first.avg_price,
                            "cost": intent.pair.first.notional + intent.pair.second.notional,
                            "fee": intent.pair.first.fee + intent.pair.second.fee,
                            "net_pnl": intent.pair.realized_net or intent.pair.locked_profit or 0.0,
                            "at_ms": int(time.time() * 1000),
                            "kind": "enter_first",
                            "pair_id": intent.pair.pair_id,
                            "live": True,
                            "merged": bool(result.get("merged")),
                        })

            open_ids = {p.pair_id for p in store.open_pairs()}
            for t in trades:
                if t.get("status") == "open" and t.get("pair_id") and t["pair_id"] not in open_ids:
                    pair = store.get(t["pair_id"])
                    if pair and pair.realized_net is not None:
                        t["status"] = "settled"
                        t["net_pnl"] = pair.realized_net

            # Refresh capital after fills/merges.
            wallet = client.wallet_capital()
            if wallet is not None:
                capital = wallet

            portfolio = build_live_portfolio(
                engine.cfg, capital, store, trades, engine.decisions, resets_path=resets_path
            )
            atomic_json(portfolio_path, portfolio)
            state.update(
                stage="monitor",
                portfolio=portfolio,
                strategy_stats=portfolio["strategy_stats"],
                risk_controls=portfolio["risk_controls"],
                pairs=portfolio["pairs"],
                pnl_breakdown=portfolio["pnl_breakdown"],
                updated_at_ms=int(time.time() * 1000),
            )
            save(state_path, state)
            append_event(journal_path, {
                "type": "health",
                "strategy": "arbitrage",
                "live": True,
                "capital": capital,
                "open_pairs": risk.open_pairs,
                "unpaired_notional": risk.unpaired_notional,
                "pnl_breakdown": portfolio["pnl_breakdown"],
                "at_ms": int(time.time() * 1000),
            })
            time.sleep(max(1.0, engine.cfg.poll_interval_seconds))
    except Exception as exc:
        state.update(
            stage="requires_attention",
            reason=str(exc),
            error_type=type(exc).__name__,
            at_ms=int(time.time() * 1000),
        )
        save(state_path, state)
        log.exception("live arb crashed")
        return 1
    finally:
        if state.get("stage") == "monitor":
            state["stage"] = "stopped"
            save(state_path, state)
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
