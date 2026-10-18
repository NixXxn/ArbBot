"""Bounded live execution with concurrent per-market resolution and durable statistics."""
import argparse
import asyncio
from decimal import Decimal, ROUND_DOWN
import fcntl
import json
import os
from pathlib import Path
import time

import requests
from eth_utils import keccak
from polymarket.environments import PRODUCTION
from polymarket.models.clob.order_response import AcceptedOrder
from live_executor import (
    ExecutionStopped, OrderNotSubmitted, best, buy, connect, enabled_env, encode_response,
    merge_positions, place_limit_buy, save, sell_position, settle, token_balance,
    wait_limit_fill, UNITS,
)
from run_live import validate_signal
from session_risk import SessionRisk, dynamic_reservation, minimum_reservation
from portfolio_controls import balanced_reservation, loss_cooldown
from statistics_resets import load as load_resets
import quality_config

D = Decimal
TRANSFER = '0x'+keccak(text='Transfer(address,address,uint256)').hex()
TERMINAL_ENTRY_STAGES = {'resolved','recovered_resolved','reconciled_no_inventory','not_submitted'}


def decoded_list(value):
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:return []
    return value if isinstance(value,list) else []


def resolution_share_payout(entry):
    """Return the binary redemption entitlement, independent of relayer receipt routing."""
    prices=decoded_list(entry.get('resolution_prices'))
    outcomes=decoded_list(entry.get('resolution_outcomes'))
    outcome=(entry.get('signal') or {}).get('outcome')
    if not outcomes and len(prices)==2 and outcome in ('Up','Down'):
        outcomes=['Up','Down']
    if len(outcomes)!=len(prices) or outcome not in outcomes:
        return None
    try:
        winning_price=D(str(prices[outcomes.index(outcome)]))
        shares=D(str(entry.get('filled_shares',(entry.get('buy') or {}).get('taking_amount'))))
    except (ValueError,TypeError,ArithmeticError):
        return None
    if winning_price not in (D(0),D(1)) or not shares.is_finite() or shares < 0:
        return None
    return shares*winning_price


def capture_resolution(entry,resolved):
    prices=decoded_list(resolved.get('outcomePrices','[]'))
    outcomes=decoded_list(resolved.get('outcomes','[]'))
    try:binary=sorted(D(str(price)) for price in prices)==[D(0),D(1)]
    except (ValueError,TypeError,ArithmeticError):binary=False
    if not (resolved.get('closed') and binary
            and resolved.get('umaResolutionStatus')=='resolved'):
        return False
    entry['resolution_prices']=prices
    if outcomes:entry['resolution_outcomes']=outcomes
    return True


def settlement_record(entry, fallback_id):
    if entry.get('stage') not in ('resolved','recovered_resolved'):
        return None
    if entry.get('buy_cost') is None:
        return None
    cost=D(str(entry['buy_cost']))
    resolved_payout=resolution_share_payout(entry)
    if resolved_payout is not None:
        payout=resolved_payout;pnl=payout-cost;source='resolved_shares'
    else:
        if entry.get('net_pnl') is None:return None
        pnl=D(str(entry['net_pnl']))
        payout=D(str(entry.get('payout',entry.get('recovered_payout',cost+pnl))))
        source=entry.get('payout_source','receipt_transfer')
    return {'id':entry.get('entry_id',fallback_id),'strategy':entry.get('strategy','unknown'),
            'slug':(entry.get('signal') or {}).get('slug'),
            'ask':(entry.get('signal') or {}).get('ask'),'cost':str(cost),
            'payout':str(payout),'net_pnl':str(pnl),'won':pnl>0,
            'payout_source':source,
            'settled_at_ms':entry.get('settled_at_ms',entry.get('reconciled_at_ms'))}


def load_live_history(history_path):
    records={}
    path=Path(history_path)
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            try:record=json.loads(line)
            except ValueError:continue
            if isinstance(record,dict) and record.get('id'):
                records[record['id']]=record
    except OSError:
        pass
    return records


def append_live_history(history_path,records,record):
    # JSONL is append-only. A later row with the same id supersedes an older row,
    # allowing upgraded accounting to repair historical statistics once.
    if records.get(record['id'])==record:return False
    path=Path(history_path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(record,separators=(',',':'))+'\n')
        handle.flush();os.fsync(handle.fileno())
    records[record['id']]=record
    return True


def history_metrics(records,open_entries,reset_at=None):
    reset_at=reset_at or {}
    def metrics(strategy=None, excluded=()):
        settled=[row for row in records.values()
                 if (strategy is None or row.get('strategy')==strategy)
                 and row.get('strategy') not in excluded
                 and (not reset_at.get(row.get('strategy'),0)
                      or int(row.get('settled_at_ms') or 0)>reset_at.get(row.get('strategy'),0))]
        opened=[row for row in open_entries
                if (strategy is None or row.get('strategy')==strategy)
                and row.get('strategy') not in excluded]
        cost=sum((D(str(row['cost'])) for row in settled),D(0))
        pnl=sum((D(str(row['net_pnl'])) for row in settled),D(0))
        win_rows=[row for row in settled if D(str(row['net_pnl']))>0]
        loss_rows=[row for row in settled if D(str(row['net_pnl']))<=0]
        asks=[D(str(row['ask'])) for row in settled if row.get('ask') not in (None,'')]
        avg_win=(sum((D(str(row['net_pnl'])) for row in win_rows),D(0))/D(len(win_rows))
                 if win_rows else None)
        avg_loss=(sum((D(str(row['net_pnl'])) for row in loss_rows),D(0))/D(len(loss_rows))
                  if loss_rows else None)
        breakeven=None
        if avg_win is not None and avg_loss is not None and avg_win!=avg_loss:
            breakeven=float((-avg_loss)/(avg_win-avg_loss))
        return {'trades':len(settled)+len(opened),'settled':len(settled),'open':len(opened),
                'wins':len(win_rows),'win_rate':float(D(len(win_rows))/D(len(settled))) if settled else None,
                'roi':float(pnl/cost) if cost else None,'pnl':float(pnl),'settled_cost':float(cost),
                'avg_entry':float(sum(asks,D(0))/D(len(asks))) if asks else None,
                'breakeven_win_rate':breakeven}
    names=('kraken','up60','preopen','clip5','clip15','early3m','early3m5','down35')
    stats={name:metrics(name) for name in names}
    stats['combined']=metrics(excluded={'down35'})
    return stats


def validate_early3m_signal(signal, now_ms, interval=900):
    parts = signal.get('slug', '').split('-')
    quality = signal.get('quality') or {}
    scenario = signal.get('scenario') or 'directional'
    family = '5m' if interval == 300 else '15m'
    observe_s = 90 if interval == 300 else 180
    min_remaining = 60 if interval == 300 else 120
    directional_bp = 6 if interval == 300 else 8
    try:
        ask = float(signal.get('ask'))
        move = float(quality.get('move_bp', 0))
    except (TypeError, ValueError):
        ask, move = -1, 0
    if (len(parts) != 4 or parts[0] != 'btc' or parts[1:3] != ['updown', family]
            or signal.get('outcome') not in ('Up', 'Down')
            or not 0.1 <= ask <= 0.9
            or not 0 <= now_ms - signal.get('received_ms', 0) <= 500):
        raise ExecutionStopped('Unexpected early3m market, price, or stale signal')
    try:
        start = int(parts[3])
    except ValueError:
        raise ExecutionStopped('Invalid early3m market start')
    if start % interval or signal.get('end_ms') != (start + interval) * 1000:
        raise ExecutionStopped('early3m market timing does not match its slug')
    elapsed = now_ms / 1000 - start
    remaining = start + interval - now_ms / 1000
    if elapsed < observe_s or remaining < min_remaining:
        raise ExecutionStopped('early3m signal is outside the live window')
    if scenario == 'directional':
        if abs(move) < directional_bp:
            raise ExecutionStopped(f'early3m directional move below {directional_bp}bp')
        if ask > 0.62:
            raise ExecutionStopped('early3m directional ask above 0.62')
        if signal.get('legs'):
            raise ExecutionStopped('early3m directional signal must not include legs')
        return
    if scenario != 'hedge_merge':
        raise ExecutionStopped('Unknown early3m scenario')
    legs = signal.get('legs') or []
    if len(legs) != 2 or legs[0].get('role') != 'primary' or legs[1].get('role') != 'hedge':
        raise ExecutionStopped('early3m hedge requires primary then hedge legs')
    try:
        pair = float(legs[0]['ask']) + float(legs[1]['ask'])
    except (TypeError, ValueError, KeyError):
        raise ExecutionStopped('early3m hedge legs missing asks')
    if pair >= 0.99 or pair * 1.02 >= 1.0:
        raise ExecutionStopped('early3m pair is not arbable after fees')
    outcomes = {legs[0].get('outcome'), legs[1].get('outcome')}
    if outcomes != {'Up', 'Down'}:
        raise ExecutionStopped('early3m hedge must cover both outcomes')


