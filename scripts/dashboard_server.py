"""Low-overhead dashboard and safe worker supervisor for the container deployment."""
from __future__ import annotations

import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
import zipfile


DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", "/data")).resolve()
SETTINGS_FILE = DATA_DIR / "dashboard-settings.json"
STOP_FILE = DATA_DIR / "live-stop-requested"
STATS_RESET_FILE = DATA_DIR / "replica-stat-resets.json"
ALLOWED_SUFFIXES = (".json", ".jsonl", ".log", ".1", ".2")
ALLOWED_PREFIXES = ("replica-", "manual-preflight-", "arb-")
PROVEN_PRE_SUBMISSION_FAILURES = {
    "Budget must be finite and at least $1.25",
    "Price or spread exceeded the entry guard",
    "Budget cannot cover the minimum share quantity",
    "Insufficient top-of-book depth for the test",
    "Signed order exceeded the notional limit",
    "Market expiry is too close",
    "Signal became stale before order submission",
}


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def live_state_files() -> list[Path]:
    return sorted(DATA_DIR.glob("replica-live-*.json"),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def latest_live_state() -> tuple[Path | None, dict | None]:
    for path in live_state_files():
        value = read_json(path)
        if value is not None:
            return path, value
    return None, None


def unresolved_live_states() -> list[Path]:
    active = {"buy_intent", "exit_intent", "holding", "redemption_intent"}
    result = []
    for path in live_state_files():
        state = read_json(path) or {}
        entries = state.get("entries", [])
        unresolved = []
        for entry in entries:
            if entry.get("stage") not in active:
                continue
            proven_unsubmitted = (
                entry.get("stage") == "buy_intent"
                and state.get("stage") == "requires_attention"
                and state.get("reason") in PROVEN_PRE_SUBMISSION_FAILURES
                and not any(key in entry for key in ("buy", "buy_hashes", "buy_cost"))
            )
            if not proven_unsubmitted:
                unresolved.append(entry)
        if state.get("stage") in {"holding", "exit_intent", "redemption_intent"} or unresolved:
            result.append(path)
    return result


def unsafe_live_state() -> tuple[Path, str] | None:
    states = unresolved_live_states()
    return ((states[0], "Previous live state contains an unresolved order or position")
            if states else None)


def safe_files() -> list[Path]:
    files = []
    for path in DATA_DIR.iterdir():
        if (path.is_file() and path.name.startswith(ALLOWED_PREFIXES)
                and path.name.endswith(ALLOWED_SUFFIXES)):
            files.append(path)
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def tail_text(path: Path, byte_limit: int = 128 * 1024, line_limit: int = 250) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - byte_limit))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if size > byte_limit:
        text = text.split("\n", 1)[-1]
    return "\n".join(text.splitlines()[-line_limit:])


