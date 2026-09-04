import unittest
from research.intraday_validation import matched_controls, walk_forward_trade_scorecards
from research.execution_model import ExecutionAssumptions

class IntradayValidationTests(unittest.TestCase):
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
