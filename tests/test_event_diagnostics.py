import unittest
from types import SimpleNamespace

from research.event_diagnostics import build_event_diagnostics


class EventDiagnosticsTests(unittest.TestCase):
    def _fixture(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
        bars = {}
        for index, day in enumerate(dates):
            bars[day] = {
                "SPY": {"open": 100 + index, "high": 102 + index,
                        "low": 99 + index, "close": 101 + index},
                "SHY": {"open": 100, "high": 100.1, "low": 99.9, "close": 100},
            }
        config = SimpleNamespace(
            strategy_names=("MARKET_DIP_REBOUND_1D",),
            cash_proxy_symbol="SHY", cost_ladder_bps=(0.0, 10.0),
        )
        conditions = {day: {"volatility_20d_bucket": "Q5_HIGH"} for day in dates}
        return dates, bars, config, conditions

    def test_event_uses_next_open_and_preserves_duplicate_signals(self):
        dates, bars, config, conditions = self._fixture()
        calls = {"count": 0}

        def targets(_strategy, _histories, _config):
            calls["count"] += 1
            return {"SPY": 1.0} if calls["count"] in (1, 2) else {"SHY": 1.0}

        rows, summary = build_event_diagnostics(
            dates, bars, config, dates[0], conditions, targets
        )
        zero_cost = [row for row in rows if row["cost_bps"] == 0.0]
        self.assertEqual(1, len(zero_cost))
        event = zero_cost[0]
        self.assertEqual(dates[0], event["signal_date"])
        self.assertEqual(dates[1], event["entry_date"])
        self.assertEqual(dates[3], event["exit_date"])
        self.assertEqual(1, event["duplicate_signal_count"])
        self.assertEqual("Q5_HIGH", event["volatility_20d_bucket"])
        self.assertAlmostEqual(bars[dates[3]]["SPY"]["open"] /
                               bars[dates[1]]["SPY"]["open"] - 1,
                               event["gross_return"])
        self.assertEqual(2, len(summary))

    def test_cost_is_applied_on_entry_and_exit(self):
        dates, bars, config, conditions = self._fixture()

        def targets(_strategy, histories, _config):
            return {"SPY": 1.0} if len(histories["SPY"]) == 1 else {"SHY": 1.0}

        rows, _ = build_event_diagnostics(
            dates, bars, config, dates[0], conditions, targets
        )
        gross = next(row for row in rows if row["cost_bps"] == 0.0)
        net = next(row for row in rows if row["cost_bps"] == 10.0)
        expected = gross["exit_price"] * 0.999 / (gross["entry_price"] * 1.001) - 1
        self.assertAlmostEqual(expected, net["net_return"])
        self.assertLess(net["net_return"], gross["gross_return"])

    def test_open_event_is_marked_censored(self):
        dates, bars, config, conditions = self._fixture()

        def targets(_strategy, _histories, _config):
            return {"SPY": 1.0}

        rows, _ = build_event_diagnostics(
            dates, bars, config, dates[0], conditions, targets
        )
        self.assertTrue(rows[0]["censored"])
        self.assertEqual(dates[-1], rows[0]["exit_date"])


if __name__ == "__main__":
    unittest.main()
