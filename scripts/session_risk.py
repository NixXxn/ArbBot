"""Pure budget, loss, and fee calculations for a bounded multi-entry experiment."""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from live_executor import ExecutionStopped


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ExecutionStopped("Risk inputs must be finite")
    return result


def fee_per_share(price, fee_rate):
    price, rate = decimal(price), decimal(fee_rate)
    if not 0 < price < 1 or not 0 <= rate <= 1:
        raise ExecutionStopped("Invalid price or fee rate")
    return rate * price * (1-price)


def break_even_probability(price, fee_rate):
    """Probability needed when a share is held to a $1/$0 resolution."""
    return decimal(price) + fee_per_share(price, fee_rate)


def dynamic_reservation(available_budget, base_budget, reference_budget=30,
                        max_exposure_rate=Decimal("0.30")):
    """Scale the old $4-$5 tiers with bankroll while retaining a 30% hard ceiling."""
    available = decimal(available_budget)
    base = decimal(base_budget)
    reference = decimal(reference_budget)
    rate = decimal(max_exposure_rate)
    if available <= 0 or base <= 0 or reference <= 0 or not 0 < rate <= Decimal("0.30"):
        raise ExecutionStopped("Invalid dynamic sizing input")
    desired = available * base / reference
    return min(desired, available * rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def minimum_reservation(minimum_shares, max_price, spend_fraction=Decimal("0.80")):
    """Smallest cent-denominated reservation whose spend buys the market minimum."""
    shares, price, fraction = (decimal(minimum_shares), decimal(max_price),
                               decimal(spend_fraction))
    if shares <= 0 or not 0 < price < 1 or not 0 < fraction <= 1:
        raise ExecutionStopped("Invalid minimum-order sizing input")
    return (shares * price / fraction).quantize(Decimal("0.01"), rounding=ROUND_UP)


@dataclass
class SessionRisk:
    total_buy_cap: Decimal
    stop_loss: Decimal
    target_entries: int = 10
    spent: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    high_water_pnl: Decimal = Decimal(0)
    intents: int = 0
    markets: set = field(default_factory=set)
    reservations: dict = field(default_factory=dict)
    open_costs: dict = field(default_factory=dict)
    dynamic: bool = False
    max_exposure_rate: Decimal = Decimal("0.30")

    def __post_init__(self):
        self.total_buy_cap = decimal(self.total_buy_cap)
        self.stop_loss = decimal(self.stop_loss)
        self.max_exposure_rate = decimal(self.max_exposure_rate)
        if (self.total_buy_cap <= 0 or self.stop_loss <= 0
                or not 1 <= self.target_entries <= 1_000_000
                or not 0 < self.max_exposure_rate <= Decimal("0.30")):
            raise ExecutionStopped("Invalid experiment bounds")

    @property
    def reserved(self):
        return sum(self.reservations.values(), Decimal(0))

    @property
    def unresolved(self):
        return bool(self.reservations or self.open_costs)

    @property
    def current_cost(self):
        """Compatibility view for bounded single-position callers."""
        return next(iter(self.open_costs.values())) if len(self.open_costs) == 1 else None

    def _market(self, market, collection, action):
        if market is not None:
            return market
        if len(collection) == 1:
            return next(iter(collection))
        raise ExecutionStopped(f"Market identity is required to {action} concurrent positions")

    def reserve(self, market, maximum_cost, available_budget=None):
        cost = decimal(maximum_cost)
        if cost <= 0:
            raise ExecutionStopped("Entry cost must be positive")
        if self.intents >= self.target_entries or market in self.markets:
            raise ExecutionStopped("Entry count or one-entry-per-market limit reached")
        if not self.dynamic and self.spent + self.reserved + cost > self.total_buy_cap:
            raise ExecutionStopped("Total buy-spending cap would be exceeded")
        if self.dynamic:
            available = decimal(available_budget)
            open_exposure = sum(self.open_costs.values(), Decimal(0)) + self.reserved
            capital = available + sum(self.open_costs.values(), Decimal(0))
            if (available <= 0 or capital <= 0
                    or open_exposure + cost > capital * self.max_exposure_rate):
                raise ExecutionStopped("Entry would exceed the 30% portfolio exposure limit")
        drawdown = self.high_water_pnl - self.realized_pnl
        worst_case_open = sum(self.open_costs.values(), Decimal(0)) + self.reserved
        if drawdown + worst_case_open + cost > self.stop_loss:
            raise ExecutionStopped("Worst-case entry loss would exceed remaining drawdown allowance")
        self.reservations[market] = cost
        self.intents += 1
        self.markets.add(market)

    def record_fill(self, actual_cost, market=None):
        market = self._market(market, self.reservations, "record a fill for")
        cost = decimal(actual_cost)
        reservation = self.reservations.get(market)
        if cost <= 0 or reservation is None or cost > reservation or market in self.open_costs:
            raise ExecutionStopped("Fill cost is outside the reserved bound; reconcile and halt")
        self.spent += cost
        del self.reservations[market]
        self.open_costs[market] = cost

    def cancel_before_submission(self, market=None):
        """Release one reservation after a proven pre-POST rejection."""
        market = self._market(market, self.reservations, "cancel")
        if market not in self.reservations or market in self.open_costs:
            raise ExecutionStopped("Only an unfilled reservation can be cancelled")
        del self.reservations[market]
        self.intents -= 1

    def record_resolution(self, payout, entry_cost, market=None):
        market = self._market(market, self.open_costs, "resolve")
        payout, cost = decimal(payout), decimal(entry_cost)
        if payout < 0 or self.open_costs.get(market) != cost:
            raise ExecutionStopped("Resolution must follow a reconciled fill")
        self.realized_pnl += payout-cost
        self.high_water_pnl = max(self.high_water_pnl, self.realized_pnl)
        del self.open_costs[market]
