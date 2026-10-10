import csv
import tempfile
import unittest
from pathlib import Path

from research.static_balanced_forward_ledger import append_session


def _bars(path: Path, sessions: list[str]) -> None:
    rows = []
    for index, session in enumerate(sessions):
        for symbol, price in (("SPY", 100 + index), ("IEF", 50 + index), ("GLD", 200 + index)):
            rows.append({"timestamp": session + "T20:00:00Z", "symbol": symbol,
                         "open": price, "high": price * 1.01, "low": price * .99,
                         "close": price * 1.005, "volume": 1000})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


class StaticBalancedForwardLedgerTests(unittest.TestCase):
    def test_append_is_idempotent_and_broker_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bars = root / "bars.csv"
            ledger = root / "ledger.jsonl"; state = root / "state.json"
            _bars(bars, ["2026-10-12"])
            first = append_session(bars, ledger, state, "2026-10-12", "2026-10-13")
            second = append_session(bars, ledger, state, "2026-10-12", "2026-10-13")
            self.assertEqual("appended", first["status"])
            self.assertEqual("unchanged", second["status"])
            self.assertFalse(first["broker_orders_enabled"])
            self.assertTrue(first["portfolio_invariants_pass"])
            self.assertEqual(1, len(ledger.read_text().splitlines()))

    def test_rejects_pre_freeze_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bars = root / "bars.csv"
            _bars(bars, ["2026-10-09"])
            with self.assertRaisesRegex(ValueError, "precedes frozen forward start"):
                append_session(bars, root / "ledger", root / "state",
                               "2026-10-09", "2026-10-12")

    def test_rejects_gap_after_first_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bars = root / "bars.csv"
            ledger = root / "ledger.jsonl"; state = root / "state.json"
            _bars(bars, ["2026-10-12"])
            append_session(bars, ledger, state, "2026-10-12", "2026-10-13")
            _bars(bars, ["2026-10-14"])
            with self.assertRaisesRegex(ValueError, "next expected session"):
                append_session(bars, ledger, state, "2026-10-14", "2026-10-15")


if __name__ == "__main__":
    unittest.main()
