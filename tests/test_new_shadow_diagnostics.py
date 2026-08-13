import unittest

from layers.layer4_shadow_outcomes import update_layer4_shadow_outcome
from layers.live_cohort import latest_completed_live_cohort


class NewShadowDiagnosticTests(unittest.TestCase):
    def test_partial_live_cohort_is_ready_at_configured_minimum(self):
        bars = {symbol: [{"bucket_start": 1000.0}] for symbol in ("AAPL", "AMD", "AMZN")}
        bars["COST"] = []
        cohort = latest_completed_live_cohort(bars, timeframe_seconds=300, required_symbols=3)
        self.assertEqual(cohort["status"], "ready")
        self.assertEqual(cohort["symbols"], ["AAPL", "AMD", "AMZN"])

    def test_live_cohort_waits_below_configured_minimum(self):
        bars = {"AAPL": [{"bucket_start": 1000.0}], "AMD": [{"bucket_start": 1000.0}]}
        cohort = latest_completed_live_cohort(bars, timeframe_seconds=300, required_symbols=3)
        self.assertEqual(cohort["status"], "waiting_for_completed_live_cohort")

    def test_layer4_outcome_marks_horizons_and_avoided_pnl(self):
        item = {"created_epoch": 1000.0, "start_live_price": 100.0, "original_qty": 10.0}
        row, pending = update_layer4_shadow_outcome(
            item, current_price=95.0, now_epoch=4601.0,
            now_iso="2026-08-13T20:00:00+00:00", market_is_open=True,
        )
        self.assertFalse(pending)
        self.assertEqual(row["forward_return_60m"], -0.05)
        self.assertEqual(row["avoided_pnl_60m"], 50.0)

    def test_layer4_outcome_finalizes_partial_at_close(self):
        item = {"created_epoch": 1000.0, "start_live_price": 100.0, "original_qty": 10.0}
        row, pending = update_layer4_shadow_outcome(
            item, current_price=102.0, now_epoch=1700.0,
            now_iso="2026-08-13T20:00:00+00:00", market_is_open=False,
        )
        self.assertFalse(pending)
        self.assertEqual(row["avoided_pnl_10m"], -20.0)
        self.assertIsNone(row["avoided_pnl_30m"])
        self.assertEqual(row["finalized_reason"], "market_closed_partial")


if __name__ == "__main__":
    unittest.main()
