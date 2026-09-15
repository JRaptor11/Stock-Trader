import unittest
from types import SimpleNamespace

from research.breakout_opportunity_diagnostics import build_breakout_opportunity_diagnostics


class BreakoutOpportunityDiagnosticsTests(unittest.TestCase):
    def test_reports_recall_without_using_future_labels_in_target_function(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 10)]
        bars = {day: {"XLK": {"open":100.0,"high":106.0 if i < 5 else 101.0,"low":99.0,"close":100.0},
                      "SPY":{"open":100.0,"high":101.0,"low":99.0,"close":100.0},
                      "SHY":{"open":100.0,"high":100.1,"low":99.9,"close":100.0}}
                for i, day in enumerate(dates)}
        config = SimpleNamespace(strategy_names=("DONCHIAN_TREND_BREAKOUT",), primary_cost_bps=10.0)
        calls=[]
        def targets(_strategy, histories, _config):
            calls.append(len(histories["XLK"]))
            return {"XLK":1.0} if len(calls)==1 else {"SHY":1.0}
        labels=[{"date":day,"core_state":"TEST"} for day in dates]
        rows, summary = build_breakout_opportunity_diagnostics(
            dates,bars,config,dates[0],labels,targets,("XLK",)
        )
        five=next(row for row in summary if row["threshold"]==.05 and row["core_state"]=="ALL")
        self.assertGreater(five["opportunities"],0)
        self.assertGreater(five["detected_opportunities"],0)
        self.assertTrue(rows)

