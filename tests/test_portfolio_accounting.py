import unittest
from research.portfolio_accounting import allocate_trades

class PortfolioAccountingTests(unittest.TestCase):
    def test_overlapping_trade_is_rejected_when_capital_is_reserved(self):
        trades=[{"strategy":"A","symbol":"AAA","entry_timestamp":"2026-01-01T10:00:00+00:00","exit_timestamp":"2026-01-01T11:00:00+00:00","net_return":.1},{"strategy":"A","symbol":"BBB","entry_timestamp":"2026-01-01T10:05:00+00:00","exit_timestamp":"2026-01-01T11:05:00+00:00","net_return":.1}]
        rows,curve=allocate_trades(trades,initial_cash=10000,target_notional=10000,maximum_positions=5,maximum_symbol_pct=1)
        self.assertEqual("accepted",rows[0]["portfolio_status"]); self.assertEqual("insufficient_cash",rows[1]["portfolio_rejection_reason"]); self.assertEqual(11000,curve[-1]["cash"])

    def test_strategies_have_independent_capital(self):
        trades=[{"strategy":s,"symbol":"AAA","entry_timestamp":"2026-01-01T10:00:00+00:00","exit_timestamp":"2026-01-01T11:00:00+00:00","net_return":0} for s in ("A","B")]
        rows,_=allocate_trades(trades,initial_cash=10000,target_notional=10000,maximum_positions=1,maximum_symbol_pct=1)
        self.assertTrue(all(row["portfolio_status"]=="accepted" for row in rows))

if __name__=="__main__": unittest.main()
