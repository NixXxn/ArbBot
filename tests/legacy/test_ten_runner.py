import asyncio
from pathlib import Path
import sys
import tempfile
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_ten
from decimal import Decimal as D

class TenRunner(IsolatedAsyncioTestCase):
    async def test_live_history_survives_worker_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            history=Path(directory)/'history.jsonl';records={}
            first={'id':'session-a:1','strategy':'kraken','slug':'btc-1','cost':'2',
                   'payout':'5','net_pnl':'3','won':True,'settled_at_ms':1}
            second={'id':'session-b:1','strategy':'up60','slug':'eth-1','cost':'4',
                    'payout':'0','net_pnl':'-4','won':False,'settled_at_ms':2}
            self.assertTrue(run_ten.append_live_history(history,records,first))
            self.assertTrue(run_ten.append_live_history(history,records,second))
            self.assertFalse(run_ten.append_live_history(history,records,first))
            corrected={**first,'payout':'6','net_pnl':'4','won':True}
            self.assertTrue(run_ten.append_live_history(history,records,corrected))
            restored=run_ten.load_live_history(history)
            stats=run_ten.history_metrics(restored,[{'strategy':'kraken','cost':'2'}])
        self.assertEqual(stats['combined']['trades'],3)
        self.assertEqual(stats['combined']['settled'],2)
        self.assertEqual(stats['combined']['open'],1)
        self.assertEqual(stats['combined']['pnl'],0.0)
        self.assertEqual(stats['combined']['win_rate'],.5)

    async def test_live_history_reset_filters_one_strategy_and_keeps_open_positions(self):
        records={'a':{'strategy':'kraken','cost':'2','net_pnl':'1','settled_at_ms':100},
                 'b':{'strategy':'up60','cost':'3','net_pnl':'1','settled_at_ms':100}}
        stats=run_ten.history_metrics(records,[{'strategy':'kraken','cost':'2'}],{'kraken':200})
        self.assertEqual(stats['kraken']['settled'],0)
        self.assertEqual(stats['kraken']['open'],1)
        self.assertEqual(stats['up60']['settled'],1)
        self.assertEqual(stats['combined']['trades'],2)

    def test_resolved_shares_override_zero_proxy_receipt(self):
        entry={'stage':'resolved','entry_id':'winner','strategy':'kraken','buy_cost':'2.50',
               'payout':'0','receipt_payout':'0','net_pnl':'-2.50',
               'buy':{'taking_amount':'5'},
               'signal':{'slug':'btc-updown-5m-1','outcome':'Up'},
               'resolution_outcomes':['Up','Down'],'resolution_prices':['1','0']}
        self.assertEqual(run_ten.resolution_share_payout(entry),D(5))
        record=run_ten.settlement_record(entry,'fallback')
        self.assertEqual(record['payout'],'5')
        self.assertEqual(record['net_pnl'],'2.50')
        self.assertTrue(record['won'])
        self.assertEqual(record['payout_source'],'resolved_shares')

    def test_losing_resolved_shares_have_zero_payout(self):
        entry={'stage':'resolved','buy_cost':'2.50','net_pnl':'99',
               'buy':{'taking_amount':'5'},'signal':{'outcome':'Down'},
               'resolution_outcomes':['Up','Down'],'resolution_prices':['1','0']}
        record=run_ten.settlement_record(entry,'loser')
        self.assertEqual(record['payout'],'0')
        self.assertEqual(record['net_pnl'],'-2.50')
        self.assertFalse(record['won'])

    async def test_distinct_live_markets_resolve_concurrently(self):
        root=Path(__file__).resolve().parents[1]
        start=1790857800;now=start+180
        btc_config=run_ten.quality_config.load(root/'kraken-trend-btc.json')
        def signal(asset,config):
            bucket=config['move_probability_buckets'][0]
            probability=bucket['probability_lower']-config['probability_haircut']
            return {'slug':f'{asset}-updown-15m-{start}','token':f'{asset}-up',
                'outcome':'Up','ask':.55,'received_ms':now*1000,'end_ms':(start+900)*1000,
                'quality':{'z':1.2,'move_bp':8,'probability_lower':probability,
                    'fee_rate_ceiling':.1,'min_edge':.005,
                    'suggested_budget':float(run_ten.quality_config.position_budget(config,probability))}}
        up60_signal={'slug':f'btc-updown-15m-{start}','token':'btc-up','outcome':'Up','ask':.60,
            'received_ms':now*1000,'end_ms':(start+900)*1000,'execution_delay_ms':200,
            'quality':{'probability_lower':.69,'move_bp':8,'fee_rate_ceiling':.10,'min_edge':.015}}
        signals={'btc-15m':signal('btc',btc_config),'btc-up60':up60_signal}
        def row_for(slug,closed=False):
            return {'slug':slug,'conditionId':'condition','acceptingOrders':not closed,
                'closed':closed,'clobTokenIds':['btc-up','btc-down'],
                'outcomes':['Up','Down'],'orderMinSize':5,
                'outcomePrices':['1','0'] if closed else ['.5','.5'],
                'umaResolutionStatus':'resolved' if closed else 'unresolved'}
        submit_count=0
        spawned=[]
        async def submit(*_args,**_kwargs):
            nonlocal submit_count
            submit_count+=1
            return SimpleNamespace(ok=True,code=None,order_id=f'order-{submit_count}',
                status='matched',making_amount=2,taking_amount=5,trade_ids=[],
                transactions_hashes=[f'buy-{submit_count}'])
        async def getmarket(slug):
            return row_for(slug,submit_count>=1)
        async def spawn(*command,**_kwargs):
            label=('btc-up60' if '--up60' in command else 'btc-preopen' if '--trader-clone' in command
                   else 'btc-clip5' if '--clip5' in command else 'btc-clip15' if '--clip15' in command
                   else 'btc-early3m5' if '--early3m5' in command
                   else 'btc-early3m' if '--early3m' in command else 'btc-15m')
            spawned.append(label)
            emits=label in signals
            lines=([json.dumps({'type':'signal','signal':signals[label]}).encode()] if emits else [])+[b'']
            async def readline():
                await asyncio.sleep(.1)
                return lines.pop(0)
            return SimpleNamespace(stdout=SimpleNamespace(readline=readline),
                wait=AsyncMock(return_value=0),returncode=0)
        async def allowance(*_args,**kwargs):
            return SimpleNamespace(balance=100_000_000 if kwargs.get('asset_type')=='COLLATERAL' else 0)
        redemption_count=0
        async def redeem(**_kwargs):
            nonlocal redemption_count
            redemption_count+=1;tx=f'redeem-{redemption_count}'
            return SimpleNamespace(transaction_id=tx,transaction_hash=tx,
                wait=AsyncMock(return_value=SimpleNamespace(transaction_hash=tx)))
        client=SimpleNamespace(close=AsyncMock(),get_balance_allowance=AsyncMock(side_effect=allowance),
                               redeem_positions=AsyncMock(side_effect=redeem))
        async def receipt_flows(hashes,_wallet):
            return D(-2) if str(hashes[0]).startswith('buy-') else D(5)
        fee_response=SimpleNamespace(raise_for_status=lambda:None,
            json=lambda:{'c':'condition','fd':{'r':.07,'e':1}})
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(live=True,preflight=False,state=directory+'/state.json',env='test.env',
                binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                trader_clone=False,kraken_quality=False,wallet_replica=False,kraken_trend=False,
                up60=False,down35=False,combined=True,portfolio_strategy='all',include_5m=False,
                sizing_mode='minimum',quality_config=str(root/'kraken-trend-btc.json'),
                eth_quality_config=str(root/'kraken-trend-eth.json'),
                btc_5m_config=str(root/'kraken-replica.json'),
                eth_5m_config=str(root/'kraken-eth-replica.json'),stop_file='')
            with patch.object(run_ten.time,'time',return_value=now),\
                 patch.object(run_ten.requests,'get',return_value=fee_response),\
                 patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'concurrent-wallet'}),\
                 patch.object(run_ten,'connect',AsyncMock(return_value=client)),\
                 patch.object(run_ten,'market',getmarket),\
                 patch.object(run_ten.asyncio,'create_subprocess_exec',AsyncMock(side_effect=spawn)),\
                 patch.object(run_ten,'buy',AsyncMock(side_effect=submit)),\
                 patch.object(run_ten,'settle',AsyncMock(side_effect=lambda _client,response:[f'buy-{response.order_id}'])),\
                 patch.object(run_ten,'flows',AsyncMock(side_effect=receipt_flows)),patch('builtins.print'):
                await asyncio.wait_for(run_ten.run(args),2)
            state=json.loads(Path(args.state).read_text())
        self.assertEqual(set(spawned),{'btc-15m','btc-up60','btc-preopen','btc-clip5','btc-clip15','btc-early3m','btc-early3m5'})
        self.assertEqual(submit_count,1)
        self.assertEqual(state['confirmed_entries'],1)
        self.assertEqual(state['settled_entries'],1)
        self.assertEqual(state['strategy_stats']['combined']['trades'],1)

    async def exercise(self, fail=False, pre_reject=False, rejected=False, quality=False,
                       replica=False, trend=False, stop=False, redemption_receipt=D(5)):
        calibrated=quality or replica or trend
        base=1790857800
        now=base+(180 if trend else 240 if calibrated else 0)
        slug=('btc-updown-15m-'+str(base)) if trend else ('btc-updown-5m-'+str(now//300*300))
        signal={'slug':slug,'token':'1','outcome':'Up','ask':(.55 if trend else .72 if replica else .2),'received_ms':now*1000,'end_ms':(base+900)*1000 if trend else now*1000+20000}
        config_path=Path(__file__).resolve().parents[1]/('kraken-trend-btc.json' if trend else 'kraken-replica.json' if replica else 'kraken-quality.json')
        if calibrated:
            config=run_ten.quality_config.load(config_path)
            move=8 if trend else 10
            if replica or trend:
                bucket=next(b for b in config['move_probability_buckets']
                            if move>=b['min_move_bp'] and (b['max_move_bp'] is None or move<b['max_move_bp']))
            else:
                bucket=config['probability_buckets'][1]
            signal.update(end_ms=(base+900)*1000 if trend else (now+60)*1000,quality={'z':1.2,'move_bp':move,
                'probability_lower':bucket['probability_lower']-config['probability_haircut'],
                'fee_rate_ceiling':.1,'min_edge':config['min_edge']})
            if trend:signal['quality']['suggested_budget']=float(run_ten.quality_config.position_budget(config,signal['quality']['probability_lower']))
        row={'slug':slug,'conditionId':'condition','acceptingOrders':True,'closed':False,'clobTokenIds':['1','2'],'outcomes':['Up','Down']}
        resolved={**row,'closed':True,'outcomePrices':['1','0'],'umaResolutionStatus':'resolved'}
        lines=[json.dumps({'type':'signal','signal':signal}).encode(),b'']
        async def readline():
            await asyncio.sleep(.01)
            return lines.pop(0)
        process=SimpleNamespace(stdout=SimpleNamespace(readline=readline),wait=AsyncMock(return_value=0),returncode=0)
        handle=SimpleNamespace(transaction_id='redemption-id',transaction_hash='redemption-hash',wait=AsyncMock(return_value=SimpleNamespace(transaction_hash='redemption-hash')))
        client=SimpleNamespace(close=AsyncMock(),redeem_positions=AsyncMock(return_value=handle))
        response=SimpleNamespace(ok=not rejected,code='fok_order_not_filled' if rejected else None,
            order_id='order',status='matched',making_amount=2.4,taking_amount=5,
            trade_ids=[],transactions_hashes=['buy-hash'])
        # Resolution lookup occurs after record_fill, before redemption.
        async def getmarket(_):return resolved if submit.called else row
        submit=AsyncMock(return_value=response,side_effect=(TimeoutError() if fail else
            run_ten.OrderNotSubmitted('Price or spread exceeded the entry guard') if pre_reject else None))
        with tempfile.TemporaryDirectory() as directory:
            stop_file=directory+'/stop'
            if stop:Path(stop_file).touch()
            args=SimpleNamespace(live=True,preflight=False,state=directory+'/state.json',env='test.env',binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=not calibrated,trader_clone=False,kraken_quality=quality,wallet_replica=replica,kraken_trend=trend,quality_config=str(config_path),stop_file=stop_file)
            fee_response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'c':'condition','fd':{'r':.07,'e':1}})
            with patch.object(run_ten.time,'time',return_value=now),patch.object(run_ten.requests,'get',return_value=fee_response),patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'wallet'}),patch.object(run_ten,'connect',AsyncMock(return_value=client)),patch.object(run_ten,'market',getmarket),patch.object(run_ten.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)),patch.object(run_ten,'token_balance',AsyncMock(return_value=D(0))),patch.object(run_ten,'buy',submit),patch.object(run_ten,'settle',AsyncMock(return_value=['buy-hash'])),patch.object(run_ten,'flows',AsyncMock(side_effect=[D('-2.5'),redemption_receipt])),patch('builtins.print'):
                await run_ten.run(args)
            state=json.loads(Path(args.state).read_text())
        return state,submit,client

    async def test_actual_receipt_cost_and_redemption_determine_pnl(self):
        state,buy,client=await self.exercise()
        self.assertEqual(state['settled_entries'],1)
        self.assertEqual(state['spent'],'2.5')
        self.assertEqual(state['realized_pnl'],'2.5')
        client.redeem_positions.assert_awaited_once_with(condition_id='condition')
        buy.assert_awaited_once()
        self.assertEqual(state['strategy'],'kraken_only')

    async def test_proxy_receipt_zero_does_not_turn_winner_into_loss(self):
        state,_,_=await self.exercise(redemption_receipt=D(0))
        entry=state['entries'][0]
        self.assertEqual(entry['receipt_payout'],'0')
        self.assertEqual(entry['payout'],'5')
        self.assertEqual(entry['net_pnl'],'2.5')
        self.assertEqual(entry['payout_source'],'resolved_shares')
        self.assertEqual(state['strategy_stats']['combined']['wins'],1)

    async def test_ambiguous_buy_stops_without_redemption_or_retry(self):
        state,buy,client=await self.exercise(fail=True)
        self.assertEqual(state['stage'],'requires_attention')
        self.assertEqual(state['entries'][0]['stage'],'buy_intent')
        buy.assert_awaited_once();client.redeem_positions.assert_not_awaited()

    async def test_pre_submission_guard_is_recorded_and_monitoring_can_continue(self):
        state,buy,client=await self.exercise(pre_reject=True)
        self.assertEqual(state['stage'],'time_limit_reached')
        self.assertEqual(state['entries'][0]['stage'],'not_submitted')
        self.assertEqual(state['confirmed_entries'],0)
        self.assertEqual(state['settled_entries'],0)
        self.assertEqual(state['skipped'],1)
        buy.assert_awaited_once();client.redeem_positions.assert_not_awaited()

    async def test_explicit_fok_rejection_does_not_stop_live_worker(self):
        state,buy,client=await self.exercise(rejected=True)
        self.assertEqual(state['stage'],'time_limit_reached')
        self.assertEqual(state['entries'][0]['stage'],'not_submitted')
        self.assertIn('fok_order_not_filled',state['entries'][0]['reason'])
        self.assertEqual(state['confirmed_entries'],0)
        buy.assert_awaited_once();client.redeem_positions.assert_not_awaited()

    async def test_recovery_with_no_inventory_marks_old_state_safe(self):
        client=SimpleNamespace(get_balance_allowance=AsyncMock(
            return_value=SimpleNamespace(balance=0)))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            path.write_text(json.dumps({'stage':'requires_attention','entries':[{
                'stage':'buy_intent','signal':{'token':'token','slug':'btc-updown-5m-1'},
                'condition_id':'condition'}]}))
            await run_ten.recover_live_state(client,{'POLY_FUNDER':'wallet'},path)
            recovered=json.loads(path.read_text())
        self.assertEqual(recovered['stage'],'recovered')
        self.assertEqual(recovered['entries'][0]['stage'],'reconciled_no_inventory')

    async def test_recovery_preserves_settled_statistics_after_restart(self):
        client=SimpleNamespace(get_balance_allowance=AsyncMock(
            return_value=SimpleNamespace(balance=0)))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            path.write_text(json.dumps({'stage':'redemption_intent','entries':[{
                'stage':'redemption_intent','signal':{'token':'token','slug':'btc-updown-5m-1'},
                'condition_id':'condition','strategy':'kraken','buy_cost':'2',
                'redemption_hash':'redeemed'}]}))
            with patch.object(run_ten,'flows',AsyncMock(return_value=D(5))):
                await run_ten.recover_live_state(client,{'POLY_FUNDER':'wallet'},path)
            recovered=json.loads(path.read_text())
        self.assertEqual(recovered['entries'][0]['stage'],'recovered_resolved')
        self.assertEqual(recovered['entries'][0]['net_pnl'],'3')

    async def test_recovery_uses_protected_minimum_when_exit_post_was_ambiguous(self):
        client=SimpleNamespace(get_balance_allowance=AsyncMock(
            return_value=SimpleNamespace(balance=5_000)))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            path.write_text(json.dumps({'stage':'requires_attention','entries':[{
                'stage':'exit_intent','signal':{'token':'down','slug':'sol-updown-5m-1'},
                'condition_id':'condition','strategy':'down35','buy_cost':'5',
                'exit_intent':{'reason':'SL','minimum_proceeds':'2.80'}}]}))
            await run_ten.recover_live_state(client,{'POLY_FUNDER':'wallet'},path)
            entry=json.loads(path.read_text())['entries'][0]
        self.assertEqual(entry['stage'],'recovered_resolved')
        self.assertEqual(entry['payout_source'],'exit_minimum_proceeds')
        self.assertEqual(entry['net_pnl'],'-2.20')

    async def test_recovery_redeems_a_held_position_before_continuing(self):
        handle=SimpleNamespace(transaction_id='id',transaction_hash='pending',
            wait=AsyncMock(return_value=SimpleNamespace(transaction_hash='redeemed')))
        client=SimpleNamespace(get_balance_allowance=AsyncMock(side_effect=[
            SimpleNamespace(balance=5_000_000),SimpleNamespace(balance=5_000_000),
            SimpleNamespace(balance=0)]),redeem_positions=AsyncMock(return_value=handle))
        resolved={'closed':True,'outcomePrices':['1','0'],'umaResolutionStatus':'resolved'}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            path.write_text(json.dumps({'stage':'holding','entries':[{'stage':'holding',
                'signal':{'token':'token','slug':'btc-updown-5m-1'},
                'condition_id':'condition','buy_cost':'2'}]}))
            with patch.object(run_ten,'market',AsyncMock(side_effect=[TimeoutError(),resolved])),\
                 patch.object(run_ten.asyncio,'sleep',AsyncMock()),\
                 patch.object(run_ten,'flows',AsyncMock(return_value=D(5))):
                await run_ten.recover_live_state(client,{'POLY_FUNDER':'wallet'},path)
            recovered=json.loads(path.read_text())
        self.assertEqual(recovered['stage'],'recovered')
        self.assertEqual(recovered['entries'][0]['stage'],'recovered_resolved')
        self.assertEqual(recovered['entries'][0]['net_pnl'],'3')
        self.assertEqual(recovered['entries'][0]['resolution_poll_error'],'TimeoutError')
        client.redeem_positions.assert_awaited_once_with(condition_id='condition')

    async def test_recovery_cancels_a_known_legacy_order_before_continuing(self):
        client=SimpleNamespace(
            cancel_order=AsyncMock(return_value=SimpleNamespace(canceled=('order-1',))),
            get_balance_allowance=AsyncMock(return_value=SimpleNamespace(balance=0)))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            path.write_text(json.dumps({'stage':'buy_intent','entries':[{
                'stage':'buy_intent','signal':{'token':'token','slug':'btc-updown-5m-1'},
                'condition_id':'condition','buy':{'ok':False,'order_id':'order-1'}}]}))
            await run_ten.recover_live_state(client,{'POLY_FUNDER':'wallet'},path)
            recovered=json.loads(path.read_text())
        self.assertTrue(recovered['entries'][0]['recovery_cancelled_order'])
        self.assertEqual(recovered['entries'][0]['stage'],'reconciled_no_inventory')
        client.cancel_order.assert_awaited_once_with(order_id='order-1')

    async def test_quality_runner_preserves_receipt_limits_and_redemption(self):
        state,submit,client=await self.exercise(quality=True)
        self.assertEqual(state['strategy'],'kraken_quality')
        self.assertEqual(state['target_entries'],7)
        self.assertEqual(state['per_entry_cap'],'4')
        self.assertEqual(state['settled_entries'],1)
        self.assertEqual(state['realized_pnl'],'2.5')
        self.assertEqual(submit.call_args.args[3],D(4))

    async def test_wallet_replica_uses_six_fixed_five_dollar_reservations(self):
        state,submit,_=await self.exercise(replica=True)
        self.assertEqual(state['strategy'],'kraken_wallet_replica')
        self.assertEqual(state['target_entries'],6)
        self.assertEqual(state['per_entry_cap'],'5.0')
        self.assertEqual(submit.call_args.args[3],D(5))

    async def test_15m_trend_uses_calibrated_budget_for_weak_signal(self):
        state,submit,_=await self.exercise(trend=True)
        self.assertEqual(state['strategy'],'kraken_trend_15m')
        self.assertEqual(state['target_entries'],6)
        self.assertEqual(state['entries'][0]['reserved_budget'],'4.5')
        self.assertEqual(submit.call_args.args[3],D('4.5'))

    async def test_trend_preflight_can_load_all_four_markets(self):
        root=Path(__file__).resolve().parents[1]
        client=SimpleNamespace(close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(live=False,preflight=True,state=directory+'/state.json',env='test.env',
                binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                trader_clone=False,kraken_quality=False,wallet_replica=False,kraken_trend=True,
                include_5m=True,quality_config=str(root/'kraken-trend-btc.json'),
                eth_quality_config=str(root/'kraken-trend-eth.json'),
                btc_5m_config=str(root/'kraken-replica.json'),
                eth_5m_config=str(root/'kraken-eth-replica.json'),stop_file='')
            with patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'four-market-wallet'}),\
                 patch.object(run_ten,'connect',AsyncMock(return_value=client)),patch('builtins.print'):
                await run_ten.run(args)
            state=json.loads(Path(args.state).read_text())
        self.assertEqual(state['strategy'],'kraken_trend_15m_5m')
        self.assertEqual(state['markets'],['btc-15m','btc-5m','eth-15m','eth-5m'])
        self.assertEqual(set(state['quality_configs']),set(state['markets']))

    async def test_stop_file_prevents_another_entry(self):
        state,submit,client=await self.exercise(replica=True,stop=True)
        self.assertEqual(state['stage'],'stopped')
        self.assertEqual(state['confirmed_entries'],0)
        submit.assert_not_awaited()
        client.redeem_positions.assert_not_awaited()

    async def test_replica_preflight_loads_btc_and_eth_configs(self):
        root=Path(__file__).resolve().parents[1]
        client=SimpleNamespace(close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(live=False,preflight=True,state=directory+'/state.json',env='test.env',
                binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                trader_clone=False,kraken_quality=False,wallet_replica=True,
                quality_config=str(root/'kraken-replica.json'),
                eth_quality_config=str(root/'kraken-eth-replica.json'),stop_file='')
            with patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'dual-wallet'}),\
                 patch.object(run_ten,'connect',AsyncMock(return_value=client)),patch('builtins.print'):
                await run_ten.run(args)
            state=json.loads(Path(args.state).read_text())
        self.assertEqual(state['stage'],'preflight_complete')
        self.assertEqual(state['assets'],['btc','eth'])
        self.assertEqual(set(state['quality_configs']),{'btc','eth'})

    async def test_combined_preflight_loads_all_btc_feeds_and_wallet_scaled_limits(self):
        root=Path(__file__).resolve().parents[1]
        client=SimpleNamespace(close=AsyncMock(),get_balance_allowance=AsyncMock(
            return_value=SimpleNamespace(balance=100_000_000)))
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(live=False,preflight=True,state=directory+'/state.json',env='test.env',
                binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                trader_clone=False,kraken_quality=False,wallet_replica=False,kraken_trend=False,
                up60=False,combined=True,include_5m=False,
                sizing_mode='minimum',
                quality_config=str(root/'kraken-trend-btc.json'),
                eth_quality_config=str(root/'kraken-trend-eth.json'),
                btc_5m_config=str(root/'kraken-replica.json'),
                eth_5m_config=str(root/'kraken-eth-replica.json'),stop_file='')
            with patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'combined-wallet'}),\
                 patch.object(run_ten,'connect',AsyncMock(return_value=client)),patch('builtins.print'):
                await run_ten.run(args)
            state=json.loads(Path(args.state).read_text())
        self.assertEqual(state['strategy'],'combined')
        self.assertEqual(state['starting_balance'],'100')
        self.assertEqual(state['stop_loss'],'30.00')
        self.assertEqual(state['sizing_mode'],'minimum')
        self.assertEqual(state['portfolio']['capital'],100.0)
        self.assertEqual(state['portfolio']['max_exposure'],30.0)
        self.assertEqual(set(state['markets']),
                         {'btc-15m','btc-up60','btc-preopen','btc-clip5','btc-clip15','btc-early3m','btc-early3m5'})

    async def test_combined_portfolio_can_enable_one_strategy(self):
        root=Path(__file__).resolve().parents[1]
        expected={
            'kraken':{'btc-15m'},
            'up60':{'btc-up60'},
            'preopen':{'btc-preopen'},
            'clip5':{'btc-clip5'},
            'clip15':{'btc-clip15'},
            'early3m':{'btc-early3m'},
            'early3m5':{'btc-early3m5'},
            'down35':{'sol-down35'},
            'all':{'btc-15m','btc-up60','btc-preopen','btc-clip5','btc-clip15','btc-early3m','btc-early3m5'},
            'combined':{'btc-15m','btc-up60','btc-preopen','btc-clip5','btc-clip15','btc-early3m','btc-early3m5'},
        }
        for selection,markets in expected.items():
            with self.subTest(selection=selection),tempfile.TemporaryDirectory() as directory:
                client=SimpleNamespace(close=AsyncMock(),get_balance_allowance=AsyncMock(
                    return_value=SimpleNamespace(balance=100_000_000)))
                args=SimpleNamespace(live=False,preflight=True,state=directory+'/state.json',env='test.env',
                    binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                    trader_clone=False,kraken_quality=False,wallet_replica=False,kraken_trend=False,
                    up60=False,combined=True,portfolio_strategy=selection,include_5m=False,
                    quality_config=str(root/'kraken-trend-btc.json'),
                    eth_quality_config=str(root/'kraken-trend-eth.json'),
                    btc_5m_config=str(root/'kraken-replica.json'),
                    eth_5m_config=str(root/'kraken-eth-replica.json'),stop_file='')
                with patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'selected-wallet'}),\
                     patch.object(run_ten,'connect',AsyncMock(return_value=client)),patch('builtins.print'):
                    await run_ten.run(args)
                state=json.loads(Path(args.state).read_text())
            self.assertEqual(state['strategy'],selection)
            self.assertEqual(set(state['markets']),markets)

    async def test_down35_live_trade_exits_at_tp_without_second_leg(self):
        start=1790859600;now=start+100
        signal={'slug':f'sol-updown-5m-{start}','token':'down','outcome':'Down','ask':.32,
                'received_ms':now*1000,'end_ms':(start+300)*1000,
                'execution_delay_ms':0,'quality':{'probability_lower':.50,'move_bp':-11,
                    'fee_rate_ceiling':0,'min_edge':.10}}
        row={'slug':signal['slug'],'conditionId':'condition','acceptingOrders':True,'closed':False,
             'clobTokenIds':['up','down'],'outcomes':['Up','Down'],'orderMinSize':5}
        lines=[json.dumps({'type':'signal','signal':signal}).encode(),b'']
        async def readline():await asyncio.sleep(.01);return lines.pop(0)
        process=SimpleNamespace(stdout=SimpleNamespace(readline=readline),
                                wait=AsyncMock(return_value=0),returncode=0)
        buy_response=SimpleNamespace(ok=True,code=None,order_id='buy',status='matched',
            making_amount=5,taking_amount=14.2857,trade_ids=[],transactions_hashes=['buy-hash'])
        exit_response=SimpleNamespace(ok=True,code=None,order_id='sell',status='matched',
            making_amount=14.2857,taking_amount=9.1,trade_ids=[],transactions_hashes=['sell-hash'])
        held=SimpleNamespace(bids=[SimpleNamespace(price=D('.66'),size=D(100))],
            asks=[SimpleNamespace(price=D('.67'),size=D(100))],min_order_size=D(5),tick_size=D('.01'))
        opposite=SimpleNamespace(bids=[SimpleNamespace(price=D('.79'),size=D(100))],
            asks=[SimpleNamespace(price=D('.80'),size=D(100))],min_order_size=D(5),tick_size=D('.01'))
        client=SimpleNamespace(close=AsyncMock(),get_balance_allowance=AsyncMock(
            return_value=SimpleNamespace(balance=100_000_000)),get_order_book=AsyncMock(
                side_effect=lambda token_id:held if token_id=='down' else opposite))
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(live=True,preflight=False,state=directory+'/state.json',env='test.env',
                binary='fake',journal=directory+'/signals.jsonl',seconds=1,kraken_only=False,
                trader_clone=False,kraken_quality=False,wallet_replica=False,kraken_trend=False,
                up60=False,down35=False,combined=True,portfolio_strategy='down35',include_5m=False,
                sizing_mode='adaptive',quality_config='kraken-trend-btc.json',
                eth_quality_config='kraken-trend-eth.json',btc_5m_config='kraken-replica.json',
                eth_5m_config='kraken-eth-replica.json',stop_file='')
            with patch.object(run_ten.time,'time',return_value=now),\
                 patch.object(run_ten,'enabled_env',return_value={'POLY_FUNDER':'wallet'}),\
                 patch.object(run_ten,'connect',AsyncMock(return_value=client)),\
                 patch.object(run_ten,'market',AsyncMock(return_value=row)),\
                 patch.object(run_ten.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)),\
                 patch.object(run_ten,'token_balance',AsyncMock(return_value=D(0))),\
                 patch.object(run_ten,'buy',AsyncMock(return_value=buy_response)),\
                 patch.object(run_ten,'sell_position',AsyncMock(return_value=exit_response)),\
                 patch.object(run_ten,'settle',AsyncMock(side_effect=lambda _client,response:list(response.transactions_hashes))),\
                 patch.object(run_ten,'flows',AsyncMock(side_effect=[D('-5'),D('9.1')])),patch('builtins.print'):
                await asyncio.wait_for(run_ten.run(args),2)
            state=json.loads(Path(args.state).read_text())
        entry=state['entries'][0]
        self.assertEqual(entry['strategy'],'down35');self.assertEqual(entry['exit_reason'],'TP')
        self.assertEqual(entry['net_pnl'],'4.1');self.assertNotIn('counter_hedge',entry)
        self.assertEqual(state['strategy_stats']['down35']['wins'],1)

    def test_trader_clone_signal_window(self):
        start=1790859600
        signal={'slug':f'btc-updown-15m-{start}','outcome':'Down','ask':.51,
                'received_ms':start*1000-205000,'end_ms':(start+900)*1000,
                'quality':{'move_bp':-15}}
        run_ten.validate_strategy_signal(signal,start*1000-205000,True)
        with self.assertRaises(run_ten.ExecutionStopped):
            run_ten.validate_strategy_signal(signal,start*1000-100000,True)
        for changed in (dict(signal,ask=.52),dict(signal,quality={'move_bp':-12})):
            with self.assertRaises(run_ten.ExecutionStopped):
                run_ten.validate_strategy_signal(changed,start*1000-205000,True)

    def test_clip_signal_stays_inside_the_profitable_bands(self):
        start=1790859600
        now=(start+100)*1000
        five={'slug':f'btc-updown-5m-{start}','outcome':'Up','ask':.63,
              'received_ms':now,'end_ms':(start+300)*1000,'quality':{'move_bp':6}}
        run_ten.validate_clip_signal(five,now,300)
        fifteen_now=(start+320)*1000
        fifteen={'slug':f'btc-updown-15m-{start}','outcome':'Up','ask':.60,
                 'received_ms':fifteen_now,'end_ms':(start+900)*1000,'quality':{'move_bp':8}}
        run_ten.validate_clip_signal(fifteen,fifteen_now,900)
        for changed,interval,when in ((dict(five,ask=.58),300,now),(dict(five,quality={'move_bp':2}),300,now),
                                      (dict(fifteen,ask=.40),900,fifteen_now),
                                      (dict(fifteen,ask=.60),900,(start+100)*1000),
                                      (dict(five,slug=f'eth-updown-5m-{start}'),300,now)):
            with self.assertRaises(run_ten.ExecutionStopped):
                run_ten.validate_clip_signal(changed,when,interval)

    def test_up60_signal_is_isolated_and_strictly_up(self):
        start=1790859600;now=(start+100)*1000
        signal={'slug':f'btc-updown-15m-{start}','token':'up','outcome':'Up','ask':.59,
                'received_ms':now,'end_ms':(start+900)*1000,
                'execution_delay_ms':200,'quality':{'probability_lower':.69,'move_bp':8,
                    'fee_rate_ceiling':.10,'min_edge':.015}}
        run_ten.validate_strategy_signal(signal,now,up60=True)
        for changed in (dict(signal,outcome='Down'),dict(signal,ask=.63),
                        dict(signal,slug=f'eth-updown-15m-{start}')):
            with self.assertRaises(run_ten.ExecutionStopped):
                run_ten.validate_strategy_signal(changed,now,up60=True)

    def test_down35_signal_is_sol_5m_down_only(self):
        start=1790859600;now=(start+100)*1000
        signal={'slug':f'sol-updown-5m-{start}','token':'down','outcome':'Down','ask':.32,
                'received_ms':now,'end_ms':(start+300)*1000,'execution_delay_ms':0,
                'quality':{'probability_lower':.50,'move_bp':-11,
                    'fee_rate_ceiling':0,'min_edge':.10}}
        run_ten.validate_down35_signal(signal,now)
        for changed in (dict(signal,outcome='Up'),dict(signal,ask=.33),
                        dict(signal,slug=f'eth-updown-5m-{start}')):
            with self.assertRaises(run_ten.ExecutionStopped):
                run_ten.validate_down35_signal(changed,now)

    def test_early3m_signal_accepts_directional_and_hedge(self):
        start=1790859600;now=(start+190)*1000
        directional={'slug':f'btc-updown-15m-{start}','token':'up','outcome':'Up','ask':.58,
            'received_ms':now,'end_ms':(start+900)*1000,'scenario':'directional',
            'quality':{'move_bp':9,'fee_rate_ceiling':.02,'min_edge':0}}
        run_ten.validate_early3m_signal(directional,now)
        hedge={'slug':f'btc-updown-15m-{start}','token':'up','outcome':'Up','ask':.48,
            'received_ms':now,'end_ms':(start+900)*1000,'scenario':'hedge_merge',
            'quality':{'move_bp':3,'fee_rate_ceiling':.02,'min_edge':.02},
            'legs':[{'token':'up','outcome':'Up','ask':.48,'role':'primary'},
                    {'token':'down','outcome':'Down','ask':.49,'role':'hedge'}]}
        run_ten.validate_early3m_signal(hedge,now)
        five_now=(start+100)*1000
        five={'slug':f'btc-updown-5m-{start}','token':'up','outcome':'Up','ask':.58,
            'received_ms':five_now,'end_ms':(start+300)*1000,'scenario':'directional',
            'quality':{'move_bp':7,'fee_rate_ceiling':.02,'min_edge':0}}
        run_ten.validate_early3m_signal(five,five_now,300)
        with self.assertRaises(run_ten.ExecutionStopped):
            run_ten.validate_early3m_signal(dict(directional,quality={'move_bp':5}),now)
        with self.assertRaises(run_ten.ExecutionStopped):
            run_ten.validate_early3m_signal(dict(directional,ask=.70),now)
        with self.assertRaises(run_ten.ExecutionStopped):
            run_ten.validate_early3m_signal(dict(hedge,legs=[
                {'token':'up','outcome':'Up','ask':.55,'role':'primary'},
                {'token':'down','outcome':'Down','ask':.55,'role':'hedge'}]),now)

    def test_cli_portfolio_strategy_accepts_preopen_and_clip_feeds(self):
        source=(Path(__file__).resolve().parents[1]/'scripts'/'run_ten.py').read_text()
        marker="choices=('kraken','up60','preopen','clip5','clip15','early3m','early3m5','down35','combined','all')"
        self.assertIn(marker,source)
        import argparse
        parser=argparse.ArgumentParser()
        parser.add_argument('--portfolio-strategy',
                            choices=('kraken','up60','preopen','clip5','clip15','early3m','early3m5','down35','combined','all'))
        for name in ('preopen','clip5','clip15','early3m','early3m5','all'):
            self.assertEqual(parser.parse_args(['--portfolio-strategy',name]).portfolio_strategy,name)
        with self.assertRaises(SystemExit):
            parser.parse_args(['--portfolio-strategy','unknown'])
