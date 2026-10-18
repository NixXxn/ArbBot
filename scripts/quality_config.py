"""Independent execution-side checks for the Kraken quality model."""
from decimal import Decimal
import json
import math
from pathlib import Path

from live_executor import ExecutionStopped


def load(path):
    config = json.loads(Path(path).read_text())
    version = config.get('version')
    config.setdefault('asset','btc')
    expected_timing = (900,720) if version == 3 else (300,60)
    expected_window = 30 if version == 3 else 5
    if config['asset'] not in ('btc','eth') or version not in (1, 2, 3) \
            or (config.get('interval_seconds'),config.get('remaining_seconds')) != expected_timing \
            or config.get('entry_window_seconds',5) != expected_window \
            or config.get('volatility_minutes') != 60 or config.get('proxy_validation_passed') is not True:
        raise ExecutionStopped('Unexpected or unvalidated quality configuration')
    ranges = {'min_z':((0 if version in (2,3) else .5),10),
              'min_move_bp':((1 if version == 3 else 5 if version == 2 else 2),1000),'volatility_floor':(.0001,.01),
              'min_probability':((.55 if version == 3 else .80 if version == 2 else .65),.99),
              'min_edge':((.005 if version == 3 else .02 if version == 2 else .03),.2),
              'max_ask':(.1,(.80 if version == 3 else .77 if version == 2 else .59)),'fee_rate_ceiling':(.07,.10),
              'probability_haircut':((.02 if version in (2,3) else .10),(.10 if version == 3 else .15 if version == 2 else .25))}
    for key,(low,high) in ranges.items():
        value = config.get(key)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not low <= value <= high:
            raise ExecutionStopped('Invalid quality parameter: '+key)
    min_ask = config.get('min_ask', .1)
    if not isinstance(min_ask,(int,float)) or not math.isfinite(min_ask) or not .1 <= min_ask <= config['max_ask']:
        raise ExecutionStopped('Invalid quality parameter: min_ask')
    if version == 3:
        if min_ask < .20 or config.get('order_notional') != 4.0 \
                or config.get('entry_budget') != 5.0 or config.get('target_entries') != 6 \
                or (config.get('ema_fast'),config.get('ema_slow'),config.get('rsi_period')) != (9,21,14):
            raise ExecutionStopped('Invalid 15-minute trend execution bounds')
        tiers=config.get('position_tiers',[])
        prior_probability=0.0; prior_budget=0.0
        if len(tiers) < 2:
            raise ExecutionStopped('Invalid 15-minute position tiers')
        for tier in tiers:
            try:
                floor=float(tier['min_probability']); budget=float(tier['budget'])
            except (KeyError, TypeError, ValueError) as error:
                raise ExecutionStopped('Invalid 15-minute position tiers') from error
            if (not math.isfinite(floor) or not math.isfinite(budget)
                    or floor < config['min_probability'] or floor <= prior_probability
                    or budget < 4.0 or budget > 5.0 or budget <= prior_budget):
                raise ExecutionStopped('Invalid 15-minute position tiers')
            prior_probability=floor; prior_budget=budget
        boundary = config['min_move_bp']
        buckets = config.get('move_probability_buckets',[])
        lower_key, upper_key = 'min_move_bp', 'max_move_bp'
    elif version == 2:
        if min_ask < .65 or config.get('order_notional') != 4.0 \
                or config.get('entry_budget') != 5.0 or config.get('target_entries') != 6:
            raise ExecutionStopped('Invalid wallet-replica execution bounds')
        boundary = config['min_move_bp']
        buckets = config.get('move_probability_buckets',[])
        lower_key, upper_key = 'min_move_bp', 'max_move_bp'
    else:
        boundary = config['min_z']
        buckets = config.get('probability_buckets',[])
        lower_key, upper_key = 'min_z', 'max_z'
    if not buckets:
        raise ExecutionStopped('Missing probability calibration')
    for index,bucket in enumerate(buckets):
        probability = bucket['probability_lower']
        if bucket[lower_key] != boundary or not math.isfinite(probability) or not 0 <= probability <= .99 or (bucket['samples'] < 100 and probability > 0):
            raise ExecutionStopped('Invalid probability calibration')
        upper = bucket[upper_key]
        if upper is None:
            if index != len(buckets)-1:
                raise ExecutionStopped('Unbounded calibration bucket must be last')
        elif not math.isfinite(upper) or upper <= boundary:
            raise ExecutionStopped('Invalid calibration bounds')
        else:
            boundary = upper
    return config


