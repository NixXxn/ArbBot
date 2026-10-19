#!/usr/bin/env python3
"""Arbot paper runner: fee-aware YES/NO arbitrage with sequential hedge."""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

# Allow `python scripts/run_arb_paper.py` and package imports from scripts/
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from arbitrage.config import ArbConfig, load_arb_config
from arbitrage.executor_bridge import IntentExecutor, PaperBookClient
from arbitrage.pair_state import PairStore
from arbitrage.strategy import ArbEngine
from statistics_resets import load as load_resets, visible as stats_visible

D = Decimal
log = logging.getLogger("arbot.paper")


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


def metrics(rows: list[dict]) -> dict:
    settled = [r for r in rows if r.get("status") == "settled"]
    pnl = sum((D(str(r.get("net_pnl") or 0)) for r in settled), D(0))
    cost = sum((D(str(r.get("cost") or 0)) + D(str(r.get("fee") or 0)) for r in settled), D(0))
    wins = [r for r in settled if D(str(r.get("net_pnl") or 0)) > 0]
    return {
        "trades": len(rows),
        "settled": len(settled),
        "open": len(rows) - len(settled),
        "wins": len(wins),
        "win_rate": float(D(len(wins)) / D(len(settled))) if settled else None,
        "roi": float(pnl / cost) if cost else None,
        "pnl": float(pnl),
        "settled_cost": float(cost),
        "avg_entry": None,
        "breakeven_win_rate": None,
    }


def pair_dashboard_rows(store: PairStore) -> list[dict]:
    now = int(time.time() * 1000)
    rows = []
    for p in store.all():
        rows.append({
            "pair_id": p.pair_id,
            "slug": p.slug,
            "status": p.status,
            "first_outcome": p.first.outcome,
            "first_shares": p.first.shares,
            "first_avg": p.first.avg_price,
            "first_fee": p.first.fee,
            "second_outcome": p.second.outcome,
            "second_shares": p.second.shares,
            "second_avg": p.second.avg_price,
            "second_fee": p.second.fee,
            "expected_net": p.expected_net,
            "locked_profit": p.locked_profit,
            "realized_net": p.realized_net,
            "unpaired_shares": p.unpaired_shares(),
            "unpaired_age_s": p.unpaired_age_seconds(now),
            "max_second_price": p.max_second_price,
            "escalation_reason": p.escalation_reason,
        })
    return rows


def pnl_breakdown(store: PairStore) -> dict:
    locked = 0.0
    expected = 0.0
    unpaired_risk = 0.0
    realized = 0.0
    for p in store.all():
        if p.realized_net is not None and p.status == "closed":
            realized += p.realized_net
        if p.status == "paired" and p.locked_profit is not None:
            locked += p.locked_profit
        if p.status in ("awaiting_hedge", "pending_first", "escalating"):
            if p.expected_net is not None:
                expected += p.expected_net
            unpaired_risk += p.first.notional
        elif p.status == "paired" and p.expected_net is not None and p.locked_profit is None:
            expected += p.expected_net
    return {
        "locked_pair_profit": locked,
        "expected_profit": expected,
        "unpaired_directional_risk": unpaired_risk,
        "realized_pnl": realized,
    }


def build_portfolio(data_dir: Path, cfg: ArbConfig, budget: float, store: PairStore, trades: list[dict],
                    resets_path: Path | None, decisions: list[dict]) -> dict:
    baselines = load_resets(resets_path) if resets_path else {}
    visible = [t for t in trades if stats_visible(t, baselines)]
    arb_stats = metrics([t for t in visible if t.get("strategy") == "arbitrage"])
    breakdown = pnl_breakdown(store)
    exposure = sum(
        (p.first.notional + p.second.notional)
        for p in store.open_pairs()
    )
    equity = float(budget) + float(arb_stats["pnl"])
    return {
        "started_at_ms": int(time.time() * 1000),
        "updated_at_ms": int(time.time() * 1000),
        "budget": budget,
        "strategy": "arbitrage",
        "exposure_rate": 0.30,
        "equity": equity,
        "open_exposure": exposure,
        "max_exposure": max(0.0, equity * 0.30),
        "available": max(0.0, equity - exposure),
        "trades": trades[-500:],
        "strategy_stats": {
            "arbitrage": arb_stats,
            "combined": arb_stats,
        },
        "pairs": pair_dashboard_rows(store),
        "pnl_breakdown": breakdown,
        "arb_config": cfg.to_dict(),
        "recent_decisions": decisions[-40:],
        "engine_health": {"arbitrage": {"signals": len(decisions), "status": "scanning"}},
        "risk_controls": {
            "mode": "arb_limits",
            "skips": {},
        },
    }


