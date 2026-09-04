import tempfile, unittest
from pathlib import Path
from research.point_in_time_fundamentals import fundamentals_audit, load_fundamentals, snapshot_at

class FundamentalsTests(unittest.TestCase):
    def test_snapshot_never_uses_future_known_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"fundamentals.csv"
            path.write_text("symbol,effective_date,known_at,market_cap,float_shares,source\nAAA,2026-01-01,2026-01-02T14:00:00+00:00,1000,100,vendor\nAAA,2026-01-01,2026-01-03T14:00:00+00:00,2000,200,vendor\n",encoding="utf-8")
            rows=load_fundamentals(path)
            self.assertEqual(1000,snapshot_at(rows,"AAA","2026-01-02T15:00:00+00:00")["market_cap"])
            self.assertEqual(2000,snapshot_at(rows,"AAA","2026-01-03T15:00:00+00:00")["market_cap"])
            sessions={"2026-01-02":{"AAA":[{"timestamp":"2026-01-02T15:00:00+00:00"}],"BBB":[{"timestamp":"2026-01-02T15:00:00+00:00"}]}}
            audit,diagnostics=fundamentals_audit(sessions,rows)
            self.assertEqual(.5,audit["coverage"])
            self.assertFalse(audit["market_cap_complete"])
            self.assertEqual(2,len(diagnostics))

    def test_rejects_nonpositive_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"fundamentals.csv"
            path.write_text("symbol,effective_date,known_at,market_cap,float_shares,source\nAAA,2026-01-01,2026-01-02T14:00:00+00:00,0,100,vendor\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"positive"): load_fundamentals(path)

    def test_rejects_naive_timestamp_and_duplicate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"fundamentals.csv"; header="symbol,effective_date,known_at,market_cap,float_shares,source\n"
            path.write_text(header+"AAA,2026-01-01,2026-01-02T14:00:00,1000,100,vendor\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"invalid identity"): load_fundamentals(path)
            row="AAA,2026-01-01,2026-01-02T14:00:00+00:00,1000,100,vendor\n"; path.write_text(header+row+row,encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"duplicate fundamentals"): load_fundamentals(path)

if __name__=="__main__": unittest.main()
