from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from portfolio_controls import balanced_reservation, loss_cooldown, recovery_budget


class PortfolioControlsTests(unittest.TestCase):
    def test_balances_strategy_at_fifteen_percent(self):
        self.assertEqual(balanced_reservation(100,0,25,2),D('15.00'))
        self.assertEqual(balanced_reservation(100,12,5,2),D('3.00'))
        self.assertEqual(balanced_reservation(100,14,5,2),D(0))

    def test_allows_one_market_minimum_on_small_bankroll(self):
        self.assertEqual(balanced_reservation(30,0,D('6.25'),D('6.25')),D('6.25'))
        self.assertEqual(balanced_reservation(30,D('6.25'),D('6.25'),D('6.25')),D(0))

    def test_three_losses_cool_down_then_expire(self):
        rows=[{'strategy':'up60','net_pnl':'-1','settled_at_ms':stamp}
              for stamp in (1000,2000,3000)]
        self.assertEqual(loss_cooldown(rows,'up60',4000),899000)
        self.assertEqual(loss_cooldown(rows,'up60',903001),0)
        rows.append({'strategy':'up60','net_pnl':'1','settled_at_ms':4000})
        self.assertEqual(loss_cooldown(rows,'up60',5000),0)

    def test_preopen_recovery_steps_once_after_a_loss(self):
        self.assertEqual(recovery_budget([],'preopen'),D('5'))
        loss=[{'strategy':'preopen','net_pnl':'-2','settled_at_ms':1000}]
        self.assertEqual(recovery_budget(loss,'preopen'),D('10'))
        win=loss+[{'strategy':'preopen','net_pnl':'3','settled_at_ms':2000}]
        self.assertEqual(recovery_budget(win,'preopen'),D('5'))


if __name__=='__main__':unittest.main()
