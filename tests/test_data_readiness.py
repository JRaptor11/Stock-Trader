import unittest
from types import SimpleNamespace

from research.data_readiness import intraday_data_readiness


class DataReadinessTests(unittest.TestCase):
    def test_missing_sources_fail_promotion_without_blocking_research(self):
        audit=SimpleNamespace(duplicate_symbol_timestamps=0,missing_ohlcv_rows=0,rows=10,symbols=2,first_timestamp="a",last_timestamp="b")
        result=intraday_data_readiness(audit=audit,universe=("SPY","AAA"),security_master={},market_events={},fundamentals={},survivorship_safe_declared=False)
        self.assertTrue(result["research_usable"]); self.assertFalse(result["promotion_data_ready"])
        self.assertIn("survivorship_safe_universe",result["missing_requirements"])

    def test_missing_spy_blocks_standard_research_comparison(self):
        audit=SimpleNamespace(duplicate_symbol_timestamps=0,missing_ohlcv_rows=0,rows=10,symbols=1,first_timestamp="a",last_timestamp="b")
        result=intraday_data_readiness(audit=audit,universe=("AAA",),security_master={},market_events={},fundamentals={},survivorship_safe_declared=False)
        self.assertFalse(result["research_usable"])


if __name__=="__main__": unittest.main()
