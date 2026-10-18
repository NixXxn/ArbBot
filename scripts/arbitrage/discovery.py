"""Discover binary YES/NO or Up/Down markets via Gamma."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from .config import ArbConfig

log = logging.getLogger("arbot.discovery")

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

_PREFERRED_MARKERS = (
    "bitcoin", "btc-", "btc ", "ethereum", "eth-", "solana", "sol-",
    "xrp", "doge", "crypto", "updown", "up-down",
    "highest-temperature", "lowest-temperature", "temperature-in-",
)
_SKIP_MARKERS = (
    "player-props", "more-markets", "-spread-", "-total-", "-handicap-",
    "-o-u-", "first-blood", "correct-score",
)


@dataclass(frozen=True)
class ArbMarket:
    condition_id: str
    slug: str
    question: str
    outcome_a: str
    outcome_b: str
    token_a: str
    token_b: str
    liquidity: float
    volume_24h: float
    preferred: bool
    end_ms: int | None = None


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _binary_outcomes(market: dict[str, Any]) -> tuple[str, str] | None:
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    lowered = {o.lower(): o for o in outcomes}
    if "yes" in lowered and "no" in lowered:
        return lowered["yes"], lowered["no"]
    if "up" in lowered and "down" in lowered:
        return lowered["up"], lowered["down"]
    return None


def _tokens_for(market: dict[str, Any], outcome_a: str, outcome_b: str) -> tuple[str, str] | None:
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    tokens = [str(t) for t in _parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(tokens) or len(tokens) < 2:
        return None
    index = {o.lower(): i for i, o in enumerate(outcomes)}
    try:
        return tokens[index[outcome_a.lower()]], tokens[index[outcome_b.lower()]]
    except (KeyError, IndexError):
        return None


def discover_markets(cfg: ArbConfig, *, session: requests.Session | None = None) -> list[ArbMarket]:
    sess = session or requests.Session()
    results: list[ArbMarket] = []
    offset = 0
    page = 100
    while len(results) < cfg.scan_limit and offset < cfg.scan_limit * 2:
        try:
            response = sess.get(
                GAMMA_MARKETS,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": page,
                    "offset": offset,
                },
                timeout=20,
                headers={"User-Agent": "Arbot/1.0"},
            )
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            log.warning("gamma scan failed offset=%s: %s", offset, exc)
            break
        if not isinstance(rows, list) or not rows:
            break
        for market in rows:
            if not isinstance(market, dict):
                continue
            if market.get("closed") or not market.get("active", True):
                continue
            slug = str(market.get("slug") or "")
            question = str(market.get("question") or "")
            blob = f"{slug} {question}".lower()
            if any(m in blob for m in _SKIP_MARKERS):
                continue
            outcomes = _binary_outcomes(market)
            if outcomes is None:
                continue
            tokens = _tokens_for(market, outcomes[0], outcomes[1])
            if tokens is None:
                continue
            try:
                liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)
            except (TypeError, ValueError):
                liquidity = 0.0
            try:
                volume = float(market.get("volume24hr") or market.get("volume_24hr") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if liquidity < cfg.min_liquidity and volume < cfg.min_volume_24h:
                continue
            preferred = any(m in blob for m in _PREFERRED_MARKERS)
            end_ms = None
            for key in ("endDate", "end_date_iso", "endDateIso"):
                raw = market.get(key)
                if not raw:
                    continue
                try:
                    # Gamma often returns ISO; leave None if unparsable.
                    end_ms = None
                except Exception:
                    end_ms = None
            results.append(
                ArbMarket(
                    condition_id=str(market.get("conditionId") or market.get("condition_id") or slug),
                    slug=slug,
                    question=question,
                    outcome_a=outcomes[0],
                    outcome_b=outcomes[1],
                    token_a=tokens[0],
                    token_b=tokens[1],
                    liquidity=liquidity,
                    volume_24h=volume,
                    preferred=preferred,
                    end_ms=end_ms,
                )
            )
            if len(results) >= cfg.scan_limit:
                break
        if len(rows) < page:
            break
        offset += page

    results.sort(
        key=lambda m: (
            0 if m.preferred else 1,
            -m.liquidity,
            -m.volume_24h,
        )
    )
    if cfg.prefer_crypto_weather:
        preferred = [m for m in results if m.preferred]
        other = [m for m in results if not m.preferred]
        results = preferred + other
    return results[: cfg.scan_limit]