def validate_strategy_signal(signal, now_ms, trader_clone=False, up60=False):
    if not trader_clone and not up60:
        return validate_signal(signal, now_ms)
    if up60:
        parts=signal.get('slug','').split('-')
        quality=signal.get('quality') or {}
        if len(parts) != 4 or parts[0] != 'btc' or parts[1:3] != ['updown','15m'] \
                or signal.get('outcome') != 'Up' or not .58 <= signal.get('ask',0) <= .62 \
                or signal.get('execution_delay_ms') != 200 \
                or quality.get('probability_lower',0) < .69 or quality.get('move_bp',0) < 7.5 \
                or quality.get('fee_rate_ceiling') != .10 or quality.get('min_edge') != .015:
            raise ExecutionStopped('Unexpected Up-60 strategy signal')
        try:start_ms=int(parts[3])*1000
        except ValueError:raise ExecutionStopped('Invalid Up-60 market start')
        if start_ms % 900_000 or signal.get('end_ms') != start_ms+900_000 \
                or not start_ms <= now_ms < signal['end_ms'] \
                or not 0 <= now_ms-signal.get('received_ms',0) <= 500:
            raise ExecutionStopped('Stale or invalid Up-60 market timing')
        return
    quality=signal.get("quality") or {}
    try:ask=float(signal.get("ask"))
    except (TypeError, ValueError):ask=-1
    if not signal["slug"].startswith("btc-updown-15m-") or signal["outcome"] not in ("Up", "Down") \
            or not 0.49 <= ask <= 0.51 or abs(quality.get("move_bp", 0)) < 15:
        raise ExecutionStopped("Unexpected pre-open market, price, or move")
    if not 0 <= now_ms-signal["received_ms"] <= 500:
        raise ExecutionStopped("Signal is stale")
    try:
        start_ms = int(signal["slug"].rsplit("-", 1)[1]) * 1000
    except (ValueError, IndexError):
        raise ExecutionStopped("Invalid market start in slug")
    if start_ms % 1_800_000 or signal["end_ms"] != start_ms + 900_000 \
            or not 175_000 <= start_ms-now_ms <= 240_000:
        raise ExecutionStopped("Outside the inferred trader-clone entry window")


def validate_clip_signal(signal, now_ms, interval):
    parts=signal.get('slug','').split('-')
    family='5m' if interval==300 else '15m'
    quality=signal.get('quality') or {}
    try:ask=float(signal.get('ask'))
    except (TypeError, ValueError):ask=-1
    try:move=float(quality.get('move_bp',0))
    except (TypeError, ValueError):move=0
    band=(0.62 <= ask <= 0.65) if interval==300 else (0.55 <= ask <= 0.65)
    required=5 if interval==300 else 7.5
    confirmed=(signal.get('outcome')=='Up' and move>=required) or \
        (signal.get('outcome')=='Down' and move<=-required)
    if len(parts)!=4 or parts[0]!='btc' or parts[1:3]!=['updown', family] \
            or signal.get('outcome') not in ('Up','Down') or not band or not confirmed:
        raise ExecutionStopped('Unexpected clip market, price band, or momentum')
    try:start=int(parts[3])
    except ValueError:raise ExecutionStopped('Invalid clip market start')
    if start % interval or signal.get('end_ms')!=(start+interval)*1000:
        raise ExecutionStopped('Clip market timing does not match its slug')
    elapsed=now_ms/1000-start
    remaining=start+interval-now_ms/1000
    min_elapsed=30 if interval==300 else 300
    min_remaining=60 if interval==300 else 90
    if elapsed < min_elapsed or remaining < min_remaining \
            or not 0 <= now_ms-signal.get('received_ms',0) <= 500:
        raise ExecutionStopped('Clip signal is outside the live window or stale')


def validate_down35_signal(signal,now_ms):
    parts=signal.get('slug','').split('-')
    quality=signal.get('quality') or {}
    try:ask=D(str(signal.get('ask')))
    except (ValueError,TypeError,ArithmeticError):ask=D('NaN')
    if (len(parts)!=4 or parts[:3]!=['sol','updown','5m']
            or signal.get('outcome')!='Down'
            or quality.get('probability_lower',0)<.50 or quality.get('move_bp',0)>-10
            or quality.get('fee_rate_ceiling')!=0 or quality.get('min_edge')!=.10
            or signal.get('execution_delay_ms')!=0
            or not ask.is_finite() or not 0 < ask <= D('.32')
            or not 0 <= now_ms-signal.get('received_ms',0) <= 500):
        raise ExecutionStopped('Unexpected SOL Down-0.35 signal')
    try:start_ms=int(parts[3])*1000
    except ValueError:raise ExecutionStopped('Invalid SOL 5m market start')
    if start_ms%300_000 or signal.get('end_ms')!=start_ms+300_000 \
            or not start_ms<=now_ms<signal['end_ms']:
        raise ExecutionStopped('Invalid SOL 5m market timing')


def collateral_flow(receipt, wallet):
    if receipt.get('status') != '0x1':
        raise ExecutionStopped('Transaction is unconfirmed or reverted')
    total = 0
    topic_wallet = '0x'+wallet.lower().removeprefix('0x').zfill(64)
    for log in receipt.get('logs', []):
        topics = log.get('topics', [])
        if log['address'].lower() != PRODUCTION._config.collateral_token.lower() or len(topics) != 3 or topics[0].lower() != TRANSFER:
            continue
        amount = int(log['data'], 16)
        if topics[1].lower() == topic_wallet:
            total -= amount
        if topics[2].lower() == topic_wallet:
            total += amount
    return D(total)/D(1_000_000)


async def flows(hashes, wallet):
    if not hashes:
        raise ExecutionStopped('Missing receipt hashes')
    total = D(0)
    for tx in set(hashes):
        deadline = time.monotonic()+60
        while True:
            r = await asyncio.to_thread(requests.post, PRODUCTION._config.rpc_url,
                json={'jsonrpc':'2.0','id':1,'method':'eth_getTransactionReceipt','params':[tx]}, timeout=15)
            r.raise_for_status()
            receipt = r.json().get('result')
            if receipt:
                total += collateral_flow(receipt, wallet)
                break
            if time.monotonic() >= deadline:
                raise ExecutionStopped('Receipt pending; reconcile before continuing')
            await asyncio.sleep(2)
    return total


