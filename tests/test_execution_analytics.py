import unittest

from diagnostics.execution_analytics import build_execution_analytics


class ExecutionAnalyticsTests(unittest.TestCase):
    def test_turnover_reversals_costs_and_realized_pnl(self):
        snapshots = {
            "open": {
                "account": {
                    "equity": 100000,
                    "last_equity": 100000,
                },
                "positions": [{
                    "symbol": "AMD",
                    "qty": 10,
                    "avg_entry_price": 100,
                    "unrealized_pl": 0,
                }],
            },
            "close": {
                "account": {"equity": 101000},
                "positions": [{
                    "symbol": "AMD",
                    "qty": 8,
                    "avg_entry_price": 102,
                    "current_price": 108,
                    "unrealized_pl": 40,
                }],
                "benchmarks": [{
                    "symbol": "SPY",
                    "return_pct": 0.5,
                }],
                "orders": [
                    {
                        "id": "sell-1",
                        "symbol": "AMD",
                        "side": "sell",
                        "status": "filled",
                        "filled_qty": 4,
                        "filled_avg_price": 110,
                        "submitted_at": "2026-08-03T14:00:00+00:00",
                        "filled_at": "2026-08-03T14:00:01+00:00",
                    },
                    {
                        "id": "buy-2",
                        "symbol": "AMD",
                        "side": "buy",
                        "status": "filled",
                        "filled_qty": 2,
                        "filled_avg_price": 106,
                        "submitted_at": "2026-08-03T15:00:00+00:00",
                        "filled_at": "2026-08-03T15:00:01+00:00",
                    },
                ],
            },
        }

        result = build_execution_analytics(
            snapshots,
            trade_date="2026-08-03",
            execution_rows=[
                {
                    "order_id": "sell-1",
                    "trade_attribution": "genuine_ranking_change",
                    "trade_attribution_evidence": "test",
                },
                {
                    "order_id": "buy-2",
                    "trade_attribution": "target_size_change",
                    "trade_attribution_evidence": "test",
                },
            ],
            plan_rows=[
                {
                    "timestamp": "2026-08-03T14:01:00+00:00",
                    "symbol": "AMD",
                    "decision": "SELL",
                    "planned_qty": 4,
                    "planned_notional": 440,
                    "live_price": 110,
                },
                {
                    "timestamp": "2026-08-03T14:06:00+00:00",
                    "symbol": "AMD",
                    "decision": "BUY",
                    "planned_qty": 2,
                    "planned_notional": 212,
                    "live_price": 106,
                },
            ],
        )

        self.assertEqual(2, result["filled_order_count"])
        self.assertEqual(1, result["same_day_round_trip_symbol_count"])
        self.assertEqual(1, result["direction_reversal_count"])
        self.assertAlmostEqual(40, result["broker_fill_realized_pnl_estimate"])
        self.assertAlmostEqual(652, result["gross_traded_notional"])
        self.assertEqual(4, len(result["cost_sensitivity"]))
        self.assertAlmostEqual(
            0.5,
            result["benchmark_excess"][0][
                "strategy_excess_return_percentage_points"
            ],
        )
        self.assertEqual([], result["quantity_reconstruction_mismatches"])
        symbol = result["symbol_analytics"][0]
        self.assertIsNotNone(symbol["gross_turnover_pct_of_average_equity"])
        self.assertAlmostEqual(
            8,
            result["first_vs_follow_up_performance"][
                "first_order_mark_to_close_pnl"
            ],
        )
        self.assertAlmostEqual(
            4,
            result["first_vs_follow_up_performance"][
                "follow_up_mark_to_close_pnl"
            ],
        )
        self.assertEqual(
            3, len(result["target_update_interval_counterfactuals"])
        )
        self.assertEqual(
            5, len(result["small_follow_up_counterfactuals"])
        )
        self.assertEqual(
            1,
            result["trade_attribution"]["genuine_ranking_change"][
                "order_count"
            ],
        )
        self.assertEqual(
            0, result["attribution_coverage"]["unknown_order_count"]
        )

    def test_intraday_thresholds_include_exited_positions(self):
        snapshots = {
            "open": {
                "account": {"equity": 100000, "last_equity": 100000},
                "positions": [],
            },
            "close": {
                "account": {"equity": 99000},
                "positions": [],
                "traded_symbol_prices": [{
                    "symbol": "GOOGL",
                    "price": 96,
                }],
                "orders": [{
                    "id": "failsafe-sell",
                    "symbol": "GOOGL",
                    "side": "sell",
                    "status": "filled",
                    "filled_qty": 10,
                    "filled_avg_price": 94,
                    "submitted_at": "2026-08-03T16:01:00+00:00",
                    "filled_at": "2026-08-03T16:01:01+00:00",
                }],
            },
        }
        observations = [
            {
                "timestamp": "2026-08-03T15:59:50+00:00",
                "symbol": "GOOGL",
                "position_qty": 10,
                "entry_price": 100,
                "current_price": 96,
                "loss_percent": 4,
                "confirmed_crossing_4_percent": "false",
            },
            {
                "timestamp": "2026-08-03T16:00:00+00:00",
                "symbol": "GOOGL",
                "position_qty": 10,
                "entry_price": 100,
                "current_price": 95.5,
                "loss_percent": 4.5,
                "confirmed_crossing_4_percent": "true",
            },
        ]

        result = build_execution_analytics(
            snapshots,
            trade_date="2026-08-03",
            execution_rows=[{
                "order_id": "failsafe-sell",
                "trade_attribution": "risk_or_fail_safe",
                "trade_attribution_detail": "forced_risk_liquidation",
            }],
            fail_safe_observation_rows=observations,
        )

        comparison = next(
            row
            for row in result["position_loss_threshold_comparison"]
            if row["threshold_percent"] == 4
        )
        self.assertEqual(1, comparison["confirmed_crossing_count"])
        crossing = comparison["confirmed_crossings"][0]
        self.assertEqual("GOOGL", crossing["symbol"])
        self.assertAlmostEqual(-5, crossing[
            "hypothetical_pnl_vs_regular_close"
        ])
        self.assertAlmostEqual(15, crossing[
            "hypothetical_pnl_vs_actual_fail_safe_exit"
        ])


if __name__ == "__main__":
    unittest.main()