def validate(signal, now_ms, config, fee=None):
    start = int(signal['slug'].rsplit('-',1)[1])*1000
    interval = config['interval_seconds']*1000
    prefix = f"{config['asset']}-updown-{config['interval_seconds']//60}m-"
    if not signal['slug'].startswith(prefix) or start % interval or signal['outcome'] not in ('Up','Down') or signal['end_ms'] != start+interval:
        raise ExecutionStopped('Unexpected quality market identity')
    remaining = config['remaining_seconds']*1000
    window = config.get('entry_window_seconds',5)*1000
    if not 0 <= now_ms-signal['received_ms'] <= 500 or not remaining-window-1000 <= signal['end_ms']-now_ms <= remaining:
        raise ExecutionStopped('Quality signal is stale or outside its entry window')
    quality = signal.get('quality',{})
    values = [signal['ask'],quality.get('z'),quality.get('move_bp'),quality.get('probability_lower'),
              quality.get('fee_rate_ceiling'),quality.get('min_edge')]
    if config['version'] == 3:values.append(quality.get('suggested_budget'))
    if any(not isinstance(value,(int,float)) or not math.isfinite(value) for value in values):
        raise ExecutionStopped('Missing or invalid signal quality')
    z, probability = quality['z'], quality['probability_lower']
    if config['version'] in (2,3):
        metric, buckets, lower_key, upper_key = abs(quality['move_bp']), config['move_probability_buckets'], 'min_move_bp', 'max_move_bp'
    else:
        metric, buckets, lower_key, upper_key = z, config['probability_buckets'], 'min_z', 'max_z'
    bucket = next((b for b in buckets if metric >= b[lower_key] and (b[upper_key] is None or metric < b[upper_key])),None)
    if bucket is None or abs(probability-max(0,bucket['probability_lower']-config['probability_haircut'])) > 1e-12 or probability < config['min_probability'] or z < config['min_z'] or abs(quality['move_bp']) < config['min_move_bp']:
        raise ExecutionStopped('Quality calibration or confidence mismatch')
    if (quality['move_bp'] > 0) != (signal['outcome'] == 'Up') or quality['fee_rate_ceiling'] != config['fee_rate_ceiling'] or quality['min_edge'] != config['min_edge']:
        raise ExecutionStopped('Quality direction or risk parameter mismatch')
    if config['version'] == 3 and Decimal(str(quality.get('suggested_budget'))) != position_budget(config,probability):
        raise ExecutionStopped('Trend position size mismatch')
    price = Decimal(str(signal['ask']))+Decimal('.01')
    rate = Decimal(str(config['fee_rate_ceiling']))
    if not config.get('min_ask',.1) <= signal['ask'] <= config['max_ask'] or Decimal(str(probability)) < price+rate*price*(1-price)+Decimal(str(config['min_edge'])):
        raise ExecutionStopped('Insufficient fee-adjusted entry edge or budget')
    if fee is not None:
        if fee.get('checked_at_ms',0) < now_ms-60_000 or fee.get('exponent') != 1 or not 0 <= Decimal(str(fee.get('rate','NaN'))) <= rate:
            raise ExecutionStopped('Fee metadata is stale, unsupported, or above the ceiling')


def position_budget(config, probability):
    budget = config.get('order_notional',4.0)
    for tier in config.get('position_tiers',[]):
        if probability >= tier['min_probability']:
            budget = tier['budget']
    return Decimal(str(budget))


def fee_info(payload, condition_id, now_ms):
    if payload.get('c') != condition_id or not isinstance(payload.get('fd'),dict):
        raise ExecutionStopped('Missing market fee metadata')
    fee = payload['fd']
    rate, exponent = Decimal(str(fee.get('r'))), Decimal(str(fee.get('e')))
    if not rate.is_finite() or not exponent.is_finite():
        raise ExecutionStopped('Invalid fee metadata')
    return {'rate':str(rate),'exponent':int(exponent) if exponent == 1 else str(exponent),
            'checked_at_ms':now_ms}
