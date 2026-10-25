import copy
import unittest
from datetime import date, timedelta

from research.market_conditions import causal_market_conditions
from research.tier1_etf_replay import Tier1Config, _simulate
from research.universes import resolve_universe


class ResearchEnginePrefixInvarianceTests(unittest.TestCase):
    def test_future_bar_mutation_cannot_change_prior_states_targets_or_fills(self):
        universe_name = "ETF_LONG_TERM_CROSS_ASSET_HISTORY"
        symbols = resolve_universe(universe_name)
        dates = [(date(2024, 1, 1) + timedelta(days=index)).isoformat()
                 for index in range(360)]
        bars = {}
        for index, day in enumerate(dates):
            observations = {}
            for symbol_index, symbol in enumerate(symbols):
                base = 40.0 + symbol_index * 3.0
                slope = 0.03 + symbol_index * 0.002
                close = base + slope * index + ((index + symbol_index) % 7) * 0.01
                observations[symbol] = {
                    "open": close * 0.999,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "close": close,
                    "volume": 1_000_000.0 + symbol_index * 10_000.0,
                }
            bars[day] = observations

        cutoff_index = 320
        cutoff = dates[cutoff_index]
        mutated = copy.deepcopy(bars)
        for day in dates[cutoff_index + 1:]:
            for symbol_index, symbol in enumerate(symbols):
                shock = 10.0 + symbol_index
                mutated[day][symbol]["open"] *= shock
                mutated[day][symbol]["high"] *= shock
                mutated[day][symbol]["low"] *= shock
                mutated[day][symbol]["close"] *= shock

        config = Tier1Config(
            universe_name=universe_name,
            market_state_universe_name="ETF_LONG_TERM_STATE_CANONICAL",
            strategy_names=("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",),
            minimum_scored_sessions=1,
        )
        scored_start = dates[260]
        original_daily, original_trades = _simulate(
            "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", dates, bars, config,
            config.primary_cost_bps, scored_start,
        )
        changed_daily, changed_trades = _simulate(
            "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", dates, mutated, config,
            config.primary_cost_bps, scored_start,
        )

        original_states = causal_market_conditions(
            dates, bars, resolve_universe("ETF_LONG_TERM_STATE_CANONICAL"),
            minimum_bucket_history=20,
        )
        changed_states = causal_market_conditions(
            dates, mutated, resolve_universe("ETF_LONG_TERM_STATE_CANONICAL"),
            minimum_bucket_history=20,
        )

        self.assertEqual(
            [row for row in original_daily if row["date"] <= cutoff],
            [row for row in changed_daily if row["date"] <= cutoff],
        )
        self.assertEqual(
            [row for row in original_trades if row["date"] <= cutoff],
            [row for row in changed_trades if row["date"] <= cutoff],
        )
        self.assertEqual(
            {day: row for day, row in original_states.items() if day <= cutoff},
            {day: row for day, row in changed_states.items() if day <= cutoff},
        )


if __name__ == "__main__":
    unittest.main()
