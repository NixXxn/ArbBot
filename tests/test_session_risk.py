from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from live_executor import ExecutionStopped
from session_risk import SessionRisk, break_even_probability, dynamic_reservation, minimum_reservation


class SessionLimits(unittest.TestCase):
    def test_fee_changes_required_win_probability(self):
        self.assertEqual(break_even_probability("0.8","0.07"),D("0.8112"))

    def test_distinct_markets_can_overlap_within_total_risk_limit(self):
        risk=SessionRisk(D(20),D(3));risk.reserve("a",2);risk.record_fill(2,"a")
        risk.reserve("b",1);risk.record_fill(1,"b")
        self.assertEqual(risk.open_costs,{"a":D(2),"b":D(1)})
        with self.assertRaises(ExecutionStopped):risk.reserve("c",D("0.01"))
        risk.record_resolution(2,2,"a");risk.record_resolution(1,1,"b")

    def test_realized_loss_reduces_remaining_allowance(self):
        risk=SessionRisk(D(20),D(3));risk.reserve("a",2);risk.record_fill(2);risk.record_resolution(0,2)
        with self.assertRaises(ExecutionStopped):risk.reserve("b",2)

    def test_buy_cap_uses_gross_spend_without_recycling_winnings(self):
        risk=SessionRisk(D(3),D(10));risk.reserve("a",2);risk.record_fill(2);risk.record_resolution(5,2)
        with self.assertRaises(ExecutionStopped):risk.reserve("b",2)

    def test_drawdown_from_peak_includes_worst_case_next_loss(self):
        risk=SessionRisk(D(20),D(3));risk.reserve("a",2);risk.record_fill(2);risk.record_resolution(4,2)
        risk.reserve("b",2);risk.record_fill(2);risk.record_resolution(0,2)
        with self.assertRaises(ExecutionStopped):risk.reserve("c",2)

    def test_duplicate_market_and_entry_count(self):
        risk=SessionRisk(D(20),D(3),target_entries=1)
        risk.reserve("a",1);risk.record_fill(1);risk.record_resolution(1,1)
        with self.assertRaises(ExecutionStopped):risk.reserve("b",1)

    def test_cost_outside_reservation_halts(self):
        risk=SessionRisk(D(20),D(3));risk.reserve("a",2)
        with self.assertRaises(ExecutionStopped):risk.record_fill("2.01")

    def test_pre_submission_cancel_releases_reservation_without_retrying_market(self):
        risk=SessionRisk(D(20),D(3));risk.reserve("a",2)
        risk.cancel_before_submission()
        self.assertFalse(risk.unresolved)
        self.assertEqual(risk.reserved,0)
        self.assertEqual(risk.intents,0)
        with self.assertRaises(ExecutionStopped):risk.reserve("a",2)
        risk.reserve("b",2)

    def test_nan_budget_rejected(self):
        with self.assertRaises(ExecutionStopped):SessionRisk(D("NaN"),D(3))

    def test_cannot_reconcile_using_previous_entry_cost(self):
        risk=SessionRisk(D(30),D(30))
        risk.reserve("a",3);risk.record_fill(2);risk.record_resolution(3,2)
        risk.reserve("b",3);risk.record_fill(1)
        with self.assertRaises(ExecutionStopped):risk.record_resolution(0,2)

    def test_fill_cannot_be_recorded_twice(self):
        risk=SessionRisk(D(30),D(30));risk.reserve("a",3);risk.record_fill(2)
        with self.assertRaises(ExecutionStopped):risk.record_fill(1)

    def test_dynamic_size_scales_and_never_exceeds_thirty_percent(self):
        self.assertEqual(dynamic_reservation(30, 5),D("5.00"))
        self.assertEqual(dynamic_reservation(300, 5),D("50.00"))
        self.assertEqual(dynamic_reservation(10, 20),D("3.00"))

    def test_minimum_size_covers_five_shares_after_spend_reserve(self):
        reservation=minimum_reservation(5,D("0.61"))
        self.assertEqual(reservation,D("3.82"))
        self.assertGreaterEqual(reservation*D("0.8")/D("0.61"),5)

    def test_dynamic_risk_uses_available_budget_instead_of_cumulative_spend_cap(self):
        risk=SessionRisk(D(1),D(100),target_entries=20,dynamic=True)
        risk.reserve("a",D("6"),available_budget=20)
        risk.record_fill(5);risk.record_resolution(5,5)
        risk.reserve("b",D("6"),available_budget=20)
        with self.assertRaisesRegex(ExecutionStopped,"30%"):
            SessionRisk(D(1),D(100),dynamic=True).reserve("x",D("6.01"),available_budget=20)


if __name__=="__main__":unittest.main()
