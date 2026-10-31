import csv
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from research.qqq_bull_leadership_forward_ledger import append_session


class QqqBullLeadershipForwardLedgerTests(unittest.TestCase):
    def test_append_is_causal_idempotent_and_broker_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bars = root / "bars.csv"
            start = date(2026, 1, 1)
            rows = []
            for index in range(301):
                day = (start + timedelta(days=index)).isoformat()
                spy = 100.0 * math.exp(0.00002 * index * index)
                for symbol, price in (("SPY", spy), ("QQQ", 100.0 + index)):
                    rows.append({"timestamp": day, "symbol": symbol, "open": price,
                                 "high": price, "low": price, "close": price, "volume": 1})
            with bars.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
            session = (start + timedelta(days=300)).isoformat()
            result = append_session(bars, root / "ledger.jsonl", root / "state.json",
                                    session, "2026-10-29")
            self.assertEqual("QQQ", result["opening_target"])
            self.assertTrue(result["strategy_active"])
            self.assertFalse(result["broker_orders_enabled"])
            unchanged = append_session(bars, root / "ledger.jsonl", root / "state.json",
                                       session, "2026-10-29")
            self.assertEqual("unchanged", unchanged["status"])


if __name__ == "__main__":
    unittest.main()
