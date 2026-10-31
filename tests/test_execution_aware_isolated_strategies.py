import math
import unittest
from datetime import date, timedelta

from research.tier1_etf_replay import (
    Tier1Config,
    _canonical_raw_trend_state,
    _recent_raw_trend_states,
    _simulate,
    _strategy_rebalance_frequency,
    _targets,
)


def _histories(spy):
    return {"SPY": spy, "QQQ": [100.0] * len(spy), "BIL": [100.0] * len(spy), "SHY": [100.0] * len(spy)}


class ExecutionAwareIsolatedStrategyTests(unittest.TestCase):
    def setUp(self):
        self.config = Tier1Config(universe_name="ETF_LONG_TERM_STATE_SPECIALISTS_EXACT")

    def test_raw_trend_state_distinguishes_bull_deceleration(self):
        bull = [100.0 * math.exp(0.00002 * index * index) for index in range(230)]
        decelerating = [100.0 + 0.5 * index for index in range(220)]
        peak = decelerating[-1]
        decelerating.extend(peak - 2.0 * (index + 1) for index in range(10))
        self.assertEqual("BULL_ACCELERATING", _canonical_raw_trend_state(bull))
        self.assertEqual("BULL_DECELERATING", _canonical_raw_trend_state(decelerating))

    def test_bull_strategies_have_explicit_spy_fallback(self):
        bull = [100.0 * math.exp(0.00002 * index * index) for index in range(230)]
        decelerating = [100.0 + 0.5 * index for index in range(220)]
        peak = decelerating[-1]
        decelerating.extend(peak - 2.0 * (index + 1) for index in range(10))
        self.assertEqual({"QQQ": 1.0}, _targets(
            "QQQ_BULL_TREND_LEADERSHIP", _histories(bull), self.config
        ))
        self.assertEqual({"SPY": 1.0}, _targets(
            "QQQ_BULL_DECELERATION_EXPLORATORY", _histories(bull), self.config
        ))
        self.assertEqual({"QQQ": 1.0}, _targets(
            "QQQ_BULL_DECELERATION_EXPLORATORY", _histories(decelerating), self.config
        ))

    def test_downside_strategies_compare_first_signal_and_confirmation(self):
        decline = [200.0 - 0.4 * index for index in range(230)]
        histories = _histories(decline)
        self.assertEqual("BEAR_DETERIORATING", _canonical_raw_trend_state(decline))
        self.assertEqual(["BEAR_DETERIORATING"] * 3, _recent_raw_trend_states(decline, 3))
        for strategy in (
            "BIL_RAW_BEAR_DETERIORATION",
            "BIL_THREE_DAY_BEAR_DETERIORATION",
            "BIL_RAW_TRANSITION_OR_BEAR_DETERIORATION",
        ):
            self.assertEqual({"BIL": 1.0}, _targets(strategy, histories, self.config))

    def test_all_execution_aware_strategies_evaluate_daily(self):
        for strategy in (
            "QQQ_BULL_TREND_LEADERSHIP",
            "QQQ_BULL_DECELERATION_EXPLORATORY",
            "BIL_RAW_BEAR_DETERIORATION",
            "BIL_THREE_DAY_BEAR_DETERIORATION",
            "BIL_RAW_TRANSITION_OR_BEAR_DETERIORATION",
        ):
            self.assertEqual("daily", _strategy_rebalance_frequency(strategy, self.config))

    def test_turning_point_controls_preserve_published_slow_fast_blend(self):
        spy = [100.0 + 0.4 * index for index in range(232)]
        peak = spy[-1]
        spy.extend(peak - 0.5 * (index + 1) for index in range(21))
        histories = {
            "SPY": spy,
            "SHY": [100.0] * len(spy),
            "BIL": [100.0] * len(spy),
            "IEF": [100.0] * len(spy),
            "GLD": [100.0] * len(spy),
        }
        self.assertEqual(
            {"SPY": 1.0}, _targets("TURNING_POINT_SLOW_EQUITY", histories, self.config)
        )
        self.assertEqual(
            {"SHY": 1.0}, _targets("TURNING_POINT_FAST_EQUITY", histories, self.config)
        )
        self.assertEqual(
            {"SPY": 0.5, "SHY": 0.5},
            _targets("TURNING_POINT_MEDIUM_EQUITY", histories, self.config),
        )
        for strategy in (
            "TURNING_POINT_SLOW_EQUITY", "TURNING_POINT_FAST_EQUITY",
            "TURNING_POINT_MEDIUM_EQUITY", "TURNING_POINT_MEDIUM_ADAPTIVE_DEFENSE",
        ):
            self.assertEqual("monthly", _strategy_rebalance_frequency(strategy, self.config))

    def test_adaptive_defense_uses_bonds_only_with_negative_correlation(self):
        spy = [100.0]
        ief = [100.0]
        for _ in range(192):
            spy.append(spy[-1] * 1.002)
            ief.append(ief[-1] * 1.0001)
        for index in range(60):
            spy.append(spy[-1] * (1.004 if index % 2 else 0.99))
            ief.append(ief[-1] * (0.997 if index % 2 else 1.01))
        histories = {
            "SPY": spy,
            "IEF": ief,
            "SHY": [100.0] * len(spy),
            "BIL": [100.0] * len(spy),
            "GLD": [100.0] * len(spy),
        }
        self.assertEqual(
            {"SPY": 0.5, "IEF": 0.5},
            _targets("TURNING_POINT_MEDIUM_ADAPTIVE_DEFENSE", histories, self.config),
        )

    def test_complete_strategy_switches_at_next_open_and_pays_costs(self):
        dates = [
            (date(2020, 1, 1) + timedelta(days=index)).isoformat()
            for index in range(260)
        ]
        rise = [100.0 * math.exp(0.00002 * index * index) for index in range(220)]
        peak = rise[-1]
        spy = rise + [peak * (0.985 ** (index + 1)) for index in range(40)]
        bars = {}
        for day, spy_close in zip(dates, spy):
            bars[day] = {
                "SPY": {"open": spy_close, "close": spy_close},
                "QQQ": {"open": 100.0, "close": 100.0},
                "BIL": {"open": 100.0, "close": 100.0},
                "SHY": {"open": 100.0, "close": 100.0},
            }
        config = Tier1Config(
            universe_name="ETF_LONG_TERM_STATE_SPECIALISTS_EXACT",
            no_trade_band=0.0,
            strategy_names=("SPY_BUY_HOLD", "BIL_RAW_BEAR_DETERIORATION"),
        )
        _, trades = _simulate(
            "BIL_RAW_BEAR_DETERIORATION", dates, bars, config,
            cost_bps=10.0, scored_start=dates[205],
        )
        self.assertTrue({"SPY", "BIL"} <= {row["symbol"] for row in trades})
        self.assertGreater(sum(row["cost"] for row in trades), 0.0)
        initial_entries = [
            row for row in trades
            if row["date"] == dates[205] and row["symbol"] == "SPY"
        ]
        defensive_entries = [
            row for row in trades
            if row["date"] > dates[205] and row["symbol"] == "BIL"
            and row["notional"] > 0.0
        ]
        self.assertEqual(1, len(initial_entries))
        self.assertTrue(defensive_entries)


if __name__ == "__main__":
    unittest.main()
