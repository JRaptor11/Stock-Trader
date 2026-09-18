import unittest

from research.isolated_strategy_diagnostics import (
    build_baseline_era_recurrence, build_defensive_baseline_comparisons,
    build_defensive_distinctness, build_locked_tactical_validation,
    build_tactical_horizon_comparisons,
)


class Config:
    primary_cost_bps = 10.0
    strategy_names = ("BASELINE", "DRAWDOWN_BRAKE", "DONCHIAN_TREND_BREAKOUT")
    discovery_end_date = "2024-12-31"
    holdout_start_date = "2025-01-01"


class IsolatedDiagnosticsTests(unittest.TestCase):
    def test_defensive_comparison_is_relative_to_displaced_baseline(self):
        rows = []
        for cost in (10.0, 20.0):
            rows.extend([
                {"period":"holdout","core_state":"BEAR","strategy":"BASELINE","cost_bps":cost,"strategy_compounded_return":-.10},
                {"period":"holdout","core_state":"BEAR","strategy":"DRAWDOWN_BRAKE","cost_bps":cost,"strategy_compounded_return":.02,"sessions":30,"episodes":4,"conditional_sequence_max_drawdown":-.03,"positive_episode_rate":.75},
            ])
        primary = [row for row in rows if row["cost_bps"] == 10.0]
        output = build_defensive_baseline_comparisons(
            Config, primary, rows, "BASELINE"
        )
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

    def test_locked_tactical_validation_keeps_transition_phases_separate(self):
        rows=[]
        for index in range(24):
            rows.append({
                "strategy":"DONCHIAN_TREND_BREAKOUT","core_state":"BULL",
                "horizon_sessions":5,"entry_date":f"{2020 + index % 6}-01-{index % 20 + 1:02d}",
                "incremental_return_vs_baseline":.01,
                "pending_core_state_at_entry":"NEXT" if index % 2 else "",
                "state_episode_id":index // 2,
            })
        hypotheses=[{"hypothesis_id":"stable","strategy":"DONCHIAN_TREND_BREAKOUT",
                     "core_state":"BULL","horizon_sessions":5,"transition_phase":"stable"},
                    {"hypothesis_id":"pending","strategy":"DONCHIAN_TREND_BREAKOUT",
                     "core_state":"BULL","horizon_sessions":5,"transition_phase":"pending"}]
        output=build_locked_tactical_validation(Config,rows,hypotheses)
        full=[row for row in output if row["period"]=="full"]
        self.assertEqual(2,len(full)); self.assertEqual({12},{row["events"] for row in full})
        self.assertTrue(all(row["additional_20bps_round_trip_stress"] > 0 for row in full))

    def test_defensive_distinctness_detects_duplicate_return_paths(self):
        class DefensiveConfig:
            primary_cost_bps=10.0
            strategy_names=("DRAWDOWN_BRAKE","VOLATILITY_SHOCK_DEFENSIVE")
        daily=[]
        for strategy in DefensiveConfig.strategy_names:
            daily += [{"strategy":strategy,"cost_bps":10,"date":"2025-01-01","equity":100},
                      {"strategy":strategy,"cost_bps":10,"date":"2025-01-02","equity":101}]
        labels=[{"date":"2025-01-01","core_state":"BULL"},{"date":"2025-01-02","core_state":"BULL"}]
        rows=build_defensive_distinctness(DefensiveConfig,daily,[],labels)
        self.assertTrue(next(row for row in rows if row["core_state"]=="ALL")["behaviorally_indistinguishable"])

    def test_defensive_distinctness_uses_economic_tolerance(self):
        class DefensiveConfig:
            primary_cost_bps=10.0
            strategy_names=("DRAWDOWN_BRAKE","VOLATILITY_SHOCK_DEFENSIVE")
        daily=[]
        for index, day in enumerate(("2025-01-01", "2025-01-02", "2025-01-03")):
            daily.extend([
                {"strategy":"DRAWDOWN_BRAKE","cost_bps":10,"date":day,
                 "equity":100 * (1.01 ** index)},
                {"strategy":"VOLATILITY_SHOCK_DEFENSIVE","cost_bps":10,"date":day,
                 "equity":100 * (1.010002 ** index)},
            ])
        labels=[{"date":row["date"],"core_state":"BULL"} for row in daily[:3]]
        trades=[
            {"strategy":strategy,"cost_bps":10,"date":"2025-01-02"}
            for strategy in DefensiveConfig.strategy_names
        ]
        row=next(row for row in build_defensive_distinctness(
            DefensiveConfig,daily,trades,labels) if row["core_state"]=="ALL")
        self.assertLess(row["identical_daily_return_rate"], .98)
        self.assertEqual(1.0,row["economically_equivalent_daily_return_rate_1bp"])
        self.assertTrue(row["behaviorally_indistinguishable"])

    def test_baseline_era_recurrence_uses_fixed_nonoverlapping_eras(self):
        class BaselineConfig:
            primary_cost_bps=10.0
            strategy_names=("SPY_BUY_HOLD","CROSS_ASSET_DUAL_MOMENTUM")
        daily=[]; labels=[]
        for year in (2019,2021,2023):
            for index in range(22):
                day=f"{year}-01-{index+1:02d}"; labels.append({"date":day,"core_state":"BULL"})
                daily.extend([{"strategy":"SPY_BUY_HOLD","cost_bps":10,"date":day,"equity":100+index},
                              {"strategy":"CROSS_ASSET_DUAL_MOMENTUM","cost_bps":10,"date":day,"equity":100+index*2}])
        _,summary=build_baseline_era_recurrence(BaselineConfig,daily,labels)
        row=next(row for row in summary if row["strategy"]=="CROSS_ASSET_DUAL_MOMENTUM")
        self.assertEqual(3,row["eligible_eras"]); self.assertTrue(row["recurs_across_eras"])


if __name__ == "__main__": unittest.main()
