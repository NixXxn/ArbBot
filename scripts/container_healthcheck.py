"""Container liveness from bounded state/journal files; never reads credentials."""
import os
from pathlib import Path
import time
import urllib.request

if os.environ.get("BOT_MODE", "dashboard") == "dashboard":
    try:
        port = os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8080"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            raise SystemExit(0 if response.status == 200 else 1)
    except Exception:
        raise SystemExit(1)

directory = Path(os.environ.get("BOT_DATA_DIR", "/data"))
candidates = [
    directory / "replica-paper-arbitrage.jsonl",
    directory / "replica-paper-portfolio.json",
    directory / "arb-pairs.json",
    directory / "replica-paper-console.log",
]
fresh = [path for path in candidates if path.exists() and time.time() - path.stat().st_mtime <= 180]
raise SystemExit(0 if fresh else 1)