async def market(slug):
    r = await asyncio.to_thread(requests.get, 'https://gamma-api.polymarket.com/markets', params={'slug':slug}, timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        # Gamma's default list excludes closed markets; resolution needs this query.
        r = await asyncio.to_thread(requests.get, 'https://gamma-api.polymarket.com/markets',
                                    params={'slug':slug,'closed':'true'}, timeout=15)
        r.raise_for_status();rows = r.json()
    if len(rows) != 1 or rows[0]['slug'] != slug:
        raise ExecutionStopped('Market identity unavailable')
    return rows[0]


async def collateral_available(client):
    balance = await client.get_balance_allowance(asset_type="COLLATERAL")
    value = D(str(balance.balance)) / UNITS
    if not value.is_finite() or value <= 0:
        raise ExecutionStopped('Collateral balance is unavailable')
    return value


async def recover_live_state(client, env, state_path):
    """Reconcile one older FOK intent/holding before any new signal is accepted."""
    path = Path(state_path)
    previous = json.loads(path.read_text(encoding='utf-8'))
    previous['stage'] = 'recovering'
    save(path,previous)
    for entry in previous.get('entries',[]):
        if entry.get('stage') not in ('buy_intent','exit_intent','holding','redemption_intent'):
            continue
        signal=entry.get('signal') or {}
        token=signal.get('token')
        if not token:
            raise ExecutionStopped(f'Recovery state lacks token identity: {path.name}')
        buy_record=entry.get('buy') or {}
        # Current entries are FOK and therefore cannot rest. Older state files may
        # contain a known order id from a previous strategy version; cancelling that
        # id is safe even when the order already filled or was killed.
        if buy_record.get('order_id') and not entry.get('recovery_cancel_checked'):
            try:
                cancellation=await client.cancel_order(order_id=buy_record['order_id'])
                entry['recovery_cancelled_order']=buy_record['order_id'] in {
                    str(order_id) for order_id in cancellation.canceled}
            except Exception as exc:
                entry['recovery_cancel_error']=type(exc).__name__
            entry['recovery_cancel_checked']=True
            save(path,previous)
        if buy_record.get('ok') and not entry.get('buy_hashes'):
            try:
                response=AcceptedOrder(order_id=buy_record['order_id'],status=buy_record['status'],
                    making_amount=buy_record['making_amount'],taking_amount=buy_record['taking_amount'],
                    trade_ids=tuple(buy_record.get('trade_ids',())),
                    transactions_hashes=tuple(buy_record.get('transaction_hashes',())))
                entry['buy_hashes']=await settle(client,response)
                if not entry.get('buy_cost'):
                    entry['buy_cost']=str(-await flows(entry['buy_hashes'],env['POLY_FUNDER']))
                save(path,previous)
            except Exception as exc:
                entry['settlement_recheck_error']=type(exc).__name__
                save(path,previous)
        exit_record=entry.get('exit') or {}
        if exit_record.get('ok') and not entry.get('exit_hashes'):
            try:
                response=AcceptedOrder(order_id=exit_record['order_id'],status=exit_record['status'],
                    making_amount=exit_record['making_amount'],taking_amount=exit_record['taking_amount'],
                    trade_ids=tuple(exit_record.get('trade_ids',())),
                    transactions_hashes=tuple(exit_record.get('transaction_hashes',())))
                entry['exit_hashes']=await settle(client,response);save(path,previous)
            except Exception as exc:
                entry['exit_settlement_recheck_error']=type(exc).__name__;save(path,previous)
        balance=await token_balance(client,token)
        if entry.get('stage')=='exit_intent' and balance<=D('.01') and entry.get('buy_cost'):
            try:
                if entry.get('exit_hashes'):
                    payout=await flows(entry['exit_hashes'],env['POLY_FUNDER']);source='exit_receipt'
                else:
                    payout=D(str((entry.get('exit_intent') or {})['minimum_proceeds']))
                    source='exit_minimum_proceeds'
                entry.update(stage='recovered_resolved',recovered_payout=str(payout),
                    payout_source=source,net_pnl=str(payout-D(str(entry['buy_cost']))),
                    reconciled_at_ms=int(time.time()*1000))
                save(path,previous);continue
            except Exception as exc:
                entry['exit_recovery_error']=type(exc).__name__;save(path,previous)
        if balance <= 0:
            if entry.get('buy_cost') and entry.get('redemption_hash'):
                try:
                    try:capture_resolution(entry,await market(signal['slug']))
                    except Exception as exc:entry['recovery_resolution_error']=type(exc).__name__
                    receipt_payout=await flows([str(entry['redemption_hash'])],env['POLY_FUNDER'])
                    payout=resolution_share_payout(entry)
                    source='resolved_shares' if payout is not None else 'receipt_transfer'
                    if payout is None:payout=receipt_payout
                    entry.update(stage='recovered_resolved',recovered_payout=str(payout),
                                 receipt_payout=str(receipt_payout),payout_source=source,
                                 net_pnl=str(payout-D(str(entry['buy_cost']))))
                except Exception as exc:
                    entry['recovery_receipt_error']=type(exc).__name__
                    entry['stage']='reconciled_no_inventory'
            else:
                entry['stage']='reconciled_no_inventory'
            entry['reconciled_at_ms']=int(time.time()*1000)
            save(path,previous)
            continue
        entry['recovered_shares']=str(balance)
        entry['stage']='holding'
        previous['stage']='recovering_holding'
        save(path,previous)
        while True:
            try:
                resolved=await market(signal['slug'])
            except Exception as exc:
                entry['resolution_poll_error']=type(exc).__name__
                save(path,previous)
                await asyncio.sleep(15)
                continue
            if capture_resolution(entry,resolved):
                break
            await asyncio.sleep(15)
        # A prior redemption may still be settling. Give it time to finish before
        # requesting another idempotent CTF redemption for remaining inventory.
        if entry.get('redemption_hash'):
            for _ in range(20):
                if await token_balance(client,token) <= 0:break
                await asyncio.sleep(15)
        if await token_balance(client,token) > 0:
            entry['stage']='redemption_intent';save(path,previous)
            handle=await client.redeem_positions(condition_id=entry['condition_id'])
            entry['redemption_transaction_id']=handle.transaction_id
            entry['redemption_hash']=handle.transaction_hash;save(path,previous)
            result=await handle.wait()
            entry['redemption_hash']=str(result.transaction_hash);save(path,previous)
            receipt_payout=await flows([str(result.transaction_hash)],env['POLY_FUNDER'])
            entry['receipt_payout']=str(receipt_payout)
            payout=resolution_share_payout(entry)
            entry['payout_source']='resolved_shares' if payout is not None else 'receipt_transfer'
            entry['recovered_payout']=str(receipt_payout if payout is None else payout)
        if await token_balance(client,token) > 0:
            raise ExecutionStopped(f'Recovery left inventory in {path.name}')
        entry['stage']='recovered_resolved'
        entry['reconciled_at_ms']=int(time.time()*1000)
        if entry.get('buy_cost') and entry.get('recovered_payout'):
            entry['net_pnl']=str(D(entry['recovered_payout'])-D(entry['buy_cost']))
        save(path,previous)
    previous['stage']='recovered'
    previous['recovered_at_ms']=int(time.time()*1000)
    save(path,previous)
    return path.name


async def run(args):
    if not args.live and not args.preflight:
        raise ExecutionStopped('Use --live explicitly or --preflight')
    path = Path(args.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path)+'.lock', 'a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    if path.exists():
        raise ExecutionStopped('State already exists: inspect it; never restart an uncertain session')
    trader_clone = getattr(args, 'trader_clone', False)
    quality_mode = getattr(args, 'kraken_quality', False)
    replica_mode = getattr(args, 'wallet_replica', False)
    trend_mode = getattr(args, 'kraken_trend', False)
    up60_mode = getattr(args, 'up60', False)
    down35_mode = getattr(args,'down35',False)
    combined_mode = getattr(args, 'combined', False)
    portfolio_strategy = getattr(args, 'portfolio_strategy', 'combined')
    sizing_mode = getattr(args, 'sizing_mode', 'minimum')
    if combined_mode and portfolio_strategy not in (
            'kraken','up60','preopen','clip5','clip15','early3m','early3m5','down35','combined','all'):
        raise ExecutionStopped(
            'Portfolio strategy must be kraken, up60, preopen, clip5, clip15, early3m, early3m5, down35, combined, or all')
    if combined_mode and sizing_mode not in ('minimum','adaptive'):
        raise ExecutionStopped('Sizing mode must be minimum or adaptive')
    all_mode = combined_mode and portfolio_strategy in ('all','combined')
    use_kraken = not combined_mode or portfolio_strategy in ('kraken','combined','all')
    use_up60 = up60_mode or (combined_mode and portfolio_strategy in ('up60','combined','all'))
    use_preopen = combined_mode and portfolio_strategy in ('preopen','combined','all')
    use_clip5 = combined_mode and portfolio_strategy in ('clip5','combined','all')
    use_clip15 = combined_mode and portfolio_strategy in ('clip15','combined','all')
    use_early3m = combined_mode and portfolio_strategy in ('early3m','combined','all')
    use_early3m5 = combined_mode and portfolio_strategy in ('early3m5','combined','all')
    use_down35 = down35_mode or (combined_mode and portfolio_strategy == 'down35')
    include_5m = bool(trend_mode and getattr(args, 'include_5m', False))
    calibrated_mode = quality_mode or replica_mode or trend_mode or (combined_mode and use_kraken)
    if sum((args.kraken_only,trader_clone,quality_mode,replica_mode,trend_mode,up60_mode,down35_mode,combined_mode)) > 1:
        raise ExecutionStopped('Choose one strategy flag')
    primary_config_path = getattr(args,'quality_config','kraken-quality.json')
    if combined_mode and use_kraken and primary_config_path == 'kraken-quality.json':
        primary_config_path = 'kraken-trend-btc.json'
    config = quality_config.load(primary_config_path) if calibrated_mode else None
    config_specs = []
    if config:
        primary_label = f"{config['asset']}-{config['interval_seconds']//60}m" if trend_mode or combined_mode else config['asset']
        config_specs.append((primary_label, config,
                             primary_config_path,
                             '--kraken-trend' if trend_mode or combined_mode else '--wallet-replica' if replica_mode else '--kraken-quality'))
    eth_config_path = getattr(args, 'eth_quality_config', '')
    if (replica_mode or trend_mode) and not combined_mode and eth_config_path:
        eth_config = quality_config.load(eth_config_path)
        if eth_config['asset'] != 'eth':
            raise ExecutionStopped('ETH configuration must declare asset=eth')
        eth_label = f"eth-{eth_config['interval_seconds']//60}m" if trend_mode or combined_mode else 'eth'
        config_specs.append((eth_label, eth_config,
                             eth_config_path, '--kraken-trend' if trend_mode or combined_mode else '--wallet-replica'))
    if include_5m:
        for asset, attr, default in (('btc','btc_5m_config','kraken-replica.json'),
                                     ('eth','eth_5m_config','kraken-eth-replica.json')):
            config_path = getattr(args,attr,default)
            item = quality_config.load(config_path)
            if item['asset'] != asset or item['version'] != 2:
                raise ExecutionStopped(f'{asset.upper()} 5-minute configuration identity mismatch')
            config_specs.append((f'{asset}-5m',item,config_path,'--wallet-replica'))
    configs = {(item['asset'],item['interval_seconds']):item for _,item,_,_ in config_specs}
    if len(configs) != len(config_specs):
        raise ExecutionStopped('Duplicate asset/timeframe configuration')
    if replica_mode and any(item.get('version') != 2 for item in configs.values()):
        raise ExecutionStopped('Wallet replica requires version 2 configurations')
    if (trend_mode or combined_mode) and any(item.get('version') != 3 for _,item,_,flag in config_specs if flag == '--kraken-trend'):
        raise ExecutionStopped('15-minute trend strategy requires version 3 configurations')
    if quality_mode and config.get('version') != 1:
        raise ExecutionStopped('Kraken quality requires a version 1 configuration')
    env = enabled_env(args.env)
    # Current run_ten sessions in this checkout share this wallet lock, even when
    # they use different state paths. Older versions must still be stopped manually.
    wallet_lock_path = Path('logs')/('wallet-'+env['POLY_FUNDER'].lower()+'.lock')
    wallet_lock_path.parent.mkdir(parents=True,exist_ok=True)
    wallet_lock = open(wallet_lock_path,'a')
    fcntl.flock(wallet_lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
    if combined_mode:
        target_entries, entry_budget = 1_000_000, D(5)
    elif replica_mode or trend_mode:
        target_entries, entry_budget = config['target_entries'], D(str(config['entry_budget']))
    elif up60_mode or down35_mode:
        target_entries, entry_budget = 6, D(5)
    else:
        target_entries, entry_budget = (7, D(4)) if trader_clone or quality_mode else (10, D(3))
    risk = SessionRisk(D(1_000_000) if combined_mode else D(30),
                       D(1_000_000) if combined_mode else D(30),
                       target_entries=target_entries, dynamic=combined_mode)
    strategy = portfolio_strategy if combined_mode else ('sol_down35_5m' if down35_mode else ('up60_15m' if up60_mode else (('kraken_trend_15m_5m' if include_5m else 'kraken_trend_15m') if trend_mode else ('kraken_wallet_replica' if replica_mode else ('kraken_quality' if quality_mode else ('trader_clone_15m_mean_reversion' if trader_clone else ('kraken_only' if args.kraken_only else 'three_exchange_confirmation')))))))
    state = {'stage':'connect','target_entries':None if combined_mode else target_entries,
             'total_buy_cap':'dynamic' if combined_mode else '30','stop_loss':'30% starting balance' if combined_mode else '30',
             'per_entry_cap':str(entry_budget),'exit':'hold_to_resolution','entries':[], 'skipped':0,
             'strategy':strategy,
             'sizing_mode':sizing_mode if combined_mode else 'fixed',
             'strategies':(['kraken','up60','preopen','clip5','clip15','early3m','early3m5'] if all_mode else
                           [portfolio_strategy]) if combined_mode else [strategy],
             'assets':sorted(({'btc'} if use_kraken or use_up60 or use_preopen or use_clip5 or use_clip15 or use_early3m or use_early3m5 else set()) |
                             ({'sol'} if use_down35 else set())) if combined_mode else
                      (['sol'] if down35_mode else (sorted({asset for asset,_ in configs}) if configs else (['btc'] if up60_mode else ['btc']))),
             'markets':(sorted(label for label,_,_,_ in config_specs) +
                        (['btc-up60'] if use_up60 else []) +
                        (['btc-preopen'] if use_preopen else []) +
                        (['btc-clip5'] if use_clip5 else []) +
                        (['btc-clip15'] if use_clip15 else []) +
                        (['btc-early3m'] if use_early3m else []) +
                        (['btc-early3m5'] if use_early3m5 else []) +
                        (['sol-down35'] if use_down35 else [])) if combined_mode else
                       (['sol-down35'] if down35_mode else (sorted(label for label,_,_,_ in config_specs) if config_specs else (['btc-up60'] if up60_mode else []))),
             'martingale':False,
             'max_exposure_rate':'0.30' if combined_mode else None,
             'risk_controls':({'mode':'risk_balanced_all','strategy_exposure_rate':0.15,
                               'loss_streak_limit':3,'loss_cooldown_seconds':900,
                               'skips':{}} if all_mode else {}),
             'skip_reasons':{},
             'confirmed_entries':0,'settled_entries':0,'archived_entries':0,
             'profitability_established':False, 'started_at_ms':int(time.time()*1000)}
    if (trend_mode or combined_mode) and config:state['position_tiers']=config['position_tiers']
    if config:state['quality_config']=config
    if len(config_specs)>1:state['quality_configs']={label:item for label,item,_,_ in config_specs}
    history_path=Path(getattr(args,'history','') or path.parent/'replica-live-history.jsonl')
    portfolio_state_path=Path(getattr(args,'portfolio_state','') or path.parent/'replica-portfolio-live.json')
    stats_resets_path=Path(getattr(args,'stats_resets','') or path.parent/'replica-stat-resets.json')
    history_records=load_live_history(history_path)
    for old_path in sorted(path.parent.glob('replica-live-*.json')):
        if old_path == path:continue
        try:old_state=json.loads(old_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        for index,old_entry in enumerate(old_state.get('entries',[])):
            record=settlement_record(old_entry,f'{old_path.name}:{index}')
            if record:append_live_history(history_path,history_records,record)
    client, processes, cache_task, stream_tasks, position_tasks = None, [], None, [], set()
    position_errors=[]
    cached = {}
    async def refresh():
        while True:
            now = int(time.time())
            if trader_clone:
                slugs = ['btc-updown-15m-'+str((now//900+1)*900)]
            elif combined_mode:
                slugs=[]
                if use_kraken:
                    slugs.extend(asset+f'-updown-{interval//60}m-'+str(now//interval*interval)
                                 for asset,interval in configs)
                if use_up60 or use_clip15 or use_early3m:
                    slugs.append('btc-updown-15m-'+str(now//900*900))
                if use_preopen:
                    slugs.append('btc-updown-15m-'+str((now//900+1)*900))
                if use_clip5 or use_early3m5:
                    slugs.append('btc-updown-5m-'+str(now//300*300))
                if use_down35:
                    slugs.append('sol-updown-5m-'+str(now//300*300))
                slugs=sorted(set(slugs))
            elif down35_mode:
                slugs=['sol-updown-5m-'+str(now//300*300)]
            elif up60_mode:
                slugs=['btc-updown-15m-'+str(now//900*900)]
            else:
                slugs = [asset+f'-updown-{interval//60}m-'+str(now//interval*interval)
                         for asset,interval in configs] if calibrated_mode else ['btc-updown-5m-'+str(now//300*300)]
            async def fetch_metadata(slug):
                try:
                    row = await market(slug)
                    if calibrated_mode:
                        response = await asyncio.to_thread(requests.get,
                            'https://clob.polymarket.com/clob-markets/'+row['conditionId'],timeout=15)
                        response.raise_for_status()
                        row['_quality_fee'] = quality_config.fee_info(response.json(),row['conditionId'],int(time.time()*1000))
                    return slug,row
                except Exception:
                    return slug,None
            rows=await asyncio.gather(*(fetch_metadata(slug) for slug in slugs))
            fresh={slug:row for slug,row in rows if row is not None}
            cached.clear();cached.update(fresh)
            await asyncio.sleep(5)
    def persist():
        for index,entry in enumerate(state['entries']):
            record=settlement_record(entry,entry.get('entry_id',f'{path.name}:{index}'))
            if record and append_live_history(history_path,history_records,record):
                entry['history_recorded']=True
        active_entries=[entry for entry in state['entries']
                        if entry.get('stage') in ('buy_intent','exit_intent','holding','redemption_intent')]
        open_entries=[{'strategy':entry.get('strategy','unknown'),'cost':entry['buy_cost']}
                      for entry in active_entries if entry.get('buy_cost') is not None]
        state.update(spent=str(risk.spent),realized_pnl=str(risk.realized_pnl),
                     open_positions=len(active_entries))
        state['strategy_stats']=history_metrics(history_records,open_entries,
                                                load_resets(stats_resets_path))
        if combined_mode and state.get('available_budget') is not None:
            available=D(str(state['available_budget']))
            deployed=sum(risk.open_costs.values(),D(0))
            reserved=risk.reserved
            capital=available+deployed
            state['portfolio']={'starting_capital':float(D(str(state.get('starting_balance',capital)))),
                'capital':float(capital),'available':float(available),
                'open_exposure':float(deployed+reserved),
                'max_exposure':float((capital*D('0.30')).quantize(D('0.01'))),
                'exposure_rate':0.30}
        if len(state['entries'])>500:
            removable=[entry for entry in state['entries'] if entry.get('stage') in TERMINAL_ENTRY_STAGES]
            remove_count=min(len(removable),len(state['entries'])-400)
            remove_ids={id(entry) for entry in removable[:remove_count]}
            state['entries']=[entry for entry in state['entries'] if id(entry) not in remove_ids]
            state['archived_entries']+=remove_count
        save(path,state)
        save(portfolio_state_path,{'updated_at_ms':int(time.time()*1000),
            'strategy_stats':state['strategy_stats'],'portfolio':state.get('portfolio',{}),
            'risk_controls':state.get('risk_controls',{}),
            'history_file':history_path.name,'settled_trades':len(history_records)})
    try:
        client = await connect(args.env, redemption=True)
        recovery_paths=getattr(args,'recover_state',[]) or []
        if recovery_paths:
            state['stage']='recovering_previous_state';persist()
            for recovery_path in recovery_paths:
                state.setdefault('recovered_states',[]).append(
                    await recover_live_state(client,env,recovery_path))
                try:recovered_state=json.loads(Path(recovery_path).read_text(encoding='utf-8'))
                except (OSError,ValueError):recovered_state={}
                for index,recovered_entry in enumerate(recovered_state.get('entries',[])):
                    record=settlement_record(recovered_entry,f'{Path(recovery_path).name}:{index}')
                    if record:append_live_history(history_path,history_records,record)
        if combined_mode:
            starting_balance = await collateral_available(client)
            risk.stop_loss = (starting_balance * D('0.30')).quantize(D('0.01'))
            state.update(starting_balance=str(starting_balance), available_budget=str(starting_balance),
                         stop_loss=str(risk.stop_loss),
                         sizing=('market minimum shares' if sizing_mode=='minimum'
                                 else 'base tier / $30 × available budget'))
        if args.preflight:
            state['stage']='preflight_complete'
            return True
        persist()
        cache_task=asyncio.create_task(refresh())
        process_specs=[]
        if calibrated_mode:
            for label,item,config_path,flag in config_specs:
                asset=item['asset']
                journal = args.journal if len(config_specs)==1 else str(Path(args.journal).with_name(Path(args.journal).stem+'-'+label+Path(args.journal).suffix))
                process_specs.append((label,[str(Path(args.binary).resolve()), '--signals-only', '--seconds',
                    str(args.seconds), '--journal', journal, '--asset', asset, flag,
                    '--quality-config',str(Path(config_path).resolve())]))
        if use_down35:
            journal=str(Path(args.journal).with_name(Path(args.journal).stem+'-sol-down35'+Path(args.journal).suffix))
            process_specs.append(('sol-down35',[str(Path(args.binary).resolve()),'--signals-only',
                '--seconds',str(args.seconds),'--journal',journal,'--asset','sol','--down35']))
        if use_up60:
            for asset in ('btc',):
                label=f'{asset}-up60'
                journal=str(Path(args.journal).with_name(Path(args.journal).stem+'-'+label+Path(args.journal).suffix))
                process_specs.append((label,[str(Path(args.binary).resolve()),'--signals-only','--seconds',
                    str(args.seconds),'--journal',journal,'--asset',asset,'--up60']))
        if use_preopen:
            label='btc-preopen'
            journal=str(Path(args.journal).with_name(Path(args.journal).stem+'-'+label+Path(args.journal).suffix))
            process_specs.append((label,[str(Path(args.binary).resolve()),'--signals-only','--seconds',
                str(args.seconds),'--journal',journal,'--asset','btc','--trader-clone']))
        for label, flag, enabled in (
                ('btc-clip5','--clip5',use_clip5),('btc-clip15','--clip15',use_clip15),
                ('btc-early3m','--early3m',use_early3m),('btc-early3m5','--early3m5',use_early3m5)):
            if enabled:
                journal=str(Path(args.journal).with_name(Path(args.journal).stem+'-'+label+Path(args.journal).suffix))
                process_specs.append((label,[str(Path(args.binary).resolve()),'--signals-only','--seconds',
                    str(args.seconds),'--journal',journal,'--asset','btc',flag]))
        if not process_specs and not calibrated_mode:
            process_args=[str(Path(args.binary).resolve()), '--signals-only', '--seconds',
                          str(args.seconds), '--journal', args.journal]
            if args.kraken_only:process_args.append('--kraken-only')
            if trader_clone:process_args.append('--trader-clone')
            process_specs.append(('btc',process_args))
        for asset,process_args in process_specs:
            process=await asyncio.create_subprocess_exec(*process_args, stdout=asyncio.subprocess.PIPE)
            processes.append((asset,process))
        state['stage']='monitor';persist()
        # Signal ingestion stays independent from position resolution. The queue is
        # bounded because signals older than one second are intentionally unusable.
        stream = asyncio.Queue(maxsize=max(8,len(processes)*2))
        active_streams=len(processes)
        async def read_stream(asset,process):
            nonlocal active_streams
            # Drain each engine while any number of distinct markets resolve in parallel.
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:break
                    try:event=json.loads(line)
                    except ValueError:continue
                    if event.get('type') == 'health':
                        state.setdefault('engine_health_by_asset',{})[asset] = event
                        if len(processes)==1:state['engine_health'] = event
                        persist();continue
                    if event.get('type') != 'signal':continue
                    if stream.full():
                        stream.get_nowait();state['discarded_pending_signals']=state.get('discarded_pending_signals',0)+1
                    stream.put_nowait((asset,event))
            finally:
                active_streams-=1
                state.setdefault('engine_exits',{})[asset]=await process.wait()
                persist()
                if active_streams==0:
                    if stream.full():stream.get_nowait()
                    stream.put_nowait(None)
        stream_tasks = [asyncio.create_task(read_stream(asset,process)) for asset,process in processes]
        try:last_reset_mtime=stats_resets_path.stat().st_mtime_ns
        except OSError:last_reset_mtime=0
        async def resolve_position(entry,cost):
            signal=entry['signal'];slug=signal['slug']
            resolution_deadline=time.monotonic()+1800
            async def attempt_down35_exit():
                if entry.get('strategy')!='down35':return False
                try:
                    held_book,opposite_book=await asyncio.gather(
                        client.get_order_book(token_id=signal['token']),
                        client.get_order_book(token_id=entry['opposite_token']))
                    held_bid,_=best(held_book);_,opposite_ask=best(opposite_book)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    entry['exit_quote_error']=type(exc).__name__;return False
                reason=trigger=None
                if held_bid.price>=D('.65'):reason,trigger='TP',D('.65')
                elif opposite_ask.price>=D('.80'):
                    reason='SL';trigger=max(held_book.tick_size,held_bid.price-held_book.tick_size*2)
                if trigger is None:return False
                shares=D(str(entry['filled_shares']))
                exit_shares=shares.quantize(D('.01'),rounding=ROUND_DOWN)
                entry['stage']='exit_intent';entry['exit_intent']={
                    'reason':reason,'trigger_price':str(trigger),
                    'shares':str(exit_shares),
                    'minimum_proceeds':str((exit_shares*trigger).quantize(D('.000001')))}
                persist()
                try:
                    response=await sell_position(client,args.env,signal['token'],exit_shares,trigger)
                except OrderNotSubmitted as exc:
                    entry['stage']='holding';entry['exit_intent']['last_rejection']=str(exc);persist()
                    return False
                entry['exit']=encode_response(response);persist()
                if not response.ok:
                    entry['stage']='holding';entry['exit_intent']['last_rejection']='Exit rejected: '+str(response.code)
                    persist();return False
                entry['exit_hashes']=await settle(client,response);persist()
                proceeds=await flows(entry['exit_hashes'],env['POLY_FUNDER'])
                if proceeds <= 0 or await token_balance(client,signal['token']) > D('.01'):
                    raise ExecutionStopped('SOL exit receipt or remaining inventory is ambiguous')
                risk.record_resolution(proceeds,cost,slug)
                if combined_mode:
                    try:state['available_budget']=str(await collateral_available(client))
                    except Exception:state['available_budget']=str(D(str(state['available_budget']))+proceeds)
                entry.update(stage='resolved',payout=str(proceeds),net_pnl=str(proceeds-cost),
                    payout_source='exit_receipt',exit_reason=reason,
                    exit_price=str(proceeds/exit_shares),settled_at_ms=int(time.time()*1000))
                state['settled_entries']+=1;persist()
                print(json.dumps({'stage':'exited','entry':entry['entry_id'],'reason':reason,
                    'net_pnl':entry['net_pnl'],'session_pnl':str(risk.realized_pnl)}),flush=True)
                return True
            while True:
                if await attempt_down35_exit():return
                try:
                    resolved=await market(slug)
                    if capture_resolution(entry,resolved):
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    entry['resolution_poll_error']=type(exc).__name__
                if resolution_deadline and time.monotonic()>=resolution_deadline:
                    entry['resolution_delayed']=True
                    entry['resolution_delayed_at_ms']=int(time.time()*1000)
                    resolution_deadline=None
                persist();await asyncio.sleep(2 if entry.get('strategy')=='down35' else 15)
            payout=None
            pending_redemption_checks=0
            while payout is None:
                try:
                    balance=await token_balance(client,signal['token'])
                    if entry.get('redemption_hash') and balance > 0:
                        if pending_redemption_checks < 20:
                            pending_redemption_checks+=1
                            await asyncio.sleep(15)
                            continue
                        entry['previous_redemption_timed_out']=True
                        entry.pop('redemption_hash',None)
                    if not entry.get('redemption_hash'):
                        entry['stage']='redemption_intent';persist()
                        handle=await client.redeem_positions(condition_id=entry['condition_id'])
                        entry['redemption_transaction_id']=handle.transaction_id
                        entry['redemption_hash']=handle.transaction_hash;persist()
                        pending_redemption_checks=0
                        result=await handle.wait()
                        entry['redemption_hash']=str(result.transaction_hash);persist()
                    tx_hash=entry.get('redemption_hash')
                    if await token_balance(client,signal['token']) <= 0 and tx_hash:
                        receipt_payout=await flows([str(tx_hash)],env['POLY_FUNDER'])
                        entry['receipt_payout']=str(receipt_payout)
                        payout=resolution_share_payout(entry)
                        entry['payout_source']='resolved_shares' if payout is not None else 'receipt_transfer'
                        if payout is None:payout=receipt_payout
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    entry['redemption_retry_error']=type(exc).__name__
                    persist();await asyncio.sleep(15)
            risk.record_resolution(payout,cost,slug)
            if combined_mode:
                try:state['available_budget']=str(await collateral_available(client))
                except Exception:state['available_budget']=str(D(str(state['available_budget']))+payout)
            entry.update(stage='resolved',payout=str(payout),net_pnl=str(payout-cost),
                         settled_at_ms=int(time.time()*1000))
            state['settled_entries']+=1
            persist()
            print(json.dumps({'stage':'resolved','entry':entry['entry_id'],'net_pnl':entry['net_pnl'],
                              'session_pnl':str(risk.realized_pnl)}),flush=True)
        def position_done(task):
            position_tasks.discard(task)
            if task.cancelled():return
            error=task.exception()
            if error is not None:position_errors.append(error)
        while risk.intents < target_entries:
            if position_errors:raise position_errors[0]
            stop_file = getattr(args, 'stop_file', '')
            if stop_file and Path(stop_file).exists():
                state['stage']='stopped';break
            try:
                item = await asyncio.wait_for(stream.get(), timeout=1)
            except asyncio.TimeoutError:
                try:reset_mtime=stats_resets_path.stat().st_mtime_ns
                except OSError:reset_mtime=0
                if reset_mtime!=last_reset_mtime:
                    last_reset_mtime=reset_mtime;persist()
                continue
            if item is None:
                if any(code != 0 for code in state.get('engine_exits',{}).values()):
                    raise ExecutionStopped('Signal engine failed')
                state['stage']='time_limit_reached';break
            if stop_file and Path(stop_file).exists():
                state['stage']='stopped';break
            source,event=item
            source_up60=source.endswith('-up60')
            source_down35=source=='sol-down35'
            source_preopen=source.endswith('-preopen')
            source_clip5=source.endswith('-clip5')
            source_clip15=source.endswith('-clip15')
            source_early3m5=source.endswith('-early3m5')
            source_early3m=source.endswith('-early3m') and not source_early3m5
            entry_strategy=('down35' if source_down35 else 'up60' if source_up60 else
                            'preopen' if source_preopen else 'clip5' if source_clip5 else
                            'clip15' if source_clip15 else 'early3m5' if source_early3m5 else
                            'early3m' if source_early3m else 'kraken')
            signal=event['signal'];row=cached.get(signal['slug'])
            signal_budget=entry_budget
            early3m_hedge = ((source_early3m or source_early3m5)
                             and signal.get('scenario') == 'hedge_merge')
            early3m_interval = 300 if source_early3m5 else 900
            try:
                slug_parts=signal['slug'].split('-')
                signal_asset=slug_parts[0]
                signal_interval=int(slug_parts[2][:-1])*60 if len(slug_parts)>3 and slug_parts[2].endswith('m') else 0
                special = (source_up60 or source_down35 or source_preopen or source_clip5
                           or source_clip15 or source_early3m or source_early3m5)
                signal_config=configs.get((signal_asset,signal_interval)) if calibrated_mode and not special else None
                if source_down35:
                    validate_down35_signal(signal,time.time()*1000);signal_budget=D('6.25')
                elif source_clip5 or source_clip15:
                    validate_clip_signal(signal,time.time()*1000,300 if source_clip5 else 900)
                elif source_early3m or source_early3m5:
                    validate_early3m_signal(signal,time.time()*1000,early3m_interval)
                    if early3m_hedge:
                        signal_budget = signal_budget * D(2)
                elif calibrated_mode and not source_up60 and not source_preopen:
                    if not signal_config:raise ExecutionStopped('Signal asset has no validated configuration')
                    quality_config.validate(signal,time.time()*1000,signal_config)
                    if signal_config.get('version') == 3:
                        signal_budget=quality_config.position_budget(signal_config,signal['quality']['probability_lower'])
                else:validate_strategy_signal(signal,time.time()*1000,trader_clone or source_preopen,up60_mode or source_up60)
                if not row or not row.get('acceptingOrders') or row.get('closed'):
                    raise ExecutionStopped('Market metadata unavailable')
                tokens=json.loads(row['clobTokenIds']) if isinstance(row['clobTokenIds'],str) else row['clobTokenIds']
                labels=json.loads(row['outcomes']) if isinstance(row['outcomes'],str) else row['outcomes']
                if dict(zip(labels,tokens)).get(signal['outcome']) != signal['token']:
                    raise ExecutionStopped('Signal token mismatch')
                # The configured cap plus the fee-adjusted edge gate bounds executable price.
                price_cap = (D('.32') if source_down35 else D('.65') if source_clip5 or source_clip15
                             else (D('.90') if early3m_hedge else D('.62')) if source_early3m or source_early3m5
                             else D(str(signal_config['max_ask'])) if signal_config
                             else (D('0.62') if up60_mode or source_up60 else D('0.51') if trader_clone or source_preopen else D('0.47')))
                if D(str(signal['ask'])) > price_cap:
                    raise ExecutionStopped('Entry not affordable at five-share minimum')
                if signal['slug'] in risk.markets:
                    raise ExecutionStopped('Duplicate market')
                for token in tokens:
                    if await token_balance(client,token) != 0:
                        raise ExecutionStopped('Existing holdings in this market')
                if source_down35:
                    validate_down35_signal(signal,time.time()*1000)
                elif source_clip5 or source_clip15:
                    validate_clip_signal(signal,time.time()*1000,300 if source_clip5 else 900)
                elif source_early3m or source_early3m5:
                    validate_early3m_signal(signal,time.time()*1000,early3m_interval)
                elif signal_config:
                    if '_quality_fee' not in row:raise ExecutionStopped('Missing fee metadata')
                    quality_config.validate(signal,time.time()*1000,signal_config,row['_quality_fee'])
                else:validate_strategy_signal(signal,time.time()*1000,trader_clone or source_preopen,up60_mode or source_up60)
                if combined_mode:
                    available = await collateral_available(client)
                    state['available_budget'] = str(available)
                    minimum = D(str(row.get('orderMinSize',5)))
                    if source_down35:
                        signal_budget=D('6.25')
                        if signal_budget > available*D('.30'):
                            raise ExecutionStopped('SOL $5 position exceeds 30% exposure limit')
                    elif sizing_mode == 'minimum':
                        signal_budget = minimum_reservation(
                            minimum,D(str(signal['ask']))+D('0.01'))
                        if signal_budget > available * D('0.30'):
                            raise ExecutionStopped('Market minimum exceeds 30% exposure limit')
                    else:
                        signal_budget = dynamic_reservation(available, signal_budget)
                    if signal_budget * D('0.8') / (D(str(signal['ask'])) + D('0.01')) < minimum:
                        raise ExecutionStopped('Dynamic budget cannot cover market minimum')
                    if all_mode:
                        now_ms=int(time.time()*1000)
                        cooldown=loss_cooldown(history_records.values(),entry_strategy,now_ms)
                        if cooldown:
                            raise ExecutionStopped(
                                f'Risk balance: {entry_strategy} cooling down for {(cooldown+999)//1000}s after 3 losses')
                        active_stages=('buy_intent','exit_intent','holding','redemption_intent')
                        strategy_exposure=sum((D(str(item.get('buy_cost',item.get('reserved_budget',0))))
                                               for item in state['entries']
                                               if item.get('strategy')==entry_strategy
                                               and item.get('stage') in active_stages),D(0))
                        capital=available+sum(risk.open_costs.values(),D(0))
                        required=(D('6.25') if source_down35 else
                                  minimum_reservation(minimum,D(str(signal['ask']))+D('.01')))
                        balanced=balanced_reservation(
                            capital,strategy_exposure,signal_budget,required)
                        if balanced<=0:
                            raise ExecutionStopped(
                                f'Risk balance: {entry_strategy} reached its 15% concentration limit')
                        signal_budget=balanced
            except ExecutionStopped as exc:
                reason=str(exc);state['skip_reasons'][reason]=state['skip_reasons'].get(reason,0)+1
                if all_mode and reason.startswith('Risk balance:'):
                    controls=state['risk_controls'];controls['skips'][reason]=controls['skips'].get(reason,0)+1
                    controls['last_skip']={'reason':reason,'at_ms':int(time.time()*1000)}
                state['skipped']+=1;persist();continue
            risk.reserve(signal['slug'],signal_budget,
                         available_budget=available if combined_mode else None)
            entry={'signal':signal,'condition_id':row['conditionId'],'stage':'buy_intent',
                   'reserved_budget':str(signal_budget),
                   'strategy':entry_strategy,'source':source,
                   'scenario':signal.get('scenario') or 'directional',
                   'opposite_token':dict(zip(labels,tokens)).get('Up') if source_down35 else None,
                   'entry_id':f'{path.name}:{len(state["entries"])+state["archived_entries"]}'}
            state['entries'].append(entry);state['stage']='buy_intent';persist()
            if up60_mode or source_up60:
                await asyncio.sleep(max(0,(signal['received_ms']+200-time.time()*1000)/1000))
            if early3m_hedge:
                # Riskier (primary) limit first, then hedge sized to the primary partial fill, then CTF merge.
                legs = signal['legs']
                per_leg = (signal_budget * D('0.8') / D(2)).quantize(D('0.01'), rounding=ROUND_DOWN)
                primary_ask = D(str(legs[0]['ask']))
                hedge_ask = D(str(legs[1]['ask']))
                primary_size = (per_leg / primary_ask).quantize(D('0.01'), rounding=ROUND_DOWN)
                fill_timeout = min(90.0, max(15.0, (signal['end_ms'] / 1000) - time.time() - 60))
                primary_filled = D(0)
                hedge_filled = D(0)
                try:
                    primary_base = await token_balance(client, legs[0]['token'])
                    primary_resp = await place_limit_buy(
                        client, args.env, legs[0]['token'], primary_ask + D('0.01'), primary_size)
                    entry['primary_buy'] = encode_response(primary_resp); persist()
                    if not primary_resp.ok:
                        raise OrderNotSubmitted('Primary limit rejected: ' + str(primary_resp.code))
                    primary_filled = await wait_limit_fill(
                        client, primary_resp.order_id, legs[0]['token'], primary_base, primary_size,
                        timeout_s=fill_timeout)
                    try:
                        await client.cancel_order(order_id=primary_resp.order_id)
                    except Exception:
                        pass
                    if primary_filled <= 0:
                        raise OrderNotSubmitted('Primary limit did not fill')
                    hedge_base = await token_balance(client, legs[1]['token'])
                    hedge_resp = await place_limit_buy(
                        client, args.env, legs[1]['token'], hedge_ask + D('0.01'), primary_filled)
                    entry['hedge_buy'] = encode_response(hedge_resp); persist()
                    if hedge_resp.ok:
                        hedge_filled = await wait_limit_fill(
                            client, hedge_resp.order_id, legs[1]['token'], hedge_base, primary_filled,
                            timeout_s=fill_timeout)
                        try:
                            await client.cancel_order(order_id=hedge_resp.order_id)
                        except Exception:
                            pass
                    matched = min(primary_filled, hedge_filled)
                    entry['primary_filled'] = str(primary_filled)
                    entry['hedge_filled'] = str(hedge_filled)
                    entry['filled_shares'] = str(matched if matched > 0 else primary_filled)
                    cost = (primary_filled * primary_ask + hedge_filled * hedge_ask).quantize(
                        D('0.0001'), rounding=ROUND_DOWN)
                    risk.record_fill(cost, signal['slug'])
                    entry['buy_cost'] = str(cost)
                    state['confirmed_entries'] += 1
                    if matched > 0:
                        amount_units = int((matched * UNITS).to_integral_value(rounding=ROUND_DOWN))
                        merge_result = await merge_positions(
                            client, args.env, row['conditionId'], amount=amount_units or 'max')
                        entry['merge'] = {
                            'status': getattr(merge_result, 'status', str(merge_result)),
                            'matched_shares': str(matched),
                        }
                        payout = matched
                        risk.record_resolution(payout, cost, signal['slug'])
                        entry.update(stage='resolved', payout=str(payout),
                                     net_pnl=str(payout - cost), exit_reason='merge',
                                     settled_at_ms=int(time.time() * 1000))
                        state['settled_entries'] = state.get('settled_entries', 0) + 1
                        if combined_mode:
                            state['available_budget'] = str(max(D(0), available - cost + payout))
                        state['stage'] = 'monitor'; persist()
                        print(json.dumps({'stage': 'merged', 'entry': risk.intents,
                                          'buy_cost': str(cost), 'payout': str(payout),
                                          'slug': signal['slug']}), flush=True)
                    else:
                        # Hedge missed: hold the primary leg through resolution.
                        entry['stage'] = 'holding'
                        entry['hedge_partial'] = True
                        if combined_mode:
                            state['available_budget'] = str(max(D(0), available - cost))
                        state['stage'] = 'monitor'; persist()
                        print(json.dumps({'stage': 'holding_unhedged', 'entry': risk.intents,
                                          'buy_cost': str(cost), 'slug': signal['slug']}), flush=True)
                        task = asyncio.create_task(resolve_position(entry, cost))
                        position_tasks.add(task); task.add_done_callback(position_done)
                except OrderNotSubmitted as exc:
                    if primary_filled > 0:
                        # Inventory already exists; keep the reservation converted to an open cost.
                        cost = (primary_filled * primary_ask).quantize(D('0.0001'), rounding=ROUND_DOWN)
                        if signal['slug'] in risk.reservations:
                            risk.record_fill(cost, signal['slug'])
                        entry.update(stage='holding', buy_cost=str(cost),
                                     filled_shares=str(primary_filled), hedge_partial=True,
                                     reason=str(exc))
                        state['confirmed_entries'] += 1
                        state['stage'] = 'monitor'; persist()
                        task = asyncio.create_task(resolve_position(entry, cost))
                        position_tasks.add(task); task.add_done_callback(position_done)
                    else:
                        risk.cancel_before_submission(signal['slug'])
                        reason = str(exc)
                        entry.update(stage='not_submitted', reason=reason)
                        state['stage'] = 'monitor'
                        state['skip_reasons'][reason] = state['skip_reasons'].get(reason, 0) + 1
                        state['skipped'] += 1
                        persist()
                continue

            try:
                response=await buy(client,args.env,signal['token'],signal_budget,D(str(signal['ask']))+D('0.01'),
                    deadline_ms=signal['end_ms'],signal_received_ms=signal['received_ms'])
            except OrderNotSubmitted as exc:
                risk.cancel_before_submission(signal['slug'])
                reason=str(exc)
                entry.update(stage='not_submitted',reason=reason)
                state['stage']='monitor'
                state['skip_reasons'][reason]=state['skip_reasons'].get(reason,0)+1
                state['skipped']+=1
                persist()
                continue
            entry['buy']=encode_response(response);persist()
            if not response.ok:
                risk.cancel_before_submission(signal['slug'])
                reason='Buy rejected: '+str(response.code)
                entry.update(stage='not_submitted',reason=reason)
                state['stage']='monitor'
                state['skip_reasons'][reason]=state['skip_reasons'].get(reason,0)+1
                state['skipped']+=1;persist();continue
            entry['buy_hashes']=await settle(client,response);persist()
            cost=-await flows(entry['buy_hashes'],env['POLY_FUNDER'])
            risk.record_fill(cost,signal['slug']);entry['buy_cost']=str(cost)
            entry['filled_shares']=str(response.taking_amount)
            state['confirmed_entries']+=1
            if combined_mode:state['available_budget']=str(max(D(0),available-cost))
            entry['stage']='holding';state['stage']='monitor';persist()
            print(json.dumps({'stage':'holding','entry':risk.intents,'buy_cost':str(cost),'slug':signal['slug']}),flush=True)
            task=asyncio.create_task(resolve_position(entry,cost))
            position_tasks.add(task);task.add_done_callback(position_done)
        if position_errors:raise position_errors[0]
        terminal_stage=state['stage']
        if position_tasks:
            state['stage']='draining_positions';persist()
            await asyncio.gather(*tuple(position_tasks))
            state['stage']=terminal_stage
        if risk.intents==target_entries:
            state['stage']='completed'
    except asyncio.CancelledError:
        state.update(stage='interrupted', reason='Interrupted; reconcile any pending intent or held position before restart')
    except Exception as exc:
        state.update(stage='requires_attention',error_type=type(exc).__name__)
        if isinstance(exc,ExecutionStopped):state['reason']=str(exc)
    finally:
        for task in tuple(position_tasks):task.cancel()
        if position_tasks:await asyncio.gather(*tuple(position_tasks),return_exceptions=True)
        persist()
        if cache_task:cache_task.cancel()
        for task in stream_tasks:task.cancel()
        for _,process in processes:
            if process.returncode is None:
                process.terminate();await process.wait()
        if stream_tasks:await asyncio.gather(*stream_tasks,return_exceptions=True)
        if client:await client.close()
        lock.close()
        wallet_lock.close()
        print(json.dumps(state,indent=2),flush=True)
    return state['stage'] not in ('requires_attention',)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--live',action='store_true');p.add_argument('--preflight',action='store_true')
    p.add_argument('--env',default='.env');p.add_argument('--seconds',type=int,default=7200)
    p.add_argument('--state',default='logs/ten-state.json');p.add_argument('--journal',default='logs/ten-signals.jsonl')
    p.add_argument('--binary',default='target/release/leadlag')
    p.add_argument('--stop-file',default='',help='Request a safe stop before the next entry')
    p.add_argument('--recover-state',action='append',default=[],
                   help='Reconcile an older FOK intent/holding before accepting new signals')
    p.add_argument('--history',default='',help='Append-only settled-trade history shared across sessions')
    p.add_argument('--portfolio-state',default='',help='Small cumulative live statistics snapshot')
    p.add_argument('--stats-resets',default='',help='Per-strategy dashboard statistics baselines')
    p.add_argument('--kraken-only',action='store_true',help='Use Kraken trigger without Deribit/Binance confirmation')
    p.add_argument('--trader-clone',action='store_true',help='BTC 15m pre-open follow ≥15bp, ask 0.49–0.51, T-200s')
    p.add_argument('--kraken-quality',action='store_true',help='BTC 5m within-market direction, train-calibrated confidence and fee-adjusted edge')
    p.add_argument('--wallet-replica',action='store_true',help='Bounded Kraken-only reconstruction of wallet 0x943c...77c2')
    p.add_argument('--kraken-trend',action='store_true',help='Calibrated BTC/ETH 15m EMA9/21 + RSI14 trend strategy')
    p.add_argument('--up60',action='store_true',help='BTC 15m Up at ask 0.58–0.62 with Kraken +7.5bp confirmation')
    p.add_argument('--down35',action='store_true',help='SOL 5m Down <=0.32 with Kraken -10bp confirmation, TP 0.65, SL when Up >=0.80')
    p.add_argument('--combined',action='store_true',
                   help='Run the selected BTC feeds together with dynamic 30%% exposure control')
    p.add_argument('--portfolio-strategy',
                   choices=('kraken','up60','preopen','clip5','clip15','early3m','early3m5','down35','combined','all'),
                   default='combined',
                   help='With --combined, enable one strategy or all BTC feeds including early3m/early3m5')
    p.add_argument('--sizing-mode',choices=('minimum','adaptive'),default='minimum',
                   help='With --combined, use market-minimum orders or bankroll-adaptive sizing')
    p.add_argument('--include-5m',action='store_true',help='With --kraken-trend, also run bounded BTC/ETH 5m replica engines')
    p.add_argument('--quality-config',default='kraken-quality.json')
    p.add_argument('--eth-quality-config',default='kraken-eth-replica.json')
    p.add_argument('--btc-5m-config',default='kraken-replica.json')
    p.add_argument('--eth-5m-config',default='kraken-eth-replica.json')
    a=p.parse_args()
    try:ok=asyncio.run(run(a))
    except (ExecutionStopped,BlockingIOError) as exc:print(str(exc));ok=False
    raise SystemExit(0 if ok else 1)
