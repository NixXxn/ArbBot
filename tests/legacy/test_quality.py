from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import quality_config
import optimize_kraken
import run_ten
from live_executor import ExecutionStopped


class QualityChecks(unittest.TestCase):
    def setUp(self):
        self.config = quality_config.load(ROOT/'kraken-quality.json')
        self.start = 1790858100
        bucket = self.config['probability_buckets'][1]
        self.signal = {'slug':f'btc-updown-5m-{self.start}','token':'up','outcome':'Up','ask':.55,
                       'received_ms':(self.start+240)*1000,'end_ms':(self.start+300)*1000,
                       'quality':{'z':1.2,'move_bp':10,
                                  'probability_lower':bucket['probability_lower']-self.config['probability_haircut'],
                                  'fee_rate_ceiling':.1,'min_edge':.03}}

    def test_calibrated_signal_and_actual_fee_metadata(self):
        now=self.signal['received_ms']
        fee=quality_config.fee_info({'c':'condition','fd':{'r':.07,'e':1}},'condition',now)
        quality_config.validate(self.signal,now+100,self.config,fee)

    def test_forged_confidence_direction_and_risk_parameters_rejected(self):
        for field,value in [('probability_lower',.99),('move_bp',-10),('fee_rate_ceiling',0),('z',float('nan'))]:
            signal=deepcopy(self.signal);signal['quality'][field]=value
            with self.subTest(field=field),self.assertRaises(ExecutionStopped):
                quality_config.validate(signal,signal['received_ms'],self.config)

    def test_window_and_affordability_rejected(self):
        signal=deepcopy(self.signal);signal['ask']=.9
        with self.assertRaises(ExecutionStopped):quality_config.validate(signal,signal['received_ms'],self.config)
        with self.assertRaises(ExecutionStopped):quality_config.validate(self.signal,self.signal['received_ms']+6000,self.config)

    def test_stale_or_changed_fees_rejected(self):
        now=self.signal['received_ms']
        for fee in ({'rate':'.11','exponent':1,'checked_at_ms':now},
                    {'rate':'.07','exponent':2,'checked_at_ms':now},
                    {'rate':'.07','exponent':1,'checked_at_ms':now-61000}):
            with self.subTest(fee=fee),self.assertRaises(ExecutionStopped):
                quality_config.validate(self.signal,now,self.config,fee)

    def test_fee_identity_is_verified(self):
        with self.assertRaises(ExecutionStopped):quality_config.fee_info({'c':'other','fd':{'r':.07,'e':1}},'condition',0)

    def test_wallet_replica_uses_move_calibration_and_consensus_floor(self):
        config=quality_config.load(ROOT/'kraken-replica.json')
        bucket=config['move_probability_buckets'][1]
        signal=deepcopy(self.signal)
        signal['ask']=.72
        signal['quality'].update(z=.1,move_bp=10,
            probability_lower=bucket['probability_lower']-config['probability_haircut'],min_edge=.02)
        quality_config.validate(signal,signal['received_ms'],config)
        signal['ask']=.60
        with self.assertRaises(ExecutionStopped):
            quality_config.validate(signal,signal['received_ms'],config)

    def test_eth_replica_rejects_a_btc_slug(self):
        config=quality_config.load(ROOT/'kraken-eth-replica.json')
        bucket=config['move_probability_buckets'][1]
        signal=deepcopy(self.signal)
        signal['slug']=f'eth-updown-5m-{self.start}'
        signal['ask']=.72
        signal['quality'].update(z=.1,move_bp=10,
            probability_lower=bucket['probability_lower']-config['probability_haircut'],min_edge=.02)
        quality_config.validate(signal,signal['received_ms'],config)
        signal['slug']=f'btc-updown-5m-{self.start}'
        with self.assertRaises(ExecutionStopped):
            quality_config.validate(signal,signal['received_ms'],config)

    def test_15m_trend_uses_calibrated_score_and_dynamic_budget(self):
        config=quality_config.load(ROOT/'kraken-trend-btc.json')
        self.assertEqual(config['min_move_bp'],5.0)
        self.assertEqual(config['max_ask'],0.60)
        self.assertEqual(config['min_probability'],0.58)
        self.assertGreaterEqual(config['position_tiers'][0]['min_probability'],config['min_probability'])
        start=self.start//900*900
        bucket=config['move_probability_buckets'][0]
        probability=bucket['probability_lower']-config['probability_haircut']
        signal={'slug':f'btc-updown-15m-{start}','token':'up','outcome':'Up','ask':.55,
                'received_ms':(start+180)*1000,'end_ms':(start+900)*1000,
                'quality':{'z':.1,'move_bp':6,
                           'probability_lower':probability,'fee_rate_ceiling':.1,
                           'min_edge':config['min_edge'],'suggested_budget':4.0}}
        quality_config.validate(signal,signal['received_ms'],config)
        self.assertEqual(quality_config.position_budget(config,.60),4)
        self.assertEqual(str(quality_config.position_budget(config,.68)),'4.5')
        self.assertEqual(quality_config.position_budget(config,.75),5)
        signal['quality']['suggested_budget']=5.0
        with self.assertRaises(ExecutionStopped):
            quality_config.validate(signal,signal['received_ms'],config)
        signal['quality']['suggested_budget']=4.0
        signal['ask']=.61
        with self.assertRaises(ExecutionStopped):
            quality_config.validate(signal,signal['received_ms'],config)

    def test_proxy_uses_no_post_entry_prices_as_features(self):
        # 61 prior closes, followed by the market's observation and outcome candles.
        candles=[{'time':minute*60000,'open':'100','close':str(100+minute*.01)} for minute in range(80)]
        first=optimize_kraken.observations(candles,300,60)
        self.assertTrue(first)
        row=first[0]
        changed=deepcopy(candles)
        final=next(c for c in changed if c['time']//1000==row['start']+240)
        final['close']='50'
        second=next(x for x in optimize_kraken.observations(changed,300,60) if x['start']==row['start'])
        self.assertEqual(row['z'],second['z'])
        self.assertEqual(row['direction'],second['direction'])
        self.assertNotEqual(row['won'],second['won'])


class ClosedMarketLookup(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_queries_closed_markets_when_default_list_is_empty(self):
        class Response:
            def __init__(self,rows):self.rows=rows
            def raise_for_status(self):pass
            def json(self):return self.rows
        slug='btc-updown-5m-100'
        resolved={'slug':slug,'closed':True}
        with patch.object(run_ten.requests,'get',side_effect=[Response([]),Response([resolved])]) as request:
            self.assertEqual(await run_ten.market(slug),resolved)
        self.assertEqual(request.call_args.kwargs['params'],{'slug':slug,'closed':'true'})

    async def test_closed_lookup_still_verifies_slug(self):
        class Response:
            def raise_for_status(self):pass
            def json(self):return [{'slug':'other'}]
        with patch.object(run_ten.requests,'get',return_value=Response()),self.assertRaises(ExecutionStopped):
            await run_ten.market('btc-updown-5m-100')
