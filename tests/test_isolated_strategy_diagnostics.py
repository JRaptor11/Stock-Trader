import unittest

from research.isolated_strategy_diagnostics import (
    build_defensive_baseline_comparisons, build_tactical_horizon_comparisons,
)


class Config:
    primary_cost_bps = 10.0
    strategy_names = ("BASELINE", "DRAWDOWN_BRAKE", "DONCHIAN_TREND_BREAKOUT")


class IsolatedDiagnosticsTests(unittest.TestCase):
    def test_defensive_comparison_is_relative_to_displaced_baseline(self):
        rows = []
        for cost in (10.0, 20.0):
            rows.extend([
                {"period":"holdout","core_state":"BEAR","strategy":"BASELINE","cost_bps":cost,"strategy_compounded_return":-.10},
                {"period":"holdout","core_state":"BEAR","strategy":"DRAWDOWN_BRAKE","cost_bps":cost,"strategy_compounded_return":.02,"sessions":30,"episodes":4,"conditional_sequence_max_drawdown":-.03,"positive_episode_rate":.75},
            ])
        output = build_defensive_baseline_comparisons(Config, rows, "BASELINE")
        self.assertEqual(1, len(output)); self.assertGreater(output[0]["relative_wealth_vs_baseline"], .10)
        self.assertTrue(output[0]["positive_at_20bps_vs_baseline"])

    def test_tactical_horizons_and_causal_state_age_are_reported(self):
        dates=[f"2025-01-{day:02d}" for day in range(1,13)]
        daily=[{"strategy":"BASELINE","cost_bps":10,"date":day,"equity":100+i} for i,day in enumerate(dates)]
        labels=[{"date":day,"core_state":"BULL","state_changed":str(i==0),"pending_core_state":"","pending_confirmation_sessions":"0"} for i,day in enumerate(dates)]
        event={"strategy":"DONCHIAN_TREND_BREAKOUT","cost_bps":10,"signal_date":dates[0],"entry_date":dates[1]}
        for horizon in (1,2,3,5,10): event[f"exit_{horizon}d_return"]=.02*horizon
        comparisons, summary=build_tactical_horizon_comparisons(Config,[event],labels,daily,"BASELINE")
        self.assertEqual(5,len(comparisons)); self.assertEqual({1,2,3,5,10},{row["horizon_sessions"] for row in comparisons})
        self.assertEqual(1,comparisons[0]["state_age_sessions_at_entry"])
        self.assertEqual(10,len(summary))


if __name__ == "__main__": unittest.main()