def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("trades") if isinstance(data, dict) else []
    return list(rows) if isinstance(rows, list) else []


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Compat: data_dir [config] [seconds] [budget] [stats_reset]
    data_dir = Path(argv[0] if argv else "data").resolve()
    config_path = Path(argv[1]) if len(argv) > 1 and not argv[1].replace(".", "", 1).isdigit() else (
        Path(__file__).resolve().parents[1] / "config" / "arbitrage.yaml"
    )
    # Flexible arg parse for dashboard: data_dir leadlag app seconds strategy budget resets
    # When called like old multi_paper: data_dir binary app seconds strategy budget resets
    budget = 1000.0
    seconds = 31536000.0
    resets_path = data_dir / "replica-stat-resets.json"
    if len(argv) >= 6:
        # dashboard form: data_dir /app/leadlag /app seconds strategy budget [resets]
        try:
            seconds = float(argv[3])
        except ValueError:
            seconds = 31536000.0
        try:
            budget = float(argv[5])
        except ValueError:
            budget = 1000.0
        if len(argv) >= 7:
            resets_path = Path(argv[6])
        config_path = Path(__file__).resolve().parents[1] / "config" / "arbitrage.yaml"
    elif len(argv) >= 2:
        try:
            if Path(argv[1]).suffix in (".yaml", ".yml"):
                config_path = Path(argv[1])
            else:
                budget = float(argv[1])
        except ValueError:
            pass
        if len(argv) >= 3:
            try:
                seconds = float(argv[2])
            except ValueError:
                pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    data_dir.mkdir(parents=True, exist_ok=True)
    def apply_overlay(base: ArbConfig) -> ArbConfig:
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

    cfg = apply_overlay(load_arb_config(config_path))
    cfg.paper_mode = True
    # Overlay dashboard budget
    if budget > 0:
        cfg.starting_balance = budget

    portfolio_path = data_dir / "replica-paper-portfolio.json"
    journal_path = data_dir / "replica-paper-arbitrage.jsonl"
    store = PairStore(data_dir / "arb-pairs.json")
    trades = load_trades(portfolio_path)

    # On restart: resume unpaired legs — do not open new entries until risk allows.
    unpaired = [p for p in store.open_pairs() if p.status in ("awaiting_hedge", "escalating", "pending_first")]
    if unpaired:
        log.warning("restart with %s open unpaired/pending pair(s) — hedges first", len(unpaired))
        append_event(journal_path, {
            "type": "restart_recovery",
            "unpaired": len(unpaired),
            "pair_ids": [p.pair_id for p in unpaired],
            "at_ms": int(time.time() * 1000),
        })

    engine = ArbEngine(cfg, store)
    client = PaperBookClient(cfg, session=engine.session)
    executor = IntentExecutor(engine, client)
    last_overlay_mtime = 0.0

    append_event(journal_path, {
        "type": "arbot_start",
        "strategy": "arbitrage",
        "budget": budget,
        "config": cfg.to_dict(),
        "at_ms": int(time.time() * 1000),
    })

    deadline = time.time() + seconds
    while time.time() < deadline:
        # Hot-reload dashboard overlay without requiring a full process restart.
        overlay_path = data_dir / "arb-settings.json"
        try:
            mtime = overlay_path.stat().st_mtime if overlay_path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if mtime > last_overlay_mtime:
            last_overlay_mtime = mtime
            engine.cfg = apply_overlay(load_arb_config(config_path))
            engine.cfg.paper_mode = True
            if budget > 0:
                engine.cfg.starting_balance = budget
            client.cfg = engine.cfg
            append_event(journal_path, {
                "type": "arb_settings_reloaded",
                "config": engine.cfg.to_dict(),
                "at_ms": int(time.time() * 1000),
            })
            log.info("reloaded arb settings from %s", overlay_path)

        cash = budget
        # Approximate free cash: budget - open notionals + realized
        breakdown = pnl_breakdown(store)
        open_n = sum(p.first.notional + p.second.notional for p in store.open_pairs())
        cash = max(0.0, budget + breakdown["realized_pnl"] - open_n)
        cfg = engine.cfg

        results = []
        # Always process hedges/exits before new entries.
        for intent in engine.scan_hedges_and_exits():
            result = executor.execute(intent)
            results.append(result)
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
                })

        risk = engine.risk()
        if risk.entries_allowed and not any(
            p.status in ("awaiting_hedge", "escalating") for p in store.open_pairs()
        ):
            # Allow entries even with awaiting hedges if under unpaired cap — risk gate handles it.
            pass
        if risk.entries_allowed:
            for intent in engine.scan_entries(cash=cash):
                result = executor.execute(intent)
                results.append(result)
                append_event(journal_path, {"type": "arb_exec", **result, "at_ms": int(time.time() * 1000)})
                if result.get("ok") and intent.pair:
                    trades.append({
                        "strategy": "arbitrage",
                        "status": "open",
                        "slug": intent.pair.slug,
                        "outcome": intent.outcome,
                        "shares": intent.pair.first.shares,
                        "price": intent.pair.first.avg_price,
                        "cost": intent.pair.first.notional,
                        "fee": intent.pair.first.fee,
                        "net_pnl": 0.0,
                        "at_ms": int(time.time() * 1000),
                        "kind": "enter_first",
                        "pair_id": intent.pair.pair_id,
                    })

        # Mark open trades settled when pair closes with realized
        open_ids = {p.pair_id for p in store.open_pairs()}
        for t in trades:
            if t.get("status") == "open" and t.get("pair_id") and t["pair_id"] not in open_ids:
                pair = store.get(t["pair_id"])
                if pair and pair.realized_net is not None:
                    t["status"] = "settled"
                    t["net_pnl"] = pair.realized_net
                elif pair and pair.status == "paired" and pair.locked_profit is not None:
                    # Locked but not exited — keep open for exposure, reflect locked in breakdown
                    pass

        portfolio = build_portfolio(data_dir, cfg, budget, store, trades, resets_path, engine.decisions)
        atomic_json(portfolio_path, portfolio)
        append_event(journal_path, {
            "type": "health",
            "strategy": "arbitrage",
            "open_pairs": risk.open_pairs,
            "unpaired_notional": risk.unpaired_notional,
            "pnl_breakdown": portfolio["pnl_breakdown"],
            "at_ms": int(time.time() * 1000),
        })
        time.sleep(max(1.0, cfg.poll_interval_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
