"""Durable pair / unpaired-leg state machine."""
from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

PairStatus = Literal[
    "pending_first",
    "awaiting_hedge",
    "paired",
    "escalating",
    "closed",
]


@dataclass
class LegFill:
    outcome: str
    token: str
    shares: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    notional: float = 0.0
    order_id: str | None = None


@dataclass
class PairRecord:
    pair_id: str
    condition_id: str
    slug: str
    status: PairStatus
    first: LegFill
    second: LegFill
    target_shares: float
    max_second_price: float | None = None
    expected_net: float | None = None
    realized_net: float | None = None
    locked_profit: float | None = None
    opened_at_ms: int = 0
    first_filled_at_ms: int | None = None
    paired_at_ms: int | None = None
    closed_at_ms: int | None = None
    hedge_order_id: str | None = None
    escalation_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def unpaired_age_seconds(self, now_ms: int | None = None) -> float | None:
        if self.status not in ("awaiting_hedge", "escalating") or not self.first_filled_at_ms:
            return None
        now = now_ms or int(time.time() * 1000)
        return max(0.0, (now - self.first_filled_at_ms) / 1000.0)

    def unpaired_shares(self) -> float:
        return max(0.0, self.first.shares - self.second.shares)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PairRecord":
        first = LegFill(**{k: raw["first"][k] for k in LegFill.__dataclass_fields__ if k in raw.get("first", {})})
        second = LegFill(**{k: raw["second"][k] for k in LegFill.__dataclass_fields__ if k in raw.get("second", {})})
        return cls(
            pair_id=str(raw["pair_id"]),
            condition_id=str(raw.get("condition_id") or ""),
            slug=str(raw.get("slug") or ""),
            status=raw.get("status") or "closed",  # type: ignore[arg-type]
            first=first,
            second=second,
            target_shares=float(raw.get("target_shares") or 0),
            max_second_price=(None if raw.get("max_second_price") is None else float(raw["max_second_price"])),
            expected_net=(None if raw.get("expected_net") is None else float(raw["expected_net"])),
            realized_net=(None if raw.get("realized_net") is None else float(raw["realized_net"])),
            locked_profit=(None if raw.get("locked_profit") is None else float(raw["locked_profit"])),
            opened_at_ms=int(raw.get("opened_at_ms") or 0),
            first_filled_at_ms=(None if raw.get("first_filled_at_ms") is None else int(raw["first_filled_at_ms"])),
            paired_at_ms=(None if raw.get("paired_at_ms") is None else int(raw["paired_at_ms"])),
            closed_at_ms=(None if raw.get("closed_at_ms") is None else int(raw["closed_at_ms"])),
            hedge_order_id=raw.get("hedge_order_id"),
            escalation_reason=raw.get("escalation_reason"),
            events=list(raw.get("events") or []),
        )


class PairStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pairs: dict[str, PairRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._pairs = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._pairs = {}
            return
        rows = data.get("pairs") if isinstance(data, dict) else data
        self._pairs = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("pair_id"):
                    rec = PairRecord.from_dict(row)
                    self._pairs[rec.pair_id] = rec

    def save(self) -> None:
        payload = {"updated_at_ms": int(time.time() * 1000),
                   "pairs": [p.to_dict() for p in self._pairs.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temporary.replace(self.path)

    def all(self) -> list[PairRecord]:
        return list(self._pairs.values())

    def open_pairs(self) -> list[PairRecord]:
        return [p for p in self._pairs.values() if p.status not in ("closed",)]

    def active_slugs(self) -> set[str]:
        return {p.slug for p in self.open_pairs()}

    def get(self, pair_id: str) -> PairRecord | None:
        return self._pairs.get(pair_id)

    def upsert(self, pair: PairRecord) -> None:
        self._pairs[pair.pair_id] = pair
        self.save()

    def log_event(self, pair: PairRecord, kind: str, **extra: Any) -> None:
        pair.events.append({"at_ms": int(time.time() * 1000), "kind": kind, **extra})
        pair.events = pair.events[-50:]
        self.upsert(pair)

    def new_pair(
        self,
        *,
        condition_id: str,
        slug: str,
        first: LegFill,
        second: LegFill,
        target_shares: float,
        expected_net: float | None,
    ) -> PairRecord:
        pair = PairRecord(
            pair_id=str(uuid.uuid4()),
            condition_id=condition_id,
            slug=slug,
            status="pending_first",
            first=first,
            second=second,
            target_shares=target_shares,
            expected_net=expected_net,
            opened_at_ms=int(time.time() * 1000),
        )
        self.upsert(pair)
        return pair
