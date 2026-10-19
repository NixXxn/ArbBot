"""Settings for Polymarket ↔ Kalshi cross-venue arb."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class MappedPair:
    poly_slug: str = ""
    kalshi_ticker: str = ""
    # If True, Polymarket YES aligns with Kalshi YES (same outcome).
    aligned: bool = True
    note: str = ""


@dataclass
class CrossConfig:
    min_edge: float = 0.03
    fee_buffer: float = 0.02  # combined venue fee cushion
    max_pair_cost: float = 0.97
    position_usd: float = 25.0
    max_open_pairs: int = 4
    poll_interval_seconds: float = 5.0
    scan_limit_poly: int = 200
    scan_limit_kalshi: int = 400
    min_match_score: float = 0.78
    max_end_delta_hours: float = 72.0
    min_ask: float = 0.01
    max_ask: float = 0.99
    min_size: float = 5.0
    paper_mode: bool = True
    auto_match: bool = True
    mapped_pairs: list[MappedPair] = field(default_factory=list)
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["mapped_pairs"] = [asdict(p) for p in self.mapped_pairs]
        return raw

    def update(self, data: dict[str, Any]) -> "CrossConfig":
        known = {f.name for f in fields(self)}
        for key, value in data.items():
            if key == "mapped_pairs" and isinstance(value, list):
                pairs: list[MappedPair] = []
                for row in value:
                    if isinstance(row, dict) and row.get("poly_slug") and row.get("kalshi_ticker"):
                        pairs.append(
                            MappedPair(
                                poly_slug=str(row["poly_slug"]),
                                kalshi_ticker=str(row["kalshi_ticker"]),
                                aligned=bool(row.get("aligned", True)),
                                note=str(row.get("note") or ""),
                            )
                        )
                self.mapped_pairs = pairs
                continue
            if key not in known or value is None:
                continue
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


def _parse_simple_yaml(text: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(0, root)]
    pending_list_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            item_text = line[2:].strip()
            if isinstance(parent, list):
                if ":" in item_text and not item_text.startswith("{"):
                    key, _, rest = item_text.partition(":")
                    row = {key.strip(): _scalar(rest.strip())}
                    parent.append(row)
                    stack.append((indent + 2, row))
                else:
                    parent.append(_scalar(item_text))
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        value = rest.strip()
        if isinstance(parent, dict):
            if value == "":
                # Lookahead-ish: next list or map — default to list for mapped_pairs
                child: dict | list = [] if key == "mapped_pairs" else {}
                parent[key] = child
                stack.append((indent + 2, child))
            else:
                parent[key] = _scalar(value)
    return root


def _scalar(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value) if "." not in value else float(value)
    except ValueError:
        return value.strip("'\"")


def load_cross_config(path: str | Path | None = None) -> CrossConfig:
    cfg = CrossConfig()
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
    if isinstance(data, dict):
        block = data.get("cross_venue") if isinstance(data.get("cross_venue"), dict) else data
        if isinstance(block, dict):
            cfg.update(block)
    return cfg
