from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import run_multi_paper as paper


class SharedPaperPortfolio(unittest.TestCase):
    def test_one_or_both_strategy_process_sets_are_selectable(self):
        self.assertEqual([row[0] for row in paper.strategy_specs("kraken")], ["btc-15m"])
        self.assertEqual([row[0] for row in paper.strategy_specs("up60")], ["btc-up60"])
        self.assertEqual([row[0] for row in paper.strategy_specs("preopen")], ["btc-preopen"])
        self.assertEqual([row[0] for row in paper.strategy_specs("clip5")], ["btc-clip5"])
        self.assertEqual([row[0] for row in paper.strategy_specs("early3m")], ["btc-early3m"])
        self.assertEqual([row[0] for row in paper.strategy_specs("early3m5")], ["btc-early3m5"])
        self.assertEqual([row[0] for row in paper.strategy_specs("combined")],
                         ["btc-15m","btc-up60","btc-preopen","btc-clip5","btc-clip15","btc-early3m","btc-early3m5"])
        self.assertEqual([row[0] for row in paper.strategy_specs("all")],
                         ["btc-15m","btc-up60","btc-preopen","btc-clip5","btc-clip15","btc-early3m","btc-early3m5"])
        with self.assertRaisesRegex(ValueError,"kraken, up60, preopen, clip5, clip15, early3m, early3m5, or all"):
            paper.strategy_specs("down35")

    def test_fill_respects_shared_exposure_and_settles_into_strategy_metrics(self):
        state={"budget":30.0,"trades":[]}
        paper.update_metrics(state)
        signal={"slug":"btc-updown-15m-1800","token":"up","outcome":"Up","ask":.60,
                "received_ms":int(time.time()*1000),"execution_delay_ms":0,"quality":None}
        with tempfile.TemporaryDirectory() as directory:
            journal=Path(directory)/"portfolio.jsonl"
            with patch.object(paper,"book",return_value=(D("0.60"),D(100),D(5))):
                paper.execute_signal(state,"up60",signal,journal)
                paper.execute_signal(state,"kraken",signal,journal)
            self.assertEqual(len(state["trades"]),1)
            self.assertLessEqual(D(str(state["open_exposure"])),D(str(state["max_exposure"])))
            resolved={"closed":True,"outcomes":["Up","Down"],"outcomePrices":["1","0"]}
            with patch.object(paper,"market",return_value=resolved):
                paper.resolve_positions(state,journal)
        stats=state["strategy_stats"]
        self.assertEqual(stats["up60"]["trades"],1)
        self.assertEqual(stats["up60"]["wins"],1)
        self.assertEqual(stats["up60"]["win_rate"],1.0)
        self.assertGreater(stats["up60"]["roi"],0)
        self.assertAlmostEqual(stats["up60"]["avg_entry"],0.60,places=2)
        self.assertIsNone(stats["up60"]["breakeven_win_rate"])
        self.assertEqual(state["open_exposure"],0.0)

    def test_down35_uses_zero_paper_fee_and_tp_exit(self):
        state={'budget':300.0,'trades':[]};paper.update_metrics(state)
        signal={'slug':'sol-updown-5m-1800','token':'down','outcome':'Down','ask':.35,
                'received_ms':int(time.time()*1000),'execution_delay_ms':0,'quality':None}
        with tempfile.TemporaryDirectory() as directory:
            journal=Path(directory)/'portfolio.jsonl'
            with patch.object(paper,'book',return_value=(D('.35'),D(100),D(5))):
                paper.execute_signal(state,'down35',signal,journal)
            self.assertEqual(state['trades'][0]['fee'],0.0)
            self.assertAlmostEqual(state['trades'][0]['cost'],5.0,places=3)
            with patch.object(paper,'market',return_value={'outcomes':['Up','Down'],
                    'clobTokenIds':['up','down'],'closed':False}),\
                 patch.object(paper,'down35_exit_quote',return_value={
                    'reason':'TP','proceeds':D('9.2857'),'price':D('.65')}):
                self.assertTrue(paper.exit_down35_positions(state,journal))
        self.assertEqual(state['trades'][0]['exit_reason'],'TP')
        self.assertGreater(state['trades'][0]['net_pnl'],0)

    def test_all_mode_pauses_only_strategy_with_three_recent_losses(self):
        now=int(time.time()*1000)
        losses=[{'id':str(i),'strategy':'up60','slug':f'old-{i}','status':'settled',
                 'cost':5,'fee':0,'net_pnl':-1,'settled_at_ms':now-i*1000}
                for i in range(3)]
        state={'budget':100.0,'strategy':'all','trades':losses};paper.update_metrics(state)
        signal={'slug':'btc-updown-15m-1800','token':'up','outcome':'Up','ask':.60,
                'received_ms':now,'execution_delay_ms':0,'quality':None}
        with tempfile.TemporaryDirectory() as directory:
            journal=Path(directory)/'portfolio.jsonl'
            with patch.object(paper,'book',return_value=(D('.60'),D(100),D(5))):
                paper.execute_signal(state,'up60',signal,journal)
        self.assertEqual(len(state['trades']),3)
        self.assertIn('after 3 losses',state['risk_controls']['last_skip']['reason'])

    def test_statistics_reset_hides_only_old_settled_rows_and_keeps_open_risk(self):
        state={'budget':100.0,'trades':[
            {'strategy':'kraken','status':'settled','cost':2,'fee':0,'net_pnl':1,'settled_at_ms':100},
            {'strategy':'up60','status':'settled','cost':3,'fee':0,'net_pnl':1,'settled_at_ms':100},
            {'strategy':'kraken','status':'open','cost':2,'fee':0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            reset=Path(directory)/'resets.json';reset.write_text('{"kraken": 200}')
            paper.update_metrics(state,reset)
        self.assertEqual(state['strategy_stats']['kraken']['settled'],0)
        self.assertEqual(state['strategy_stats']['kraken']['open'],1)
        self.assertEqual(state['strategy_stats']['up60']['settled'],1)
        self.assertEqual(state['strategy_stats']['combined']['trades'],2)

    def test_early3m_hedge_buys_riskier_leg_first_then_merges(self):
        state = {"budget": 100.0, "trades": []}
        paper.update_metrics(state)
        now = int(time.time() * 1000)
        signal = {
            "slug": "btc-updown-15m-1800", "token": "up", "outcome": "Up", "ask": 0.48,
            "received_ms": now, "execution_delay_ms": 0, "scenario": "hedge_merge",
            "quality": {"suggested_budget": 5, "move_bp": 3, "fee_rate_ceiling": 0.02, "min_edge": 0.02},
            "legs": [
                {"token": "up", "outcome": "Up", "ask": 0.48, "role": "primary"},
                {"token": "down", "outcome": "Down", "ask": 0.49, "role": "hedge"},
            ],
        }
        books = {"up": (D("0.48"), D(100), D(5)), "down": (D("0.49"), D(100), D(5))}
        order = []

        def fake_book(token):
            order.append(token)
            return books[token]

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "portfolio.jsonl"
            with patch.object(paper, "book", side_effect=fake_book):
                paper.execute_signal(state, "early3m", signal, journal)
        self.assertEqual(order[:2], ["up", "down"])
        self.assertEqual(len(state["trades"]), 2)
        self.assertTrue(all(row["status"] == "settled" for row in state["trades"]))
        self.assertTrue(all(row.get("exit_reason") == "merge" for row in state["trades"]))
        self.assertEqual(state["strategy_stats"]["early3m"]["settled"], 2)
        self.assertEqual(state["open_exposure"], 0.0)


if __name__=="__main__":unittest.main()
