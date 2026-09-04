import tempfile, unittest
from pathlib import Path
from research.market_events import blocked_event, load_market_events, market_event_audit

HEADER="record_type,symbol,effective_date,event_type,start_timestamp,end_timestamp,delisting_return,halt_luld_complete,corporate_actions_complete,delistings_complete,source\n"

class MarketEventTests(unittest.TestCase):
    def test_coverage_and_halt_are_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.csv"
            path.write_text(HEADER+"coverage,,2026-01-02,,,,,true,true,true,vendor\n"+"event,AAA,2026-01-02,HALT,2026-01-02T15:00:00+00:00,2026-01-02T15:10:00+00:00,,,,vendor\n",encoding="utf-8")
            events,coverage=load_market_events(path); audit=market_event_audit(["2026-01-02"],events,coverage)
            self.assertTrue(audit["halt_luld_complete"])
            self.assertEqual("HALT",blocked_event("AAA","2026-01-02T15:05:00+00:00",events))
            self.assertIsNone(blocked_event("AAA","2026-01-02T15:15:00+00:00",events))

    def test_delisting_requires_return(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.csv"
            path.write_text(HEADER+"event,AAA,2026-01-02,DELISTING,,,,,,,vendor\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"requires a return"): load_market_events(path)

    def test_halt_requires_complete_timezone_aware_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.csv"
            path.write_text(HEADER+"event,AAA,2026-01-02,HALT,2026-01-02T15:00:00,,,,,,vendor\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"invalid timestamps|requires timestamps"): load_market_events(path)

    def test_duplicate_coverage_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.csv"; row="coverage,,2026-01-02,,,,,true,true,true,vendor\n"
            path.write_text(HEADER+row+row,encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"duplicate.*coverage"): load_market_events(path)

if __name__=="__main__": unittest.main()
