import tempfile, unittest
from pathlib import Path
from research.research_contracts import apply_security_master, audit_bar_csv, load_security_master, promotion_gate

class ResearchContractTests(unittest.TestCase):
    def test_duplicate_bars_block_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bars.csv"; row="2026-01-01T15:00:00+00:00,AAA,1,2,.5,1.5,100\n"
            path.write_text("timestamp,symbol,open,high,low,close,volume\n"+row+row,encoding="utf-8")
            audit=audit_bar_csv(path)
            gate=promotion_gate(audit=audit,security_master=True,halt_luld=True,point_in_time_cap=True,point_in_time_float=True,minimum_trades_met=True)
            self.assertEqual(1,audit.duplicate_symbol_timestamps); self.assertFalse(gate["promotable"])

    def test_security_master_filters_by_effective_session_and_reports_gaps(self):
        sessions={"2026-01-02":{"AAA":[1],"BBB":[2]},"2026-01-03":{"AAA":[3]}}
        rows=[{"symbol":"AAA","effective_from":"2026-01-03","effective_to":"","listed":True,"tradable":True,"exchange":"X","security_type":"stock"}]
        filtered,summary,diagnostics=apply_security_master(sessions,rows)
        self.assertNotIn("2026-01-02",filtered)
        self.assertEqual({"AAA"},set(filtered["2026-01-03"]))
        self.assertEqual(1,summary["classified_symbol_sessions"])
        self.assertFalse(summary["complete_observed_coverage"])
        self.assertEqual(3,len(diagnostics))

    def test_security_master_rejects_overlapping_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"master.csv"
            path.write_text("symbol,effective_from,effective_to,listed,tradable,exchange,security_type\nAAA,2026-01-01,2026-01-10,true,true,X,stock\nAAA,2026-01-10,,true,true,X,stock\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"overlapping intervals"):
                load_security_master(path)

    def test_security_master_rejects_invalid_boolean_and_reverse_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"master.csv"; header="symbol,effective_from,effective_to,listed,tradable,exchange,security_type\n"
            path.write_text(header+"AAA,2026-01-01,,maybe,true,X,stock\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"must be boolean"): load_security_master(path)
            path.write_text(header+"AAA,2026-01-10,2026-01-01,true,true,X,stock\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"ends before"): load_security_master(path)

if __name__=="__main__": unittest.main()
