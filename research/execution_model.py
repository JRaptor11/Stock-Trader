"""Shared conservative bar-based execution primitives."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ExecutionAssumptions:
    cost_bps_per_side: float=10.; spread_bps: float=10.; maximum_bar_participation: float=.01

def entry_fill(open_price, bar_volume, notional, assumptions):
    capacity=open_price*bar_volume*assumptions.maximum_bar_participation
    if capacity < notional: return None,{"reason":"insufficient_bar_capacity","capacity":capacity}
    return open_price*(1+assumptions.spread_bps/20000),{"reason":"filled","capacity":capacity}

def exit_fill(raw_price, assumptions): return raw_price*(1-assumptions.spread_bps/20000)

def net_return(entry, exit_price, assumptions): return exit_price/entry-1-2*assumptions.cost_bps_per_side/10000
