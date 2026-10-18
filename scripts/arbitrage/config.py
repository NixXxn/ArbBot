"""Arbitrage settings with paper-safe defaults."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML subset loader (mappings + nested arbitrage block)."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(0, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        value = rest.strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent + 2, child))
            continue
        if value.lower() in ("true", "false"):
            parent[key] = value.lower() == "true"
        else:
            try:
                parent[key] = int(value) if "." not in value else float(value)
            except ValueError:
                parent[key] = value.strip("'\"")
    return root


@dataclass
class ArbConfig:
    max_pair_cost: float = 0.94
    min_edge: float = 0.025
    fee_buffer: float = 0.01
    safety_margin: float = 0.005
    taker_fee_rate: float = 0.01
    maker_fee_rate: float = 0.0
    slippage_buffer: float = 0.002
    min_liquidity: float = 400.0
    min_volume_24h: float = 150.0
    min_ask: float = 0.02
    max_ask: float = 0.95
    min_ask_size: float = 3.0
    hedge_depth_multiple: float = 1.25
    position_usd: float = 45.0
    max_position_usd: float = 110.0
    max_open_pairs: int = 8
    max_unpaired_notional: float = 45.0
    max_concurrent_unpaired: int = 2
    max_unpaired_loss: float = 0.02
    max_capital_locked: float = 500.0
    hedge_timeout_seconds: float = 60.0
    max_quote_age_ms: int = 2500
    min_pair_profit_pct: float = 0.005
    pair_bid_sum_exit: float = 0.99
    scan_limit: int = 250
    prefer_crypto_weather: bool = True
    maker_tick: float = 0.01
    immediate_hedge: bool = True
    min_position_usd: float = 2.0
    poll_interval_seconds: float = 2.0
    starting_balance: float = 1000.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def update(self, raw: dict[str, Any]) -> "ArbConfig":
        known = {f.name for f in fields(self)}
        for key, value in raw.items():
            if key in known and value is not None:
                current = getattr(self, key)
                if isinstance(current, bool):
                    setattr(self, key, bool(value))
                elif isinstance(current, int) and not isinstance(current, bool):
                    setattr(self, key, int(value))
                elif isinstance(current, float):
                    setattr(self, key, float(value))
                else:
                    setattr(self, key, value)
        return self


def load_arb_config(path: str | Path | None = None) -> ArbConfig:
    cfg = ArbConfig()
    if path is None:
        return cfg
    path = Path(path)
    if not path.exists():
        return cfg
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
    except Exception:
        data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        return cfg
    block = data.get("arbitrage") if isinstance(data.get("arbitrage"), dict) else data
    cfg.update(block)
    for key in ("min_position_usd", "poll_interval_seconds", "starting_balance"):
        if key in data and data[key] is not None:
            setattr(cfg, key, type(getattr(cfg, key))(data[key]))
    return cfg
