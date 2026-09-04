import unittest

from research.download_alpaca_bars import date_chunks


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


if __name__ == "__main__":
    unittest.main()
