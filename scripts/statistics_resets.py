"""Non-destructive dashboard counter baselines."""
import json
from pathlib import Path

STRATEGIES = ("kraken", "up60", "preopen", "clip5", "clip15", "early3m", "early3m5")


def load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: int(value.get(key, 0)) for key in STRATEGIES
            if isinstance(value.get(key), (int, float)) and value.get(key) >= 0}


def visible(row, baselines):
    if row.get("status") == "open":
        return True
    reset_at = baselines.get(row.get("strategy"), 0)
    if not reset_at:
        return True
    try:
        settled_at = int(row.get("settled_at_ms") or 0)
    except (TypeError, ValueError):
        settled_at = 0
    return settled_at > reset_at
