"""Run every signal engine with one shared, resolving paper portfolio."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from contextlib import nullcontext
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

import requests

from portfolio_controls import balanced_reservation, loss_cooldown
from session_risk import dynamic_reservation, minimum_reservation
from statistics_resets import load as load_resets, visible as stats_visible


D = Decimal
EXPOSURE_RATE = D("0.30")
STATS_RESET_PATH = None
# Momentum / early-interval feeds outrank price-band clips on the same slug.
STRATEGY_PRIORITY = {"kraken": 3, "up60": 3, "early3m": 3, "early3m5": 3, "preopen": 2, "clip15": 1, "clip5": 1}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2)
        handle.write("\n")
    temporary.replace(path)


def append_event(path, event):
    path = Path(path)
    if path.exists() and path.stat().st_size >= 8 * 1024 * 1024:
        older = path.with_name(path.name + ".2")
        previous = path.with_name(path.name + ".1")
        older.unlink(missing_ok=True)
        if previous.exists():
            previous.replace(older)
        path.replace(previous)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def risk_skip(state,journal,strategy,reason):
    controls=state.setdefault('risk_controls',{})
    skips=controls.setdefault('skips',{})
    skips[reason]=skips.get(reason,0)+1
    controls['last_skip']={'strategy':strategy,'reason':reason,'at_ms':int(time.time()*1000)}
    append_event(journal,{'type':'paper_risk_skip',**controls['last_skip']})


def metrics(rows):
    settled = [row for row in rows if row.get("status") == "settled"]
    pnl = sum((D(str(row["net_pnl"])) for row in settled), D(0))
    cost = sum((D(str(row["cost"])) + D(str(row.get("fee", 0))) for row in settled), D(0))
    win_rows = [row for row in settled if D(str(row["net_pnl"])) > 0]
    loss_rows = [row for row in settled if D(str(row["net_pnl"])) <= 0]
    prices = [D(str(row["price"])) for row in settled if row.get("price") is not None]
    avg_win = (sum((D(str(row["net_pnl"])) for row in win_rows), D(0)) / D(len(win_rows))
               if win_rows else None)
    avg_loss = (sum((D(str(row["net_pnl"])) for row in loss_rows), D(0)) / D(len(loss_rows))
                if loss_rows else None)
    breakeven = None
    if avg_win is not None and avg_loss is not None and avg_win != avg_loss:
        breakeven = float((-avg_loss) / (avg_win - avg_loss))
    return {
        "trades": len(rows), "settled": len(settled), "open": len(rows) - len(settled),
        "wins": len(win_rows),
        "win_rate": float(D(len(win_rows)) / D(len(settled))) if settled else None,
        "roi": float(pnl / cost) if cost else None,
        "pnl": float(pnl), "settled_cost": float(cost),
        "avg_entry": float(sum(prices, D(0)) / D(len(prices))) if prices else None,
        "breakeven_win_rate": breakeven,
    }


def update_metrics(state, reset_path=None):
    rows = state["trades"]
    reset_path = reset_path or STATS_RESET_PATH
    baselines = load_resets(reset_path) if reset_path else {}
    visible_rows = [row for row in rows if stats_visible(row, baselines)]
    tracked = ("kraken", "up60", "preopen", "clip5", "clip15", "early3m", "early3m5")
    active_rows = [row for row in visible_rows if row.get("strategy") in tracked]
    state["strategy_stats"] = {
        name: metrics([row for row in active_rows if row["strategy"] == name])
        for name in tracked
    }
    state["strategy_stats"]["combined"] = metrics(active_rows)
    equity = D(str(state["budget"])) + D(str(state["strategy_stats"]["combined"]["pnl"]))
    exposure = sum((D(str(row["cost"])) + D(str(row.get("fee", 0)))
                    for row in rows if row.get("status") == "open"), D(0))
    state["equity"] = float(equity)
    state["open_exposure"] = float(exposure)
    state["max_exposure"] = float(max(D(0), equity * EXPOSURE_RATE))
    state["exposure_rate"] = float(EXPOSURE_RATE)
    state["updated_at_ms"] = int(time.time() * 1000)


def market(slug):
    response = requests.get("https://gamma-api.polymarket.com/markets",
                            params={"slug": slug}, timeout=10)
    response.raise_for_status()
    rows = response.json()
    if not rows:
        response=requests.get("https://gamma-api.polymarket.com/markets",
                              params={"slug":slug,"closed":"true"},timeout=10)
        response.raise_for_status();rows=response.json()
    return rows[0] if len(rows) == 1 and rows[0].get("slug") == slug else None


def book(token):
    response = requests.get("https://clob.polymarket.com/book",
                            params={"token_id": token}, timeout=10)
    response.raise_for_status()
    payload = response.json()
    asks = [(D(str(level["price"])), D(str(level.get("size", level.get("qty", 0)))))
            for level in payload.get("asks", [])]
    asks = [(price, size) for price, size in asks if price > 0 and size > 0]
    if not asks:
        return None
    ask, size = min(asks)
    return ask, size, D(str(payload.get("min_order_size", 5)))


def down35_exit_quote(held_token,opposite_token,shares):
    def payload(token):
        response=requests.get('https://clob.polymarket.com/book',params={'token_id':token},timeout=10)
        response.raise_for_status();return response.json()
    held,opposite=payload(held_token),payload(opposite_token)
    bids=sorted(((D(str(level['price'])),D(str(level.get('size',level.get('qty',0)))))
                 for level in held.get('bids',[])),reverse=True)
    asks=sorted((D(str(level['price'])) for level in opposite.get('asks',[])))
    bids=[level for level in bids if level[0]>0 and level[1]>0]
    if not bids or not asks:return None
    reason='TP' if bids[0][0]>=D('.65') else 'SL' if asks[0]>=D('.80') else None
    if reason is None:return None
    remaining=D(str(shares));proceeds=D(0)
    for price,size in bids:
        sold=min(remaining,size);proceeds+=sold*price;remaining-=sold
        if remaining<=0:break
    if remaining>0:return None
    return {'reason':reason,'proceeds':proceeds,'price':proceeds/D(str(shares))}


def exit_down35_positions(state,journal,lock=None):
    guard=lock or nullcontext();changed=False
    with guard:
        candidates=[dict(row) for row in state['trades']
                    if row.get('status')=='open' and row.get('strategy')=='down35']
    for candidate in candidates:
        try:
            opposite=candidate.get('opposite_token')
            if not opposite:
                metadata=market(candidate['slug'])
                outcomes=metadata.get('outcomes',[]) if metadata else []
                tokens=metadata.get('clobTokenIds',[]) if metadata else []
                outcomes=json.loads(outcomes) if isinstance(outcomes,str) else outcomes
                tokens=json.loads(tokens) if isinstance(tokens,str) else tokens
                opposite=dict(zip(outcomes,tokens)).get('Up')
                if not opposite:continue
                with guard:
                    row=next((row for row in state['trades'] if row.get('id')==candidate['id']),None)
                    if row is None:continue
                    row['opposite_token']=opposite;candidate['opposite_token']=opposite;changed=True
            result=down35_exit_quote(candidate['token'],opposite,candidate['shares'])
            if result is None:continue
            with guard:
                row=next((row for row in state['trades']
                          if row.get('id')==candidate['id'] and row.get('status')=='open'),None)
                if row is None:continue
                payout=result['proceeds'];net=payout-D(str(row['cost']))-D(str(row.get('fee',0)))
                row.update(status='settled',won=net>0,payout=float(payout),net_pnl=float(net),
                    exit_reason=result['reason'],exit_price=float(result['price']),
                    settled_at_ms=int(time.time()*1000))
                append_event(journal,{'type':'paper_exit',**row});update_metrics(state);changed=True
        except (OSError,ValueError,requests.RequestException):continue
    return changed


def resolve_positions(state, journal, lock=None):
    guard = lock or nullcontext()
    with guard:
        candidates = [dict(row) for row in state["trades"] if row.get("status") == "open"]
    for candidate in candidates:
        try:
            resolved = market(candidate["slug"])
            prices = resolved.get("outcomePrices", []) if resolved else []
            outcomes = resolved.get("outcomes", []) if resolved else []
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            outcome_prices = dict(zip(outcomes, (D(str(value)) for value in prices)))
            if not resolved or not resolved.get("closed") or sorted(outcome_prices.values()) != [D(0), D(1)]:
                continue
            won = outcome_prices.get(candidate["outcome"]) == 1
            payout = D(str(candidate["shares"])) if won else D(0)
            net = payout - D(str(candidate["cost"])) - D(str(candidate["fee"]))
            with guard:
                row = next((item for item in state["trades"]
                            if item.get("id") == candidate.get("id") and item.get("status") == "open"), None)
                if row is None:
                    continue
                row.update(status="settled", won=won, payout=float(payout), net_pnl=float(net),
                           settled_at_ms=int(time.time() * 1000))
                append_event(journal, {"type": "paper_settlement", **row})
        except (OSError, ValueError, requests.RequestException):
            continue
    with guard:
        update_metrics(state)


def drain(label, strategy, process, events):
    for line in process.stdout:
        print(f"[{label}] {line.rstrip()}", flush=True)
        try:
            event = json.loads(line)
        except ValueError:
            continue
        events.put((label, strategy, event))


def slug_blocked(state, strategy, slug, *, allow_own_hedge=False):
    mine = STRATEGY_PRIORITY.get(strategy, 0)
    for row in state["trades"]:
        if row.get("slug") != slug:
            continue
        if allow_own_hedge and row.get("strategy") == strategy and row.get("scenario") == "hedge_merge":
            continue
        if STRATEGY_PRIORITY.get(row.get("strategy"), 0) >= mine:
            return True
    return False


def _paper_leg_fill(state, strategy, signal, journal, leg, reservation, amount, shares_cap=None):
    try:
        current = book(leg["token"])
    except (OSError, ValueError, requests.RequestException):
        return None
    if current is None:
        return None
    ask, ask_size, minimum = current
    if ask > D(str(leg["ask"])) + D("0.01"):
        return None
    shares = min((amount / ask).quantize(D("0.0001"), rounding=ROUND_DOWN), ask_size)
    if shares_cap is not None:
        shares = min(shares, shares_cap)
    if shares < minimum:
        return None
    cost = (shares * ask).quantize(D("0.0001"), rounding=ROUND_DOWN)
    fee = (cost * D("0.02")).quantize(D("0.0001"), rounding=ROUND_DOWN)
    if D(str(state["open_exposure"])) + cost + fee > D(str(state["max_exposure"])):
        return None
    row = {
        "id": f"{signal['slug']}:{strategy}:{leg['outcome']}",
        "strategy": strategy,
        "slug": signal["slug"], "token": leg["token"], "outcome": leg["outcome"],
        "price": float(ask), "shares": float(shares), "cost": float(cost), "fee": float(fee),
        "reservation": float(reservation), "status": "open",
        "opened_at_ms": int(time.time() * 1000),
        "scenario": signal.get("scenario") or "directional",
        "leg_role": leg.get("role"),
        "group_id": f"{signal['slug']}:{strategy}",
    }
    state["trades"].append(row)
    update_metrics(state)
    append_event(journal, {"type": "paper_fill", **row})
    return row


def merge_early3m_paper(state, journal, group_id):
    """Settle matched YES+NO early3m legs as a CTF merge into $1 USDC per pair."""
    legs = [row for row in state["trades"]
            if row.get("group_id") == group_id and row.get("status") == "open"
            and row.get("scenario") == "hedge_merge"]
    if len(legs) < 2:
        return False
    by_outcome = {}
    for row in legs:
        by_outcome.setdefault(row.get("outcome"), []).append(row)
    if "Up" not in by_outcome or "Down" not in by_outcome:
        return False
    up, down = by_outcome["Up"][0], by_outcome["Down"][0]
    matched = min(D(str(up["shares"])), D(str(down["shares"])))
    if matched <= 0:
        return False
    total_cost = D(str(up["cost"])) + D(str(down["cost"]))
    total_fee = D(str(up.get("fee", 0))) + D(str(down.get("fee", 0)))
    up_weight = D(str(up["cost"])) / total_cost if total_cost else D("0.5")
    up_payout = (matched * up_weight).quantize(D("0.0001"), rounding=ROUND_DOWN)
    down_payout = matched - up_payout
    now = int(time.time() * 1000)
    for row, payout in ((up, up_payout), (down, down_payout)):
        net = payout - D(str(row["cost"])) - D(str(row.get("fee", 0)))
        row.update(status="settled", won=net > 0, payout=float(payout), net_pnl=float(net),
                   exit_reason="merge", merged_shares=float(matched), settled_at_ms=now)
        append_event(journal, {"type": "paper_merge", **row})
    update_metrics(state)
    append_event(journal, {"type": "paper_merge_group", "group_id": group_id,
                           "matched_shares": float(matched), "cost": float(total_cost),
                           "fee": float(total_fee), "payout": float(matched),
                           "net_pnl": float(matched - total_cost - total_fee)})
    return True


def execute_signal(state, strategy, signal, journal, lock=None):
    guard = lock or nullcontext()
    scenario = signal.get("scenario") or "directional"
    hedge = strategy in ("early3m", "early3m5") and scenario == "hedge_merge" and signal.get("legs")
    with guard:
        if slug_blocked(state, strategy, signal.get("slug"), allow_own_hedge=False):
            return
    if not 0 <= int(time.time() * 1000) - signal.get("received_ms", 0) <= 1000:
        return
    delay = max(0, signal.get("execution_delay_ms", 20) / 1000)
    if delay:
        time.sleep(delay)

    if hedge:
        legs = list(signal["legs"])
        if len(legs) != 2 or legs[0].get("role") != "primary":
            return
        # Pre-check both books so we never leave an unsecured single leg.
        try:
            primary_book = book(legs[0]["token"])
            hedge_book = book(legs[1]["token"])
        except (OSError, ValueError, requests.RequestException):
            return
        if primary_book is None or hedge_book is None:
            return
        if (primary_book[0] > D(str(legs[0]["ask"])) + D("0.01")
                or hedge_book[0] > D(str(legs[1]["ask"])) + D("0.01")):
            return
        # Risk rule: primary (Kraken-riskier) fills first, then hedge sized to match.
        with guard:
            if slug_blocked(state, strategy, signal.get("slug")):
                return
            update_metrics(state)
            equity = D(str(state["equity"]))
            remaining_exposure = D(str(state["max_exposure"])) - D(str(state["open_exposure"]))
            if equity <= 0 or remaining_exposure <= 0:
                return
            quality = signal.get("quality") or {}
            if state.get("strategy") == "all":
                cooldown = loss_cooldown(state["trades"], strategy, int(time.time() * 1000))
                if cooldown:
                    risk_skip(state, journal, strategy,
                              f"{strategy} cooling down for {(cooldown + 999) // 1000}s after 3 losses")
                    return
            base = D(str(quality.get("suggested_budget", 5)))
            # Pair cost is under $1/share; reserve ~1.1× base rather than 2× (was starving early3m5).
            reservation = min(dynamic_reservation(equity, base * D("1.1")), remaining_exposure)
            if state.get("strategy") == "all":
                current = sum((D(str(row["cost"])) + D(str(row.get("fee", 0)))
                               for row in state["trades"]
                               if row.get("status") == "open" and row.get("strategy") == strategy), D(0))
                required = minimum_reservation(primary_book[2], D(str(legs[0]["ask"]))) + minimum_reservation(
                    hedge_book[2], D(str(legs[1]["ask"])))
                balanced = balanced_reservation(equity, current, reservation, required)
                if balanced <= 0:
                    risk_skip(state, journal, strategy,
                              f"{strategy} reached its 15% concentration limit")
                    return
                reservation = balanced
            pair_ask = D(str(legs[0]["ask"])) + D(str(legs[1]["ask"]))
            if pair_ask <= 0:
                return
            shares = min(
                (reservation * D("0.8") / pair_ask).quantize(D("0.0001"), rounding=ROUND_DOWN),
                primary_book[1], hedge_book[1])
            if shares < primary_book[2] or shares < hedge_book[2]:
                return
            per_leg = (shares * D(str(legs[0]["ask"]))).quantize(D("0.01"), rounding=ROUND_DOWN)
            primary = _paper_leg_fill(state, strategy, signal, journal, legs[0], reservation,
                                      per_leg, shares_cap=shares)
            if primary is None:
                return
            hedge_budget = (shares * D(str(legs[1]["ask"]))).quantize(D("0.01"), rounding=ROUND_DOWN)
            hedge_row = _paper_leg_fill(state, strategy, signal, journal, legs[1], reservation,
                                        hedge_budget, shares_cap=D(str(primary["shares"])))
            if hedge_row is None:
                # Roll back the primary paper fill — never hold an unsecured early3m leg.
                state["trades"][:] = [row for row in state["trades"] if row.get("id") != primary["id"]]
                update_metrics(state)
                append_event(journal, {"type": "paper_hedge_rollback",
                                       "group_id": primary["group_id"],
                                       "filled": primary["outcome"]})
                return
            merge_early3m_paper(state, journal, primary["group_id"])
        return

    try:
        current = book(signal["token"])
    except (OSError, ValueError, requests.RequestException):
        return
    if current is None:
        return
    ask, ask_size, minimum = current
    if ask > D(str(signal["ask"])) + D("0.01"):
        return
    with guard:
        if slug_blocked(state, strategy, signal.get("slug")):
            return
        update_metrics(state)
        equity = D(str(state["equity"]))
        remaining_exposure = D(str(state["max_exposure"])) - D(str(state["open_exposure"]))
        if equity <= 0 or remaining_exposure <= 0:
            return
        quality = signal.get("quality") or {}
        if state.get('strategy')=='all':
            cooldown=loss_cooldown(state['trades'],strategy,int(time.time()*1000))
            if cooldown:
                risk_skip(state,journal,strategy,
                          f'{strategy} cooling down for {(cooldown+999)//1000}s after 3 losses')
                return
        if strategy=='down35':
            reservation=D('6.25')
            if reservation>remaining_exposure:return
            amount=D('5')
            shares=amount/ask
            if shares>ask_size:return
        else:
            base=D(str(quality.get("suggested_budget",5)))
            reservation=min(dynamic_reservation(equity,base),remaining_exposure)
            amount=(reservation*D("0.8")).quantize(D("0.01"),rounding=ROUND_DOWN)
            shares=min((amount/ask).quantize(D("0.0001"),rounding=ROUND_DOWN),ask_size)
        if state.get('strategy')=='all':
            current=sum((D(str(row['cost']))+D(str(row.get('fee',0)))
                         for row in state['trades']
                         if row.get('status')=='open' and row.get('strategy')==strategy),D(0))
            required=D('6.25') if strategy=='down35' else minimum_reservation(minimum,ask)
            balanced=balanced_reservation(equity,current,reservation,required)
            if balanced<=0:
                risk_skip(state,journal,strategy,
                          f'{strategy} reached its 15% concentration limit')
                return
            reservation=balanced
            amount=D('5') if strategy=='down35' else (reservation*D('.8')).quantize(D('.01'),rounding=ROUND_DOWN)
            shares=amount/ask if strategy=='down35' else min(
                (amount/ask).quantize(D('.0001'),rounding=ROUND_DOWN),ask_size)
            if shares>ask_size:return
        if shares < minimum:
            return
        cost = amount if strategy=='down35' else (shares*ask).quantize(D("0.0001"),rounding=ROUND_DOWN)
        fee = D(0) if strategy=='down35' else (cost * D("0.02")).quantize(D("0.0001"), rounding=ROUND_DOWN)
        if D(str(state["open_exposure"])) + cost + fee > D(str(state["max_exposure"])):
            return
        row = {
            "id": f"{signal['slug']}:{strategy}", "strategy": strategy,
            "slug": signal["slug"], "token": signal["token"], "outcome": signal["outcome"],
            "price": float(ask), "shares": float(shares), "cost": float(cost), "fee": float(fee),
            "reservation": float(reservation), "status": "open",
            "opened_at_ms": int(time.time() * 1000),
            "scenario": scenario,
        }
        state["trades"].append(row)
        update_metrics(state)
        append_event(journal, {"type": "paper_fill", **row})


def strategy_specs(selected_strategy):
    if selected_strategy == "combined":
        selected_strategy = "all"
    kraken_specs = (
        ("btc-15m", "kraken", "btc", "kraken-trend-btc.json", "--kraken-trend"),
    )
    up60_specs = (
        ("btc-up60", "up60", "btc", None, "--up60"),
    )
    preopen_specs = (
        ("btc-preopen", "preopen", "btc", None, "--trader-clone"),
    )
    clip5_specs = (
        ("btc-clip5", "clip5", "btc", None, "--clip5"),
    )
    clip15_specs = (
        ("btc-clip15", "clip15", "btc", None, "--clip15"),
    )
    early3m_specs = (
        ("btc-early3m", "early3m", "btc", None, "--early3m"),
    )
    early3m5_specs = (
        ("btc-early3m5", "early3m5", "btc", None, "--early3m5"),
    )
    if selected_strategy == "kraken":
        return kraken_specs
    if selected_strategy == "up60":
        return up60_specs
    if selected_strategy == "preopen":
        return preopen_specs
    if selected_strategy == "clip5":
        return clip5_specs
    if selected_strategy == "clip15":
        return clip15_specs
    if selected_strategy == "early3m":
        return early3m_specs
    if selected_strategy == "early3m5":
        return early3m5_specs
    if selected_strategy == "all":
        return (kraken_specs + up60_specs + preopen_specs + clip5_specs + clip15_specs
                + early3m_specs + early3m5_specs)
    raise ValueError("strategy must be kraken, up60, preopen, clip5, clip15, early3m, early3m5, or all")


def main():
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "/data")
    binary = sys.argv[2] if len(sys.argv) > 2 else "/app/leadlag"
    config_directory = Path(sys.argv[3] if len(sys.argv) > 3 else "/app")
    seconds = sys.argv[4] if len(sys.argv) > 4 else "31536000"
    selected_strategy = sys.argv[5] if len(sys.argv) > 5 else "combined"
    global STATS_RESET_PATH
    STATS_RESET_PATH = Path(sys.argv[7]) if len(sys.argv) > 7 else data / "replica-stat-resets.json"
    if selected_strategy == "combined":
        selected_strategy = "all"
    if selected_strategy not in ("kraken", "up60", "preopen", "clip5", "clip15", "early3m", "early3m5", "all"):
        raise SystemExit("strategy must be kraken, up60, preopen, clip5, clip15, early3m, early3m5, or all")
    try:
        total_budget = D(sys.argv[6] if len(sys.argv) > 6 else "30")
    except InvalidOperation as error:
        raise SystemExit("paper budget must be numeric") from error
    if not total_budget.is_finite() or not D("16") <= total_budget <= D("1000000"):
        raise SystemExit("paper budget must be between 16 and 1000000")
    data.mkdir(parents=True, exist_ok=True)
    state_path = data / "replica-paper-portfolio.json"
    journal = data / "replica-paper-portfolio.jsonl"
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state.get("trades"), list):
            raise ValueError
    except (OSError, ValueError):
        state = {"started_at_ms": int(time.time() * 1000), "trades": []}
    state.update(budget=float(total_budget), strategy=selected_strategy,
                 exposure_rate=float(EXPOSURE_RATE), engine_health={})
    if selected_strategy in ('all', 'combined'):
        state['risk_controls']={'mode':'risk_balanced_all','strategy_exposure_rate':.15,
            'loss_streak_limit':3,'loss_cooldown_seconds':900,
            'skips':(state.get('risk_controls') or {}).get('skips',{})}
    update_metrics(state)
    atomic_json(state_path, state)

    specs = strategy_specs(selected_strategy)
    children, events = [], queue.Queue()
    state_lock = threading.RLock()
    closed = threading.Event()
    for label, strategy, asset, name, flag in specs:
        command = [binary, flag, "--asset", asset, "--signals-only", "--seconds", seconds,
                   "--journal", str(data / f"replica-paper-{label}.jsonl")]
        if name is not None:
            command.extend(["--quality-config", str(config_directory / name)])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        children.append((label, process))
        threading.Thread(target=drain, args=(label, strategy, process, events), daemon=True).start()
    def execute_and_save(strategy, signal):
        execute_signal(state, strategy, signal, journal, state_lock)
        with state_lock:
            atomic_json(state_path, state)

    def resolver():
        next_resolution=0
        try:last_reset_mtime=STATS_RESET_PATH.stat().st_mtime_ns
        except OSError:last_reset_mtime=0
        while not closed.is_set():
            changed=exit_down35_positions(state,journal,state_lock)
            try:reset_mtime=STATS_RESET_PATH.stat().st_mtime_ns
            except OSError:reset_mtime=0
            if reset_mtime!=last_reset_mtime:
                last_reset_mtime=reset_mtime
                with state_lock:update_metrics(state)
                changed=True
            if time.monotonic()>=next_resolution:
                resolve_positions(state,journal,state_lock);next_resolution=time.monotonic()+15;changed=True
            if changed:
                with state_lock:atomic_json(state_path,state)
            closed.wait(2)

    resolver_thread = threading.Thread(target=resolver, name="paper-resolver", daemon=True)
    resolver_thread.start()
    try:
        while True:
            if all(process.poll() is not None for _, process in children):
                break
            try:
                label, strategy, event = events.get(timeout=1)
                if event.get("type") == "health":
                    with state_lock:
                        state.setdefault("engine_health", {})[label] = event
                elif event.get("type") == "signal":
                    threading.Thread(target=execute_and_save, args=(strategy,event["signal"]),
                                     daemon=True).start()
                with state_lock:
                    atomic_json(state_path, state)
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        closed.set()
        for _, process in children:
            if process.poll() is None:
                process.terminate()
        for _, process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return next((process.returncode for _, process in children if process.returncode), 0)


if __name__ == "__main__":
    raise SystemExit(main())
