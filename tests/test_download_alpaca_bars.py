import unittest

from research.download_alpaca_bars import date_chunks, timestamp_in_requested_range


class AlpacaBarDownloadTests(unittest.TestCase):
    def test_date_chunks_are_nonoverlapping_and_cover_requested_range(self):
        self.assertEqual(
            [("2024-01-01", "2024-01-04"),
             ("2024-01-04", "2024-01-07"),
             ("2024-01-07", "2024-01-10")],
            date_chunks("2024-01-01", "2024-01-10", 3),
        )

    def test_date_chunks_reject_invalid_ranges(self):
        with self.assertRaisesRegex(ValueError, "start must precede end"):
            date_chunks("2024-01-01", "2024-01-01", 3)
        with self.assertRaisesRegex(ValueError, "chunk_days must be positive"):
            date_chunks("2024-01-01", "2024-01-02", 0)

    def test_timestamp_range_is_half_open_even_if_provider_returns_end_date(self):
        self.assertTrue(timestamp_in_requested_range(
            "2026-09-08T19:55:00Z", "2024-01-01", "2026-09-09"
        ))
        self.assertFalse(timestamp_in_requested_range(
            "2026-09-09T13:30:00Z", "2024-01-01", "2026-09-09"
        ))


if __name__ == "__main__":
    unittest.main()
