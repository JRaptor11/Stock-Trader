import unittest
from datetime import date, timedelta

from research.daily_strategy_interface import DailyStrategyRegistry, DailyStrategySpec
from research.tier1_etf_replay import (
    Tier1Config, _legacy_targets, _simulate, _strategy_rebalance_frequency, _targets,
)
from research.universes import resolve_universe


class DailyStrategyInterfaceTests(unittest.TestCase):
    def test_six_current_cohort_strategies_have_exact_target_parity(self):
        config = Tier1Config(
            universe_name="ETF_TIER2_MULTI_SLEEVE",
            momentum_lookbacks_days=(63, 126, 252),
        )
        histories = {
            symbol: [100 + index * (0.02 + offset * 0.001) for index in range(300)]
            for offset, symbol in enumerate(resolve_universe(config.universe_name))
        }
        histories["SPY"][-1] = histories["SPY"][-2] * 0.97
        for strategy in (
            "SPY_BUY_HOLD", "STATIC_MULTI_SLEEVE", "STATIC_60_30_10",
            "INVERSE_VOLATILITY_BALANCED", "SECTOR_PRICE_BREAKOUT_20D",
            "MARKET_DIP_REBOUND_1D",
        ):
            self.assertEqual(
                _legacy_targets(strategy, histories, config),
                _targets(strategy, histories, config),
                strategy,
            )

    def test_existing_cadence_is_preserved(self):
        config = Tier1Config(rebalance_frequency="monthly")
        self.assertEqual("daily", _strategy_rebalance_frequency(
            "SECTOR_PRICE_BREAKOUT_20D", config
        ))
        self.assertEqual("daily", _strategy_rebalance_frequency(
            "MARKET_DIP_REBOUND_1D", config
        ))
        self.assertEqual("monthly", _strategy_rebalance_frequency(
            "STATIC_MULTI_SLEEVE", config
        ))

    def test_six_current_cohort_strategies_have_full_simulation_parity(self):
        config = Tier1Config(
            universe_name="ETF_TIER2_MULTI_SLEEVE",
            momentum_lookbacks_days=(63, 126, 252),
        )
        symbols = resolve_universe(config.universe_name)
        dates, bars = [], {}
        day = date(2024, 1, 2)
        for index in range(300):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            key = day.isoformat()
            dates.append(key)
            bars[key] = {}
            for offset, symbol in enumerate(symbols):
                close = 100 + index * (0.02 + offset * 0.001)
                if symbol == "SPY" and index in (270, 285):
                    close *= 0.97
                bars[key][symbol] = {
                    "open": close - 0.03, "high": close + 0.10,
                    "low": close - 0.10, "close": close,
                }
            day += timedelta(days=1)
        for strategy in (
            "SPY_BUY_HOLD", "STATIC_MULTI_SLEEVE", "STATIC_60_30_10",
            "INVERSE_VOLATILITY_BALANCED", "SECTOR_PRICE_BREAKOUT_20D",
            "MARKET_DIP_REBOUND_1D",
        ):
            interface = _simulate(strategy, dates, bars, config, 10.0, dates[260])
            legacy = _simulate(
                strategy, dates, bars, config, 10.0, dates[260],
                target_function=_legacy_targets,
            )
            self.assertEqual(legacy, interface, strategy)

    def test_registry_rejects_invalid_weights_and_symbols(self):
        config = Tier1Config()
        invalid_weight = DailyStrategyRegistry((DailyStrategySpec(
            "BAD_WEIGHT", "test", lambda _context: {"SPY": 1.1}
        ),))
        with self.assertRaisesRegex(ValueError, "above 100%"):
            invalid_weight.targets("BAD_WEIGHT", {"SPY": [100]}, config)
        invalid_symbol = DailyStrategyRegistry((DailyStrategySpec(
            "BAD_SYMBOL", "test", lambda _context: {"UNKNOWN": 1.0}
        ),))
        with self.assertRaisesRegex(ValueError, "outside"):
            invalid_symbol.targets("BAD_SYMBOL", {"SPY": [100]}, config)

    def test_equal_weight_trade_order_is_deterministic(self):
        config = Tier1Config(
            universe_name="ETF_TIER2_MULTI_SLEEVE",
            strategy_names=("STATIC_60_30_10",),
        )
        symbols = resolve_universe(config.universe_name)
        dates = ["2026-01-02", "2026-01-05"]
        bars = {
            day: {symbol: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
                  for symbol in symbols}
            for day in dates
        }
        _, trades = _simulate(
            "STATIC_60_30_10", dates, bars, config, 10.0, dates[0]
        )
        self.assertEqual(
            sorted(("GLD", "IEF", "SPY"), key=lambda symbol: (
                {"GLD": .10, "IEF": .30, "SPY": .60}[symbol], symbol
            )),
            [row["symbol"] for row in trades],
        )


if __name__ == "__main__":
    unittest.main()