def recent_events(path: Path) -> tuple[dict | None, dict | None]:
    health = fill = None
    try:
        for line in reversed(tail_text(path, line_limit=500).splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if health is None and event.get("type") == "health":
                health = event
            if fill is None and event.get("type") == "paper_fill":
                fill = event
            if health is not None and fill is not None:
                break
    except OSError:
        pass
    return health, fill


def normalize_strategy(value):
    if value in ("arbitrage", "arb", "all", "combined", None, ""):
        return "arbitrage"
    raise ValueError("Strategy must be arbitrage (only active strategy)")


def active_feeds(strategy):
    return ("arbitrage",)


class Supervisor:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        settings = read_json(SETTINGS_FILE) or {}
        initial = settings.get("desired_mode", os.environ.get("BOT_INITIAL_MODE", "paper"))
        self.desired_mode = initial if initial in ("paper", "live") else "paper"
        try:
            budget = float(settings.get("paper_budget", os.environ.get("PAPER_BUDGET", "1000")))
        except (TypeError, ValueError):
            budget = 1000.0
        self.paper_budget = budget if math.isfinite(budget) and 16 <= budget <= 1_000_000 else 1000.0
        strategy = settings.get("strategy", os.environ.get("BOT_STRATEGY", "arbitrage"))
        try:
            self.strategy = normalize_strategy(strategy)
        except ValueError:
            self.strategy = "arbitrage"
        self.active_strategy = self.strategy
        sizing = settings.get("live_sizing", os.environ.get("LIVE_SIZING", "minimum"))
        self.live_sizing = sizing if sizing in ("minimum", "adaptive") else "minimum"
        self.active_live_sizing = self.live_sizing
        self.live_state_path: Path | None = None
        self.recovery_state_paths: list[Path] = []
        self.current_mode: str | None = None
        self.process: subprocess.Popen | None = None
        self.transition = False
        self.last_exit: dict | None = None
        self.restart_attempts = 0
        self.worker_started_at = 0.0
        self.last_restart: dict | None = None
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.suppress_watch_restart = False
        self._persist()
        self._start(self.desired_mode)
        threading.Thread(target=self._watch, name="worker-watch", daemon=True).start()

    def _persist(self) -> None:
        atomic_json(SETTINGS_FILE, {"desired_mode": self.desired_mode,
                                    "paper_budget": self.paper_budget,
                                    "strategy": self.strategy,
                                    "live_sizing": self.live_sizing})

    def _command(self, mode: str) -> list[str]:
        app_root = Path(os.environ.get("BOT_APP_ROOT", "/app"))
        scripts = app_root / "scripts"
        # Local/dev: fall back to this file's directory when /app is absent.
        if not scripts.exists():
            scripts = Path(__file__).resolve().parent
            app_root = scripts.parent
        if mode == "paper":
            return [
                sys.executable, "-u", str(scripts / "bounded_console.py"),
                str(DATA_DIR / "replica-paper-console.log"),
                sys.executable, "-u", str(scripts / "run_arb_paper.py"),
                str(DATA_DIR),
                str(app_root / "leadlag"),
                str(app_root),
                "31536000",
                "arbitrage",
                str(self.paper_budget),
                str(STATS_RESET_FILE),
            ]
        # Live path retained for recovery of prior states only; LIVE_KILL defaults to on.
        # New directional strategies are removed — live arb uses the same paper runner
        # until an explicit live arb executor is enabled by the operator.
        command = [
            sys.executable, "-u", str(scripts / "bounded_console.py"),
            str(DATA_DIR / "replica-console.log"),
            sys.executable, "-u", str(scripts / "run_arb_paper.py"),
            str(DATA_DIR),
            str(app_root / "leadlag"),
            str(app_root),
            "31536000",
            "arbitrage",
            str(self.paper_budget),
            str(STATS_RESET_FILE),
        ]
        return command

    def _start(self, mode: str) -> None:
        if mode == "live" and os.environ.get("LIVE_KILL", "1") != "0":
            self.last_exit = {"mode": "live", "error": "LIVE_KILL must be 0", "at": int(time.time())}
            self.current_mode = None
            self.transition = False
            return
        if mode == "live":
            self.recovery_state_paths = unresolved_live_states()
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            self.live_state_path = DATA_DIR / f"replica-live-{stamp}-{time.time_ns()%1_000_000:06d}.json"
        STOP_FILE.unlink(missing_ok=True)
        self.process = subprocess.Popen(self._command(mode), start_new_session=True)
        self.worker_started_at = time.monotonic()
        self.current_mode = mode
        self.active_strategy = self.strategy
        self.active_live_sizing = getattr(self, "live_sizing", "minimum")
        self.transition = False
        self.last_exit = None

    def _stop_process(self, process: subprocess.Popen | None) -> None:
        """Stop a worker process group without holding the supervisor lock."""
        if not process or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            process.wait(timeout=10)

    def _stop_paper(self) -> None:
        self._stop_process(self.process)

    def _reset_all_statistics(self) -> None:
        baselines = read_json(STATS_RESET_FILE) or {}
        now = int(time.time() * 1000)
        for strategy in ("arbitrage",):
            baselines[strategy] = now
        atomic_json(STATS_RESET_FILE, baselines)

    def _apply_budget_to_portfolio(self, budget: float) -> None:
        """Update durable paper capital immediately so Apply budget is visible before restart."""
        path = DATA_DIR / "replica-paper-portfolio.json"
        state = read_json(path)
        if state is None:
            return
        rate = float(state.get("exposure_rate") or 0.30)
        open_exposure = float(state.get("open_exposure") or 0)
        state["budget"] = budget
        # Stats baselines were just reset, so settled PnL no longer reduces equity.
        state["equity"] = budget
        state["max_exposure"] = max(0.0, budget * rate)
        state["available"] = max(0.0, budget - open_exposure)
        state["updated_at_ms"] = int(time.time() * 1000)
        atomic_json(path, state)

    def _restart_paper_unlocked(self) -> None:
        """Stop any running non-live worker and start paper. Releases the lock while waiting."""
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            if running and self.current_mode == "live":
                STOP_FILE.touch()
                self.transition = True
                return
            process = self.process if running else None
            self.suppress_watch_restart = True
        try:
            if process is not None:
                self._stop_process(process)
            with self.lock:
                if self.process is not None:
                    self._record_exit()
                if self.desired_mode == "paper" and self.process is None:
                    self.transition = False
                    self.restart_attempts = 0
                    self._start("paper")
        finally:
            with self.lock:
                self.suppress_watch_restart = False

    def switch(self, mode: str, confirmation: str = "") -> dict:
        if mode not in ("paper", "live"):
            raise ValueError("Mode must be paper or live")
        if mode == "live":
            if confirmation != "LIVE":
                raise ValueError("Type LIVE to confirm funded execution")
            if os.environ.get("LIVE_KILL", "1") != "0":
                raise ValueError("Set LIVE_KILL=0 in Coolify before enabling live mode")
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            if mode == self.desired_mode and running:
                return self.status()
            previous_mode = self.current_mode
            self.desired_mode = mode
            self._persist()
            if running and self.current_mode == "live":
                STOP_FILE.touch()
                self.transition = True
            else:
                if running:
                    self._stop_paper()
                self._record_exit()
                self._start(mode)
                # Never leave the operator without a worker after a failed live start.
                if mode == "live" and self.current_mode != "live" and previous_mode == "paper":
                    self.desired_mode = "paper"
                    self._persist()
                    self._start("paper")
                    raise ValueError(self.last_exit.get("error") if self.last_exit else "Live start failed")
            return self.status()

    def _record_exit(self) -> None:
        if self.process and self.process.poll() is not None:
            exit_record = {
                "mode": self.current_mode,
                "code": self.process.returncode,
                "at": int(time.time()),
            }
            if self.current_mode == "live" and self.live_state_path:
                live_state = read_json(self.live_state_path) or {}
                if live_state.get("stage") == "requires_attention":
                    exit_record["error"] = (live_state.get("reason") or
                                            live_state.get("error_type") or
                                            "Live worker requires attention")
                    exit_record["state"] = self.live_state_path.name
                elif self.process.returncode:
                    exit_record["error"] = ("Live worker exited before monitoring; "
                                            "inspect replica-console.log")
                    exit_record["state"] = self.live_state_path.name
            self.last_exit = exit_record
        self.process = None
        self.current_mode = None

    def set_paper_budget(self, value) -> dict:
        try:
            budget = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Paper budget must be numeric") from error
        if not math.isfinite(budget) or not 16 <= budget <= 1_000_000:
            raise ValueError("Paper budget must be between $16 and $1,000,000")
        budget = round(budget, 2)
        with self.lock:
            self.paper_budget = budget
            self.desired_mode = "paper"
            self._persist()
            self._reset_all_statistics()
            self._apply_budget_to_portfolio(budget)
        # Stop/restart outside the budget-write critical section so status polls stay responsive
        # and reverse proxies do not time out while the old paper worker drains.
        self._restart_paper_unlocked()
        return self.status()

    def set_strategy(self, value) -> dict:
        value = normalize_strategy(value)
        with self.lock:
            if value == self.strategy:
                return self.status()
            self.strategy = value
            self._persist()
            running = bool(self.process and self.process.poll() is None)
            if self.current_mode == "live" and running:
                STOP_FILE.touch()
                self.transition = True
            elif self.current_mode == "paper" and running:
                self._stop_paper();self._record_exit();self._start("paper")
            return self.status()

    def set_live_sizing(self, value) -> dict:
        if value not in ("minimum", "adaptive"):
            raise ValueError("Live sizing must be minimum or adaptive")
        with self.lock:
            if value == self.live_sizing:
                return self.status()
            self.live_sizing = value
            self._persist()
            if self.current_mode == "live" and self.process and self.process.poll() is None:
                STOP_FILE.touch()
                self.transition = True
            return self.status()

    def reset_statistics(self, value) -> dict:
        if value not in ("arbitrage", "all"):
            raise ValueError("Statistics reset must target arbitrage or all")
        with self.lock:
            baselines = read_json(STATS_RESET_FILE) or {}
            now = int(time.time() * 1000)
            baselines["arbitrage"] = now
            atomic_json(STATS_RESET_FILE, baselines)
            return {"ok": True, "strategy": value, "reset_at_ms": now}

    def set_arb_settings(self, payload: dict) -> dict:
        """Persist editable arb knobs; overlay wins over baked portfolio config."""
        allowed = {
            "max_pair_cost", "max_maker_ask_sum", "paper_taker_cap",
            "min_edge", "fee_buffer", "safety_margin",
            "position_usd", "max_position_usd", "max_open_pairs",
            "hedge_timeout_seconds", "max_unpaired_notional", "max_capital_locked",
            "min_pair_profit_pct", "pair_bid_sum_exit", "scan_limit",
            "min_ask", "max_ask", "min_ask_size", "hedge_depth_multiple",
            "max_concurrent_unpaired", "immediate_hedge",
        }
        overlay = {}
        for key in allowed:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, bool) or key in ("immediate_hedge",):
                overlay[key] = bool(value)
            else:
                try:
                    overlay[key] = float(value) if key not in (
                        "max_open_pairs", "scan_limit", "max_concurrent_unpaired",
                        "max_quote_age_ms",
                    ) else int(float(value))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"Invalid {key}") from error
        path = DATA_DIR / "arb-settings.json"
        current = read_json(path) or {}
        current.update(overlay)
        current["updated_at_ms"] = int(time.time() * 1000)
        atomic_json(path, current)
        # Patch portfolio immediately so the UI does not keep showing stale arb_config.
        portfolio_path = DATA_DIR / "replica-paper-portfolio.json"
        portfolio = read_json(portfolio_path)
        if portfolio is not None:
            merged = dict(portfolio.get("arb_config") or {})
            merged.update(current)
            portfolio["arb_config"] = merged
            portfolio["updated_at_ms"] = int(time.time() * 1000)
            atomic_json(portfolio_path, portfolio)
        with self.lock:
            self._persist()
        if self.desired_mode == "paper":
            self._restart_paper_unlocked()
        return self.status()

    def _watch(self) -> None:
        while not self.closed.wait(1):
            restart_mode = None
            restart_delay = 0
            with self.lock:
                if self.process and self.process.poll() is not None:
                    previous = self.current_mode
                    runtime = time.monotonic()-getattr(self,"worker_started_at",time.monotonic())
                    intentional = self.transition
                    suppress = getattr(self, "suppress_watch_restart", False)
                    self._record_exit()
                    if previous in ("live","paper") and not suppress:
                        restart_mode=self.desired_mode
                        if intentional:
                            self.restart_attempts=0
                        else:
                            if runtime >= 300:self.restart_attempts=0
                            self.restart_attempts=getattr(self,"restart_attempts",0)+1
                            restart_delay=min(300,2**min(self.restart_attempts,8))
                            self.last_restart={"mode":restart_mode,"attempt":self.restart_attempts,
                                               "delay_seconds":restart_delay,"at":int(time.time())}
            if restart_mode is not None:
                if restart_delay and self.closed.wait(restart_delay):return
                with self.lock:
                    if self.process is None and self.desired_mode==restart_mode:
                        self._start(restart_mode)

    def status(self) -> dict:
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            result = {
                "desired_mode": self.desired_mode,
                "current_mode": self.current_mode,
                "transition": self.transition,
                "worker_running": running,
                "worker_pid": self.process.pid if running else None,
                "last_exit": self.last_exit,
                "last_restart": getattr(self,"last_restart",None),
                "restart_attempts": getattr(self,"restart_attempts",0),
                "live_enabled": os.environ.get("LIVE_KILL", "1") == "0",
                "paper_budget": self.paper_budget,
                "strategy": self.strategy,
                "active_strategy": getattr(self, "active_strategy", self.strategy),
                "live_sizing": getattr(self, "live_sizing", "minimum"),
                "active_live_sizing": getattr(self, "active_live_sizing",
                                               getattr(self, "live_sizing", "minimum")),
                "recovery_states": [path.name for path in
                                    getattr(self, "recovery_state_paths", [])],
                "feeds": list(active_feeds(self.strategy)),
            }
        live_state_path = getattr(self, "live_state_path", None)
        live_state = read_json(live_state_path) if live_state_path else None
        if live_state is None:
            live_state_path, live_state = latest_live_state()
        if live_state:
            result["live_state"] = live_state
            result["live_state_file"] = live_state_path.name
        # Show paper portfolio whenever paper is desired, including the brief restart window.
        if result["current_mode"] == "paper" or result["desired_mode"] == "paper":
            health_by_asset, fills = {}, []
            labels = active_feeds(self.strategy)
            portfolio = read_json(DATA_DIR / "replica-paper-portfolio.json") or {}
            health_by_asset.update(portfolio.get("engine_health", {}))
            for asset in labels:
                journal = DATA_DIR / f"replica-paper-{asset}.jsonl"
                health, fill = recent_events(journal)
                if health:health_by_asset[asset] = health
                if fill:fills.append((journal.stat().st_mtime,fill))
            portfolio_journal = DATA_DIR / "replica-paper-portfolio.jsonl"
            if portfolio_journal.exists():
                _, fill = recent_events(portfolio_journal)
                if fill:fills.append((portfolio_journal.stat().st_mtime,fill))
            aggregate = {"type":"health", "events":{}, "signals":0,
                         "paper_fills":0, "paper_spend":0}
            for health in health_by_asset.values():
                for name,count in health.get("events",{}).items():
                    aggregate["events"][name]=aggregate["events"].get(name,0)+count
                for key in ("signals","paper_fills","paper_spend"):
                    aggregate[key]+=health.get(key,0)
            result["engine_health_by_asset"] = health_by_asset
            result["engine_health"] = aggregate
            result["latest_paper_fill"] = max(fills,key=lambda row:row[0])[1] if fills else None
            result["strategy_stats"] = portfolio.get("strategy_stats", {})
            result["portfolio"] = {key:portfolio.get(key) for key in
                                   ("budget","equity","open_exposure","max_exposure","exposure_rate")}
            result["risk_controls"] = portfolio.get("risk_controls", {})
            result["pairs"] = portfolio.get("pairs", [])
            result["pnl_breakdown"] = portfolio.get("pnl_breakdown", {})
            # Overlay file always wins so Apply settings is visible immediately.
            baked = dict(portfolio.get("arb_config") or {})
            overlay = read_json(DATA_DIR / "arb-settings.json") or {}
            baked.update({k: v for k, v in overlay.items() if k != "updated_at_ms"})
            result["arb_config"] = baked
            result["recent_decisions"] = portfolio.get("recent_decisions", [])
            # Prefer arb journal for health
            arb_journal = DATA_DIR / "replica-paper-arbitrage.jsonl"
            if arb_journal.exists():
                health, fill = recent_events(arb_journal)
                if health:
                    result["engine_health_by_asset"] = {"arbitrage": health}
                if fill:
                    result["latest_paper_fill"] = fill
        else:
            durable = read_json(DATA_DIR / "replica-portfolio-live.json") or {}
            result["engine_health"] = (live_state or {}).get("engine_health")
            result["engine_health_by_asset"] = (live_state or {}).get("engine_health_by_asset",{})
            result["latest_paper_fill"] = None
            result["strategy_stats"] = ((live_state or {}).get("strategy_stats")
                                        or durable.get("strategy_stats", {}))
            result["portfolio"] = ((live_state or {}).get("portfolio")
                                   or durable.get("portfolio", {}))
            result["risk_controls"] = ((live_state or {}).get("risk_controls")
                                       or durable.get("risk_controls", {}))
        result["files"] = [
            {"name": path.name, "size": path.stat().st_size, "modified": int(path.stat().st_mtime)}
            for path in safe_files()
        ]
        return result

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                if self.current_mode == "live":
                    STOP_FILE.touch()
                else:
                    self._stop_paper()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ArbotDashboard/1"

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = base64.b64encode(f"{self.server.dashboard_user}:{self.server.dashboard_token}".encode()).decode()
        return header.startswith("Basic ") and hmac.compare_digest(header[6:], expected)

    def _headers(self, status: int, content_type: str, length: int | None = None,
                 filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if filename is not None:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Arbot", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"ok": True})
            return
        if not self._require_auth():
            return
        if parsed.path == "/":
            body = (Path(__file__).with_name("dashboard.html")).read_bytes()
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            self._json(200, self.server.supervisor.status())
        elif parsed.path == "/api/log":
            name = parse_qs(parsed.query).get("name", [""])[0]
            allowed = {path.name: path for path in safe_files()}
            if name not in allowed:
                self._json(404, {"error": "Log not found"})
                return
            text = tail_text(allowed[name])
            self._json(200, {"name": name, "text": text})
        elif parsed.path == "/api/download":
            name = parse_qs(parsed.query).get("name", [""])[0]
            allowed = {path.name: path for path in safe_files()}
            if name not in allowed:
                self._json(404, {"error": "Log not found"})
                return
            body = allowed[name].read_bytes()
            self._headers(200, "application/octet-stream", len(body), name)
            self.wfile.write(body)
        elif parsed.path == "/api/export.zip":
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in safe_files():
                    archive.write(path, arcname=path.name)
            body = output.getvalue()
            self._headers(200, "application/zip", len(body), "arbot-logs.zip")
            self.wfile.write(body)
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        endpoint = urlparse(self.path).path
        if endpoint not in ("/api/mode", "/api/paper-budget", "/api/strategy",
                            "/api/live-sizing", "/api/reset-statistics", "/api/arb-settings"):
            self._json(404, {"error": "Not found"})
            return
        content_type = (self.headers.get_content_type() or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 8192))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            if endpoint == "/api/mode":
                confirmation = payload.get("confirm", payload.get("confirmation", ""))
                result = self.server.supervisor.switch(payload.get("mode", ""), confirmation)
            elif endpoint == "/api/paper-budget":
                result = self.server.supervisor.set_paper_budget(payload.get("budget"))
            elif endpoint == "/api/live-sizing":
                result = self.server.supervisor.set_live_sizing(payload.get("sizing"))
            elif endpoint == "/api/reset-statistics":
                result = self.server.supervisor.reset_statistics(payload.get("strategy") or "arbitrage")
            elif endpoint == "/api/arb-settings":
                result = self.server.supervisor.set_arb_settings(payload)
            else:
                result = self.server.supervisor.set_strategy(payload.get("strategy") or "arbitrage")
            self._json(200, result)
        except (ValueError, json.JSONDecodeError, TypeError, OSError) as error:
            self._json(400, {"error": str(error)})

    def log_message(self, format: str, *args) -> None:
        return


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, supervisor, user, token):
        super().__init__(address, handler)
        self.supervisor = supervisor
        self.dashboard_user = user
        self.dashboard_token = token


def main() -> None:
    token = os.environ.get("DASHBOARD_TOKEN", "")
    if len(token) < 16:
        raise SystemExit("DASHBOARD_TOKEN must contain at least 16 characters")
    supervisor = Supervisor()
    port = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8080")
    server = DashboardServer(
        ("0.0.0.0", port),
        DashboardHandler,
        supervisor,
        os.environ.get("DASHBOARD_USER", "admin"),
        token,
    )
    print(f"Arbot dashboard listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        supervisor.close()
        server.server_close()


if __name__ == "__main__":
    main()
