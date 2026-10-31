import unittest

from market.market_data import MarketDataBuffer


class MarketDataMemoryTests(unittest.TestCase):
    def test_pressure_compaction_preserves_strategy_history(self):
        buffer = MarketDataBuffer(
            maxlen_prices=2000,
            maxlen_volumes=2000,
            max_recent_trade_ids_per_symbol=2000,
        )
        for index in range(1200):
            buffer.update_tick(
                "AMD", 100 + index / 1000, 10, 1_700_000_000 + index,
                receipt_timestamp=1_700_000_000 + index,
                trade_id=f"trade-{index}",
            )
        prices_before = buffer.get_recent_prices("AMD")
        bars_before = buffer.get_live_bars("AMD", timeframe_seconds=300)
        result = buffer.compact_for_memory_pressure(1000)
        stats = buffer.memory_stats()
        self.assertEqual(200, result["removed_trade_ids"])
        self.assertFalse(result["strategy_history_changed"])
        self.assertEqual(prices_before, buffer.get_recent_prices("AMD"))
        self.assertEqual(bars_before, buffer.get_live_bars("AMD", timeframe_seconds=300))
        self.assertEqual(1000, stats["recent_trade_ids"])


if __name__ == "__main__":
    unittest.main()
