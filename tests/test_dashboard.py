import base64
import json
from http.client import HTTPConnection
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dashboard_server


class DashboardTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patches = [
            patch.object(dashboard_server, "DATA_DIR", self.root),
            patch.object(dashboard_server, "SETTINGS_FILE", self.root / "dashboard-settings.json"),
            patch.object(dashboard_server, "STOP_FILE", self.root / "live-stop-requested"),
            patch.object(dashboard_server, "STATS_RESET_FILE", self.root / "replica-stat-resets.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_exports_only_bounded_operational_files(self):
        (self.root / "replica-console.log").write_text("visible")
        (self.root / "replica-live-state.json").write_text("{}")
        (self.root / "unrelated.json").write_text("hidden")
        (self.root / ".env").write_text("SECRET=hidden")
        (self.root / "dashboard-settings.json").write_text("{}")
        self.assertEqual({path.name for path in dashboard_server.safe_files()},
                         {"replica-live-state.json", "replica-console.log"})

    def test_tail_is_capped_by_lines(self):
        path = self.root / "events.jsonl"
        path.write_text("\n".join(str(index) for index in range(300)))
        text = dashboard_server.tail_text(path, line_limit=2)
        self.assertEqual(text.splitlines(), ["298", "299"])

    def test_live_button_uses_confirmation_dialog_and_submits_live_token(self):
        html=(Path(dashboard_server.__file__).with_name("dashboard.html")).read_text()
        self.assertIn("Arbot", html)
        self.assertIn("window.confirm('Switch to LIVE?", html)
        self.assertIn("confirmation='LIVE'", html)
        self.assertIn("Locked pair profit", html)
        self.assertIn("Cross candidates", html)
        self.assertIn("Best cross spread", html)

    def test_mode_switch_requires_live_confirmation_and_kill_switch(self):
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "paper"
        supervisor.current_mode = None
        supervisor.process = None
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 30.0
        supervisor.strategy = "combined"
        supervisor.lock = threading.RLock()
        supervisor._persist = lambda: None
        supervisor._start = lambda mode: setattr(supervisor, "current_mode", mode)
        with self.assertRaisesRegex(ValueError, "Type LIVE"):
            supervisor.switch("live")
        with patch.dict("os.environ", {"LIVE_KILL": "1"}):
            with self.assertRaisesRegex(ValueError, "LIVE_KILL"):
                supervisor.switch("live", "LIVE")
        with patch.dict("os.environ", {"LIVE_KILL": "0"}):
            result = supervisor.switch("live", "LIVE")
        self.assertEqual(result["desired_mode"], "live")

    def test_paper_budget_is_bounded_and_persisted(self):
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "paper"
        supervisor.current_mode = None
        supervisor.process = None
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 30.0
        supervisor.strategy = "combined"
        supervisor.lock = threading.RLock()
        supervisor._persist = lambda: None
        started=[]
        supervisor._record_exit = lambda: None
        supervisor._start = lambda mode: started.append(mode) or setattr(supervisor,"current_mode",mode)
        supervisor.status = lambda: {"paper_budget": supervisor.paper_budget, "desired_mode": supervisor.desired_mode}
        result = supervisor.set_paper_budget("120")
        self.assertEqual(supervisor.paper_budget, 120.0)
        self.assertEqual(result["paper_budget"], 120.0)
        self.assertEqual(started, ["paper"])
        with self.assertRaisesRegex(ValueError, "between"):
            supervisor.set_paper_budget("10")

    def test_paper_budget_resets_capital_and_restarts_running_worker(self):
        portfolio = {
            "budget": 300.0, "equity": 116.0, "open_exposure": 0.0,
            "max_exposure": 34.8, "exposure_rate": 0.30, "trades": [],
        }
        dashboard_server.atomic_json(self.root / "replica-paper-portfolio.json", portfolio)
        process = SimpleNamespace(pid=4242, poll=lambda: None, wait=Mock())
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "paper"
        supervisor.current_mode = "paper"
        supervisor.process = process
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 300.0
        supervisor.strategy = "all"
        supervisor.lock = threading.RLock()
        supervisor._persist = lambda: None
        started = []
        stopped = []
        supervisor._stop_process = lambda proc: stopped.append(proc)
        supervisor._record_exit = lambda: (setattr(supervisor, "process", None),
                                           setattr(supervisor, "current_mode", None))
        supervisor._start = lambda mode: started.append(mode) or (
            setattr(supervisor, "current_mode", mode),
            setattr(supervisor, "process", SimpleNamespace(pid=99, poll=lambda: None)))
        supervisor.status = lambda: {
            "paper_budget": supervisor.paper_budget,
            "desired_mode": supervisor.desired_mode,
            "current_mode": supervisor.current_mode,
            "worker_running": bool(supervisor.process),
        }
        result = supervisor.set_paper_budget("500")
        saved = json.loads((self.root / "replica-paper-portfolio.json").read_text())
        resets = json.loads(dashboard_server.STATS_RESET_FILE.read_text())
        self.assertEqual(supervisor.paper_budget, 500.0)
        self.assertEqual(saved["budget"], 500.0)
        self.assertEqual(saved["equity"], 500.0)
        self.assertEqual(saved["max_exposure"], 150.0)
        self.assertEqual(stopped, [process])
        self.assertEqual(started, ["paper"])
        self.assertEqual(result["paper_budget"], 500.0)
        self.assertTrue(resets["arbitrage"] > 0)

    def test_strategy_aliases(self):
        self.assertEqual(dashboard_server.normalize_strategy("all"), "combined")
        self.assertEqual(dashboard_server.normalize_strategy("arb"), "arbitrage")
        self.assertEqual(dashboard_server.normalize_strategy("cross"), "cross_venue")
        self.assertEqual(dashboard_server.active_feeds("arbitrage"), ("arbitrage",))
        self.assertEqual(dashboard_server.active_feeds("combined"), ("arbitrage", "cross_venue"))
        with self.assertRaisesRegex(ValueError, "arbitrage"):
            dashboard_server.normalize_strategy("kraken")

    def test_arb_settings_overlay_wins_in_status(self):
        dashboard_server.atomic_json(
            self.root / "replica-paper-portfolio.json",
            {
                "strategy_stats": {"arbitrage": {"trades": 0}, "combined": {"trades": 0}},
                "budget": 100, "equity": 100, "open_exposure": 0, "max_exposure": 30,
                "exposure_rate": 0.3, "engine_health": {},
                "arb_config": {"max_pair_cost": 0.94, "min_edge": 0.025},
            },
        )
        dashboard_server.atomic_json(
            self.root / "arb-settings.json",
            {"max_pair_cost": 0.97, "min_edge": 0.01},
        )
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "paper"
        supervisor.current_mode = "paper"
        supervisor.process = None
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 100
        supervisor.strategy = "arbitrage"
        supervisor.lock = threading.RLock()
        result = supervisor.status()
        self.assertEqual(result["arb_config"]["max_pair_cost"], 0.97)
        self.assertEqual(result["arb_config"]["min_edge"], 0.01)

    def test_statistics_reset_targets_arbitrage(self):
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.lock = threading.RLock()
        dashboard_server.atomic_json(dashboard_server.STATS_RESET_FILE, {"arbitrage": 123})
        result = supervisor.reset_statistics("arbitrage")
        saved = json.loads(dashboard_server.STATS_RESET_FILE.read_text())
        self.assertGreater(saved["arbitrage"], 123)
        self.assertEqual(result["strategy"], "arbitrage")
        with self.assertRaisesRegex(ValueError, "arbitrage or all"):
            supervisor.reset_statistics("kraken")

    def test_live_sizing_changes_request_safe_restart(self):
        process = SimpleNamespace(pid=321, poll=lambda: None)
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "live"
        supervisor.current_mode = "live"
        supervisor.process = process
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 30.0
        supervisor.strategy = "arbitrage"
        supervisor.active_strategy = "arbitrage"
        supervisor.live_sizing = "minimum"
        supervisor.active_live_sizing = "minimum"
        supervisor.live_state_path = None
        supervisor.lock = threading.RLock()
        supervisor._persist = lambda: None
        result = supervisor.set_live_sizing("adaptive")
        self.assertTrue(result["transition"])
        self.assertEqual(result["live_sizing"], "adaptive")
        self.assertEqual(result["active_live_sizing"], "minimum")

    def test_live_transition_restarts_desired_live_worker(self):
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="live";supervisor.current_mode="live"
        supervisor.process=SimpleNamespace(returncode=0,poll=lambda:0)
        supervisor.transition=True;supervisor.last_exit=None
        supervisor.live_state_path=None;supervisor.lock=threading.RLock()
        supervisor.closed=SimpleNamespace(wait=Mock(side_effect=[False,True]))
        with patch.object(supervisor,"_start") as start:
            supervisor._watch()
        start.assert_called_once_with("live")

    def test_unexpected_worker_exit_restarts_with_backoff(self):
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="live";supervisor.current_mode="live"
        supervisor.process=SimpleNamespace(returncode=1,poll=lambda:1)
        supervisor.transition=False;supervisor.last_exit=None;supervisor.last_restart=None
        supervisor.restart_attempts=0;supervisor.worker_started_at=0
        supervisor.live_state_path=None;supervisor.lock=threading.RLock()
        supervisor.closed=SimpleNamespace(wait=Mock(side_effect=[False,False,True]))
        with patch.object(supervisor,"_start") as start:
            supervisor._watch()
        start.assert_called_once_with("live")
        self.assertEqual(supervisor.last_restart["attempt"],1)
        self.assertEqual(supervisor.last_restart["delay_seconds"],2)

    def test_live_portfolio_is_returned_to_dashboard(self):
        state=self.root/"replica-live-current.json"
        state.write_text(json.dumps({"stage":"monitor","portfolio":{
            "capital":100,"available":94,"open_exposure":6,"max_exposure":30}}))
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="live";supervisor.current_mode="live"
        supervisor.process=None;supervisor.transition=False;supervisor.last_exit=None
        supervisor.paper_budget=30;supervisor.strategy="combined"
        supervisor.live_sizing="minimum";supervisor.live_state_path=state
        supervisor.lock=threading.RLock()
        result=supervisor.status()
        self.assertEqual(result["portfolio"]["capital"],100)
        self.assertEqual(result["portfolio"]["equity"],100)
        self.assertEqual(result["portfolio"]["open_exposure"],6)
        self.assertEqual(result["live_execution"],"clob+cross")

    def test_live_mode_shows_wallet_portfolio(self):
        """Live CLOB worker writes replica-portfolio-live; capital must surface."""
        (self.root/"replica-portfolio-live.json").write_text(json.dumps({
            "capital":250.5,"equity":250.5,"open_exposure":40,"max_exposure":75.15,
            "strategy_stats":{"arbitrage":{"trades":3,"pnl":12.5}},
            "pairs":[{"id":"p1"}],"pnl_breakdown":{"locked_pair_profit":1.2}}))
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="live";supervisor.current_mode="live"
        supervisor.process=None;supervisor.transition=False;supervisor.last_exit=None
        supervisor.paper_budget=1000;supervisor.strategy="arbitrage"
        supervisor.live_sizing="minimum";supervisor.live_state_path=None
        supervisor.lock=threading.RLock()
        result=supervisor.status()
        self.assertEqual(result["portfolio"]["equity"],250.5)
        self.assertEqual(result["strategy_stats"]["arbitrage"]["trades"],3)
        self.assertEqual(result["pairs"][0]["id"],"p1")
        self.assertEqual(result["live_execution"],"clob")

    def test_durable_live_statistics_are_visible_between_workers(self):
        (self.root/"replica-portfolio-live.json").write_text(json.dumps({
            "strategy_stats":{"combined":{"trades":12,"settled":12,"pnl":4.5}},
            "portfolio":{"capital":104.5,"open_exposure":0,"max_exposure":31.35}}))
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="live";supervisor.current_mode=None
        supervisor.process=None;supervisor.transition=False;supervisor.last_exit=None
        supervisor.paper_budget=30;supervisor.strategy="combined"
        supervisor.live_sizing="minimum";supervisor.live_state_path=None
        supervisor.lock=threading.RLock()
        result=supervisor.status()
        self.assertEqual(result["strategy_stats"]["combined"]["trades"],12)
        self.assertEqual(result["portfolio"]["capital"],104.5)
        self.assertEqual(result["portfolio"]["equity"],104.5)

    def test_dashboard_capital_reads_equity_or_capital(self):
        html=(Path(dashboard_server.__file__).with_name("dashboard.html")).read_text()
        self.assertIn("portfolio.equity??portfolio.capital",html)
        self.assertIn("live_execution==='clob'",html)
        self.assertIn("crossCandidatesTable",html)
        self.assertIn("Best spread",html)
        self.assertIn("FOK taker only",html)

    def test_cross_venue_candidates_surface_in_status(self):
        (self.root/"replica-paper-cross-portfolio.json").write_text(json.dumps({
            "strategy":"cross_venue",
            "capital":1000,"equity":1000,
            "candidate_count":3,"eligible_count":1,"best_spread_pct":4.2,
            "candidates":[{
                "slug":"fed-cut","kalshi_ticker":"KXFED","direction":"poly_yes_kalshi_no",
                "poly_ask":0.4,"kalshi_ask":0.52,"cost":0.93,"edge":0.07,
                "spread_pct":7.0,"match_score":1.0,"eligible":True,"status":"eligible",
            }],
            "strategy_stats":{"cross_venue":{
                "trades":2,"open":0,"settled":2,"pnl":1.5,"roi":0.0015,
                "candidates":3,"eligible":1,"best_spread_pct":4.2,
            }},
            "pairs":[],"pnl_breakdown":{},"recent_decisions":[],
        }))
        supervisor=dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="paper";supervisor.current_mode="paper"
        supervisor.process=None;supervisor.transition=False;supervisor.last_exit=None
        supervisor.paper_budget=1000;supervisor.strategy="cross_venue"
        supervisor.live_sizing="minimum";supervisor.live_state_path=None
        supervisor.lock=threading.RLock()
        result=supervisor.status()
        self.assertEqual(result["candidate_count"],3)
        self.assertEqual(result["best_spread_pct"],4.2)
        self.assertEqual(result["candidates"][0]["spread_pct"],7.0)
        self.assertEqual(result["strategy_stats"]["cross_venue"]["candidates"],3)

    def test_completed_legacy_state_does_not_block_unique_live_session(self):
        legacy = self.root / "replica-live-state.json"
        legacy.write_text(json.dumps({"stage":"time_limit_reached","entries":[]}))
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.current_mode = None
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.strategy = "arbitrage"
        supervisor.paper_budget = 1000.0
        supervisor.live_state_path = None
        process = SimpleNamespace(pid=123, poll=lambda: None)
        with patch.dict("os.environ", {"LIVE_KILL": "0"}), \
             patch.object(dashboard_server.subprocess, "Popen", return_value=process):
            supervisor._start("live")
        self.assertEqual(supervisor.current_mode, "live")
        self.assertNotEqual(supervisor.live_state_path, legacy)
        self.assertTrue(supervisor.live_state_path.name.startswith("replica-live-"))
        command = supervisor._command("live")
        self.assertTrue(any("run_arb_live.py" in str(part) for part in command))
        self.assertTrue(any(str(supervisor.live_state_path) == str(part) for part in command))

    def test_proven_pre_submission_guard_does_not_block_live_restart(self):
        state=self.root/"replica-live-state.json"
        state.write_text(json.dumps({"stage":"requires_attention",
            "reason":"Price or spread exceeded the entry guard",
            "entries":[{"stage":"buy_intent"}]}))
        self.assertIsNone(dashboard_server.unsafe_live_state())

    def test_ambiguous_buy_intent_still_blocks_live_restart(self):
        state=self.root/"replica-live-state.json"
        state.write_text(json.dumps({"stage":"requires_attention",
            "error_type":"TimeoutError","entries":[{"stage":"buy_intent"}]}))
        blocked=dashboard_server.unsafe_live_state()
        self.assertEqual(blocked[0],state)

    def test_unresolved_live_state_is_passed_to_live_recovery(self):
        active = self.root / "replica-live-old.json"
        active.write_text(json.dumps({"stage":"holding","entries":[{"stage":"holding"}]}))
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode="paper";supervisor.current_mode=None
        supervisor.process=None;supervisor.transition=False;supervisor.last_exit=None
        supervisor.paper_budget=30.0;supervisor.strategy="combined"
        supervisor.live_sizing="minimum";supervisor.live_state_path=None
        supervisor.recovery_state_paths=[]
        supervisor.lock=threading.RLock();supervisor._persist=lambda:None
        process=SimpleNamespace(pid=456,poll=lambda:None)
        with patch.dict("os.environ", {"LIVE_KILL":"0"}),\
             patch.object(dashboard_server.subprocess,"Popen",return_value=process) as popen:
            result=supervisor.switch("live","LIVE")
        self.assertEqual(result["current_mode"], "live")
        self.assertEqual(result["recovery_states"], ["replica-live-old.json"])
        command = popen.call_args.args[0]
        self.assertTrue(any("run_combined_live.py" in str(part) for part in command))
        # Recovery paths stay visible; live worker places real CLOB + Kalshi orders.
        self.assertTrue(any(str(supervisor.live_state_path) == str(part) for part in command))

    def test_failed_live_worker_exposes_actionable_error(self):
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.current_mode="live"
        supervisor.live_state_path=self.root/"replica-live-new.json"
        supervisor.process=SimpleNamespace(returncode=1,poll=lambda:1)
        supervisor.last_exit=None
        supervisor._record_exit()
        self.assertIn("inspect replica-console.log",supervisor.last_exit["error"])
        self.assertEqual(supervisor.last_exit["state"],"replica-live-new.json")

    def test_paper_strategy_metrics_and_exposure_are_reported(self):
        payload = {
            "strategy_stats": {
                "arbitrage": {"trades": 3, "win_rate": 2 / 3, "roi": 0.15, "pnl": 3},
                "combined": {"trades": 3, "win_rate": 2 / 3, "roi": 0.15, "pnl": 3},
            },
            "budget": 100, "equity": 103, "open_exposure": 20, "max_exposure": 30.9,
            "exposure_rate": 0.3, "engine_health": {},
            "pairs": [{"slug": "demo", "status": "paired"}],
            "pnl_breakdown": {
                "locked_pair_profit": 1.2,
                "expected_profit": 0.4,
                "unpaired_directional_risk": 0.0,
                "realized_pnl": 3.0,
            },
        }
        (self.root / "replica-paper-portfolio.json").write_text(json.dumps(payload))
        supervisor = dashboard_server.Supervisor.__new__(dashboard_server.Supervisor)
        supervisor.desired_mode = "paper"
        supervisor.current_mode = "paper"
        supervisor.process = None
        supervisor.transition = False
        supervisor.last_exit = None
        supervisor.paper_budget = 100
        supervisor.strategy = "arbitrage"
        supervisor.lock = threading.RLock()
        result = supervisor.status()
        self.assertEqual(result["strategy_stats"]["combined"]["trades"], 3)
        self.assertEqual(result["portfolio"]["open_exposure"], 20)
        self.assertEqual(result["portfolio"]["max_exposure"], 30.9)
        self.assertEqual(result["pnl_breakdown"]["locked_pair_profit"], 1.2)
        self.assertEqual(result["pairs"][0]["slug"], "demo")

    def test_http_api_requires_basic_auth(self):
        supervisor = SimpleNamespace(status=lambda: {"worker_running": True})
        server = dashboard_server.DashboardServer(
            ("127.0.0.1", 0), dashboard_server.DashboardHandler,
            supervisor, "admin", "0123456789abcdef",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/api/status")
            self.assertEqual(connection.getresponse().status, 401)
            token = base64.b64encode(b"admin:0123456789abcdef").decode()
            connection.request("GET", "/api/status", headers={"Authorization": "Basic " + token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"worker_running", response.read())
        finally:
            server.shutdown()
            server.server_close()
