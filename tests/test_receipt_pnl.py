from pathlib import Path
import sys
import unittest
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from run_ten import collateral_flow, TRANSFER
from polymarket.environments import PRODUCTION
from live_executor import ExecutionStopped

class ReceiptPnl(unittest.TestCase):
    def test_counts_only_wallet_collateral_and_includes_fees(self):
        wallet='0x'+'1'*40
        mine='0x'+wallet[2:].zfill(64);other='0x'+'2'*64
        def log(sender,receiver,amount,address=PRODUCTION._config.collateral_token):
            return {'address':address,'topics':[TRANSFER,sender,receiver],'data':hex(amount)}
        receipt={'status':'0x1','logs':[log(mine,other,2400000),log(mine,other,50000),log(other,mine,40000),
                                     log(mine,other,999999,'0x'+'3'*40),log(other,other,999999)]}
        self.assertEqual(collateral_flow(receipt,wallet),Decimal('-2.41'))
    def test_revert_is_never_profit(self):
        with self.assertRaises(ExecutionStopped):collateral_flow({'status':'0x0','logs':[]},'0x'+'1'*40)

if __name__=='__main__':unittest.main()
