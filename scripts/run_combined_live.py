#!/usr/bin/env python3
"""Run Polymarket pair-arb live and cross-venue live in one process."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_arb_live
import run_cross_live

log = logging.getLogger("arbot.combined.live")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = Path(argv[0] if argv else "data")
    env_path = argv[2] if len(argv) >= 3 else ".env"
    seconds = argv[3] if len(argv) >= 4 else "31536000"
    errors: list[BaseException] = []

    def run(target, name: str, state_name: str):
        worker_argv = [str(data_dir), str(data_dir / state_name), str(env_path), str(seconds)]
        try:
            code = target(worker_argv)
            if code:
                errors.append(RuntimeError(f"{name} exited {code}"))
        except BaseException as exc:  # noqa: BLE001
            log.exception("%s crashed", name)
            errors.append(exc)

    threads = [
        threading.Thread(
            target=run, args=(run_arb_live.main, "arb_live", "replica-live-arb-state.json"), daemon=True
        ),
        threading.Thread(
            target=run, args=(run_cross_live.main, "cross_live", "replica-live-cross-state.json"), daemon=True
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
