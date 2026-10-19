#!/usr/bin/env python3
"""Run Polymarket pair-arb and cross-venue scanners in one paper process."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_arb_paper
import run_cross_paper

log = logging.getLogger("arbot.combined")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    errors: list[BaseException] = []

    def run(target, name: str):
        try:
            code = target(argv)
            if code:
                errors.append(RuntimeError(f"{name} exited {code}"))
        except BaseException as exc:  # noqa: BLE001 — surface worker crash
            log.exception("%s crashed", name)
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(run_arb_paper.main, "arb"), daemon=True),
        threading.Thread(target=run, args=(run_cross_paper.main, "cross"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
