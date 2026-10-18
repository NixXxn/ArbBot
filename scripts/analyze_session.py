"""Describe recorded feed freshness and exploratory pairwise arrival lead/lag."""
import argparse
from collections import deque
import itertools
import json
from pathlib import Path

import numpy as np


def analyze(path):
    ticks = {}
    statuses = {}
    summary = None
    quote_count = 0
    for line in Path(path).read_text().splitlines():
        e = json.loads(line)
        if e.get("type") == "Tick":
            ticks.setdefault(e["venue"], []).append(e)
        elif e.get("type") == "Status":
            key = e["venue"] + ":" + e["state"]
            statuses[key] = statuses.get(key, 0) + 1
        elif e.get("type") == "Quote":
            quote_count += 1
        elif e.get("type") == "summary":
            summary = e
    report = {"statuses": statuses, "polymarket_quote_updates": quote_count,
              "summary": summary, "feeds": {}, "exploratory_arrival_lags": [],
              "warning": "A short local capture does not establish stable leadership or profitable execution. Exchange clocks are not synchronized."}
    for venue, samples in ticks.items():
        ages = [e["received_ms"] - e["exchange_ms"] for e in samples]
        report["feeds"][venue] = {"ticks": len(samples),
            "delivery_age_ms_p50": float(np.percentile(ages, 50)),
            "delivery_age_ms_p99": float(np.percentile(ages, 99)),
            "over_500ms": sum(a > 500 for a in ages),
            "span_seconds": (samples[-1]["mono_ms"] - samples[0]["mono_ms"]) / 1000}
    history = deque()
    raw_trigger_ticks = 0
    maximum = 0.0
    for tick in ticks.get("Kraken", []):
        ms = tick["mono_ms"]
        mid = (tick["bid"] + tick["ask"]) / 2
        while history and ms - history[0][0] > 5:
            history.popleft()
        if history:
            bp = abs((mid / history[0][1] - 1) * 10000)
            maximum = max(maximum, bp)
            raw_trigger_ticks += bp >= 1
        history.append((ms, mid))
    report["kraken_5ms_detector"] = {"raw_trigger_ticks": raw_trigger_ticks,
        "maximum_move_bp": maximum, "threshold_bp": 1.0}
    for a, b in itertools.combinations(sorted(ticks), 2):
        aa, bb = ticks[a], ticks[b]
        start = max(aa[0]["mono_ms"], bb[0]["mono_ms"])
        end = min(aa[-1]["mono_ms"], bb[-1]["mono_ms"])
        if end - start < 10000:
            continue
        grid = np.arange(start, end + 1)
        series = []
        masks = []
        for data in (aa, bb):
            ts = np.array([e["mono_ms"] for e in data])
            mids = np.array([(e["bid"] + e["ask"]) / 2 for e in data])
            indices = np.searchsorted(ts, grid, side="right") - 1
            values = mids[indices]
            fresh = grid - ts[indices] <= 500
            series.append(values[50:] / values[:-50] - 1)
            masks.append(fresh[50:] & fresh[:-50])
        results = []
        # Positive lag: B's comparable move reaches this machine after A.
        for lag in range(-200, 201):
            if lag > 0:
                x, y = series[0][:-lag], series[1][lag:]
                keep = masks[0][:-lag] & masks[1][lag:]
            elif lag < 0:
                x, y = series[0][-lag:], series[1][:lag]
                keep = masks[0][-lag:] & masks[1][:lag]
            else:
                x, y = series
                keep = masks[0] & masks[1]
            x, y = x[keep], y[keep]
            if len(x) < 10000 or x.std() == 0 or y.std() == 0:
                continue
            results.append((float(np.corrcoef(x, y)[0, 1]), lag))
        if results:
            correlation, lag = max(results)
            report["exploratory_arrival_lags"].append({"a": a, "b": b,
                "b_follows_a_ms": lag, "peak_correlation": correlation,
                "stable_lead_established": False})
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("journal")
    p.add_argument("--report", default="session-analysis.json")
    args = p.parse_args()
    result = analyze(args.journal)
    Path(args.report).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
