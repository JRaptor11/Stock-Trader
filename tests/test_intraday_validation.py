import unittest
from research.intraday_validation import (
    benchmark_scorecards, matched_controls, nested_regime_walk_forward,
    portfolio_daily_series, spy_benchmark_daily, walk_forward_trade_scorecards,
)
from research.execution_model import ExecutionAssumptions

class IntradayValidationTests(unittest.TestCase):
    def test_daily_benchmark_and_performance_are_standardized(self):
        trades=[{"date":"2026-01-02","strategy":"S","portfolio_status":"accepted","realized_pnl":100,
                 "allocated_notional":1000,"net_return":.1}]
        days=["2026-01-01","2026-01-02"]
        daily=portfolio_daily_series(trades,days,["S"],10000,10)
        spy=spy_benchmark_daily({"2026-01-01":{"SPY":{"open":100,"close":101}},"2026-01-02":{"SPY":{"open":101,"close":102}}},days,10000)
        summary,annual,bootstrap=benchmark_scorecards(daily,spy,10000,1)
        self.assertEqual(2,len(daily)); self.assertAlmostEqual(.01,summary[0]["portfolio_return"])
        self.assertEqual(1,summary[0]["trade_count"]); self.assertEqual(1,len(annual)); self.assertEqual(2,bootstrap[0]["observations"])

    def test_nested_regime_selection_uses_training_only(self):
        days=[f"2026-01-{day:02d}" for day in range(1,7)]; trades=[]
        for day,value in zip(days[:4],[.02,.021,.019,.022]):
            trades.append({"date":day,"strategy":"S","portfolio_status":"accepted","net_return":value,"realized_pnl":value*1000,
                           "market_trend_20d_return_bucket":"Q5_HIGH"})
        trades.append({"date":days[4],"strategy":"S","portfolio_status":"accepted","net_return":.01,"realized_pnl":10,
                       "market_trend_20d_return_bucket":"Q5_HIGH"})
        rows=nested_regime_walk_forward(trades,days,4,1,1,1000,1,minimum_bucket_trades=4,alpha=1.)
        self.assertEqual("REGIME_BUCKET",rows[0]["selection"]); self.assertEqual(1,rows[0]["test_trades"])
        self.assertAlmostEqual(.01,rows[0]["test_portfolio_return"])

    def test_walk_forward_can_use_full_market_calendar(self):
        trades=[{"date":"2026-01-04","strategy":"A","portfolio_status":"accepted","net_return":.01,"realized_pnl":100}]
        rows=walk_forward_trade_scorecards(trades,min_train_sessions=2,test_sessions=1,step_sessions=1,session_dates=["2026-01-01","2026-01-02","2026-01-03","2026-01-04"])
        self.assertEqual(2,len(rows))
        self.assertEqual("2026-01-03",rows[0]["test_start"])

    def test_control_uses_non_signal_same_session_bar(self):
        bars=lambda symbol:[{"timestamp":f"2026-01-01T10:0{i}:00+00:00","open":10+i,"close":10+i+.1,"volume":10000} for i in range(3)]
        trade={"date":"2026-01-01","strategy":"S","symbol":"AAA","entry_timestamp":"x","entry_bar_index":1,"entry_price":11.,"holding_bars":1,"net_return":.02,"portfolio_status":"accepted"}
        rows=matched_controls([trade],[],{"2026-01-01":{"AAA":bars("AAA"),"BBB":bars("BBB")}},ExecutionAssumptions(maximum_bar_participation=1),100)
        self.assertEqual("BBB",rows[0]["control_symbol"]); self.assertIn("matched_excess_return",rows[0])

    def test_short_history_has_no_walk_forward_fold(self):
        self.assertEqual([],walk_forward_trade_scorecards([],10,2,2))

    def test_walk_forward_uses_realized_portfolio_pnl(self):
        trades=[]
        for index in range(4):
            trades.append({"date":f"2026-01-0{index+1}","strategy":"S","net_return":.10,
                           "realized_pnl":10.,"portfolio_status":"accepted"})
        rows=walk_forward_trade_scorecards(trades,2,1,1,1_000.)
        self.assertEqual(2,len(rows))
        self.assertAlmostEqual(.01,rows[0]["test_portfolio_return"])

if __name__=="__main__": unittest.main()
