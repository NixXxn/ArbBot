"""Chronological Kraken proxy evaluation. No orders and no Polymarket fill backtest."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time

import requests

URL = 'https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/1m'


def fetch(days, path):
    end = int(time.time()) // 900 * 900
    cursor = end - days * 86400
    candles = {}
    while cursor < end:
        response = requests.get(URL, params={'from': cursor, 'to': end-60, 'count': 7000}, timeout=30)
        response.raise_for_status()
        data = response.json()
        rows = data['candles']
        if not rows:
            break
        for row in rows:
            if row['time'] < end*1000:
                candles[row['time']] = row
        following = max(row['time'] for row in rows)//1000 + 60
        if following <= cursor:
            raise ValueError('Kraken pagination did not advance')
        cursor = following
        if not data.get('more_candles'):
            break
    result = {'candles': [candles[key] for key in sorted(candles)], 'more_candles': False,
              'requested_from': end-days*86400, 'requested_to': end}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, separators=(',', ':'))+'\n')
    return result


def wilson(wins, count):
    if not count:
        return 0.0
    p, z = wins/count, 1.96
    return (p+z*z/(2*count)-z*math.sqrt(p*(1-p)/count+z*z/(4*count*count)))/(1+z*z/count)


def observations(candles, interval, remaining):
    by_time = {row['time']//1000: row for row in candles}
    result = []
    for start in sorted(by_time):
        if start % interval:
            continue
        entry = start + interval - remaining
        required = range(entry-60*61, start+interval, 60)
        if any(second not in by_time for second in required):
            continue
        opening = float(by_time[start]['open'])
        price = float(by_time[entry-60]['close'])
        last_price = float(by_time[entry-120]['close'])
        closes = [float(by_time[second]['close']) for second in range(entry-60*61, entry, 60)]
        returns = [math.log(b/a) for a, b in zip(closes, closes[1:])]
        sigma = max(statistics.stdev(returns), 0.0001)
        move = math.log(price/opening)
        previous_move = math.log(last_price/opening)
        final = float(by_time[start+interval-60]['close'])
        result.append({'start': start, 'entry': entry, 'move_bp': abs(move)*10000,
                       'z': abs(move)/(sigma*math.sqrt(remaining/60)),
                       'stable': move*previous_move > 0,
                       'direction': 'Up' if move > 0 else 'Down',
                       'won': (move > 0) == (final >= opening)})
    return result


def evaluate(rows, min_z, min_move):
    selected = [row for row in rows if row['z'] >= min_z and row['move_bp'] >= min_move and row['stable']]
    wins = sum(row['won'] for row in selected)
    days = (rows[-1]['start']-rows[0]['start']+300)/86400 if rows else 0
    return {'eligible_markets': len(rows), 'signals': len(selected), 'wins': wins,
            'win_rate': wins/len(selected) if selected else None,
            'wilson_95_lower': wilson(wins, len(selected)),
            'coverage': len(selected)/len(rows) if rows else 0,
            'signals_per_day_proxy': len(selected)/days if days else 0}


def optimize(candles):
    first = min(row['time'] for row in candles)//1000
    last = max(row['time'] for row in candles)//1000
    split = first + int((last-first)*2/3)
    candidates = []
    for interval, remaining in [(300,60), (300,120), (900,60), (900,180)]:
        rows = observations(candles, interval, remaining)
        train = [row for row in rows if row['start']+interval <= split]
        for min_z in [0.5, 0.75, 1.0, 1.25]:
            for min_move in [2.0, 5.0]:
                metrics = evaluate(train, min_z, min_move)
                # Prefer at least half the available markets, with a lower-bound win
                # rate of 65%. Choose on train only; the final third stays untouched.
                if metrics['signals'] >= 200 and metrics['coverage'] >= .50 and metrics['wilson_95_lower'] >= .65:
                    candidates.append({'interval_seconds': interval, 'remaining_seconds': remaining,
                                       'min_z': min_z, 'min_move_bp': min_move, 'train': metrics})
    if not candidates:
        raise ValueError('No candidate meets the predeclared quality and frequency gates')
    chosen = max(candidates, key=lambda row: row['train']['signals_per_day_proxy'])
    rows = observations(candles, chosen['interval_seconds'], chosen['remaining_seconds'])
    test_rows = [row for row in rows if row['start'] >= split]
    test = evaluate(test_rows, chosen['min_z'], chosen['min_move_bp'])
    train = [row for row in rows if row['start']+chosen['interval_seconds'] <= split]
    edges = [chosen['min_z'], 1.0, 1.5, 2.0, 3.0]
    edges = sorted(set(edge for edge in edges if edge >= chosen['min_z']))
    buckets = []
    for index, edge in enumerate(edges):
        upper = edges[index+1] if index+1 < len(edges) else float('inf')
        bucket = [row for row in train if edge <= row['z'] < upper and row['move_bp'] >= chosen['min_move_bp'] and row['stable']]
        count, wins = len(bucket), sum(row['won'] for row in bucket)
        # A sparse bin never inherits a more favorable neighboring probability.
        buckets.append({'min_z': edge, 'max_z': None if math.isinf(upper) else upper,
                        'samples': count, 'wins': wins,
                        'probability_lower': wilson(wins, count) if count >= 100 else 0.0})
    report = {'data_source': URL, 'candles': len(candles),
              'first_utc': datetime.fromtimestamp(first, timezone.utc).isoformat(),
              'last_utc': datetime.fromtimestamp(last, timezone.utc).isoformat(),
              'split_utc': datetime.fromtimestamp(split, timezone.utc).isoformat(),
              'selection': 'train only: largest frequency subject to n>=200, coverage>=50%, Wilson lower>=65%',
              'chosen': chosen, 'test': test, 'probability_buckets_train_only': buckets,
              'test_gate_passed': test['signals'] >= 100 and test['wilson_95_lower'] >= .65,
              'label_source': 'Kraken trade candles, not Polymarket settlement',
              'net_pnl_backtested': False, 'live_win_rate_established': False,
              'limitations': ['No historical executable Polymarket order books or fill simulation.',
                             'Kraken candle direction is a proxy; Polymarket uses its own resolution source.',
                             'Candles contain trades; live signals use order-book midpoints.',
                             'Adjacent windows are dependent; Wilson intervals assume independence.',
                             'Signal counts exclude affordability, fees, liquidity, and held-position skips.']}
    config = {'version': 1, 'strategy': 'kraken_quality',
              **{key: chosen[key] for key in ('interval_seconds','remaining_seconds','min_z','min_move_bp')},
              'volatility_minutes': 60, 'volatility_floor': .0001,
              'min_probability': .65, 'min_edge': .03, 'max_ask': .59,
              'fee_rate_ceiling': .10, 'probability_haircut': .10,
              'probability_buckets': buckets,
              'proxy_validation_passed': report['test_gate_passed'],
              'live_profitability_established': False}
    return report, config


def crosscheck(candles, report, count, output):
    chosen = report['chosen']
    split = datetime.fromisoformat(report['split_utc']).timestamp()
    rows = [row for row in observations(candles, chosen['interval_seconds'], chosen['remaining_seconds'])
            if row['start'] >= split and row['stable'] and row['z'] >= chosen['min_z']
            and row['move_bp'] >= chosen['min_move_bp']]
    # Evenly spaced test-period observations, chosen without looking at outcomes.
    size = min(count,len(rows))
    selected = [rows[int(index*(len(rows)-1)/(size-1))] for index in range(size)] if size > 1 else rows[:size]
    def lookup(row):
        slug = f"btc-updown-{chosen['interval_seconds']//60}m-{row['start']}"
        response = requests.get('https://gamma-api.polymarket.com/markets',
                                params={'slug':slug,'closed':'true'}, timeout=20)
        response.raise_for_status()
        markets = response.json()
        if len(markets) != 1 or markets[0].get('slug') != slug:
            return {**row,'slug':slug,'actual_result':None,'reason':'market_missing'}
        market = markets[0]
        labels, prices = market['outcomes'], market['outcomePrices']
        labels = json.loads(labels) if isinstance(labels,str) else labels
        prices = json.loads(prices) if isinstance(prices,str) else prices
        if not market.get('closed') or market.get('umaResolutionStatus') != 'resolved' or sorted(float(p) for p in prices) != [0.0,1.0]:
            return {**row,'slug':slug,'actual_result':None,'reason':'not_resolved'}
        outcome = labels[prices.index(next(p for p in prices if float(p)==1.0))]
        tokens = market['clobTokenIds']
        tokens = json.loads(tokens) if isinstance(tokens,str) else tokens
        token = tokens[labels.index(row['direction'])]
        history_response = requests.get('https://clob.polymarket.com/prices-history',
            params={'market':token,'startTs':row['entry']-120,'endTs':row['entry'],'fidelity':1},timeout=20)
        history_response.raise_for_status()
        history = [point for point in history_response.json().get('history',[])
                   if row['entry']-90 <= point['t'] <= row['entry']]
        historical_price = max(history,key=lambda point:point['t'])['p'] if history else None
        return {**row,'slug':slug,'actual_result':outcome,
                'actual_won':outcome==row['direction'],'historical_price_proxy':historical_price}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lookup,selected))
    output.write_text(json.dumps(results,indent=2)+'\n')
    resolved = [row for row in results if row['actual_result'] is not None]
    wins = sum(row['actual_won'] for row in resolved)
    priced = [row for row in resolved if row['historical_price_proxy'] is not None]
    affordable = [row for row in priced if row['historical_price_proxy'] <= .59]
    return {'requested':len(selected),'resolved':len(resolved),'wins':wins,
            'win_rate':wins/len(resolved) if resolved else None,
            'wilson_95_lower':wilson(wins,len(resolved)),
            'proxy_disagreements':sum(row['won'] != row['actual_won'] for row in resolved),
            'selection':'evenly spaced holdout signals, no outcome-based selection',
            'historical_prices_available':len(priced),
            'median_historical_price_proxy':statistics.median(row['historical_price_proxy'] for row in priced) if priced else None,
            'price_proxy_at_or_below_059':len(affordable),
            'wins_in_affordable_price_subset':sum(row['actual_won'] for row in affordable),
            'affordable_subset_edge_established':False,
            'price_warning':'Historical price points are not executable ask quotes; no fill or PnL estimate.',
            'prices_and_fills_verified':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candles', type=Path, default=Path('research/data/kraken-optimization/candles-21d.json'))
    parser.add_argument('--fetch-days', type=int)
    parser.add_argument('--report', type=Path, default=Path('optimization-report.json'))
    parser.add_argument('--config', type=Path, default=Path('kraken-quality.json'))
    parser.add_argument('--crosscheck-markets', type=int, default=0)
    args = parser.parse_args()
    data = fetch(args.fetch_days, args.candles) if args.fetch_days else json.loads(args.candles.read_text())
    report, config = optimize(data['candles'])
    if args.crosscheck_markets:
        report['polymarket_resolution_crosscheck'] = crosscheck(data['candles'],report,args.crosscheck_markets,
                                                               args.candles.parent/'polymarket-crosscheck.json')
    args.report.write_text(json.dumps(report, indent=2)+'\n')
    args.config.write_text(json.dumps(config, indent=2)+'\n')
    print(json.dumps({'chosen': report['chosen'], 'test': report['test'], 'proxy_validation_passed': report['test_gate_passed']}, indent=2))
