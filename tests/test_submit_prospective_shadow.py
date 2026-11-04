import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from research.submit_prospective_shadow import build_payload, fetch_first_iex_opening_quotes
from research.universes import resolve_universe


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class OpeningQuoteCollectionTests(unittest.TestCase):
    def test_first_iex_quote_collection_is_bounded_and_diagnostic(self):
        folder = Path(".test-prospective-submit") / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            env = folder / ".env"
            env.write_text("API_KEY=test-key\nSECRET_KEY=test-secret\n", encoding="utf-8")
            payloads = iter([
                {"quotes": [{"bp": 99.9, "ap": 100.1,
                              "t": "2026-09-28T13:30:00.1Z"}]},
                {"quotes": []},
            ])
            with patch("urllib.request.urlopen",
                       side_effect=lambda *args, **kwargs: Response(next(payloads))):
                quotes, metadata = fetch_first_iex_opening_quotes(
                    session="2026-09-28", symbols=["SPY", "BIL"], alpaca_env=env
                )
            self.assertEqual(1, len(quotes))
            self.assertFalse(metadata["used_for_execution"])
            self.assertEqual(2, metadata["requested_symbols"])
            self.assertEqual(1, metadata["captured_symbols"])
            self.assertIn("no_iex_quote", next(iter(metadata["failed_symbols"].values())))
        finally:
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()

    def test_payload_can_target_a_session_before_csv_end(self):
        folder = Path(".test-prospective-submit") / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            bars = folder / "bars.csv"
            fields = "timestamp,symbol,open,high,low,close,volume,trade_count,vwap\n"
            rows = []
            for day, value in (("2026-11-02", 100), ("2026-11-03", 101)):
                for symbol in resolve_universe("ETF_LONG_TERM_RESEARCH_EXPANDED"):
                    rows.append(
                        f"{day}T05:00:00Z,{symbol},{value},{value + 1},"
                        f"{value - 1},{value},1000,10,{value}\n"
                    )
            bars.write_text(fields + "".join(rows), encoding="utf-8")
            conditions = {
                "2026-11-02": {"date": "2026-11-02", "trend_state": "BULL"},
            }
            labels = {
                "2026-11-02": {
                    "raw_core_state": "BULL_ACCELERATING__LOW_VOL__MIXED_BREADTH",
                    "core_state": "BULL_ACCELERATING__LOW_VOL__MIXED_BREADTH",
                }
            }
            with patch(
                "research.submit_prospective_shadow.causal_market_conditions",
                return_value=conditions,
            ), patch(
                "research.submit_prospective_shadow.causal_state_labels",
                return_value=labels,
            ):
                payload = build_payload(
                    bars,
                    "2026-11-03",
                    "test",
                    session="2026-11-02",
                )
            self.assertEqual("2026-11-02", payload["session"])
            self.assertEqual(100.0, payload["bars"]["SPY"]["close"])
        finally:
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
