import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research.historical_replay import (
    ReplayConfig, ReplayPortfolio, SpilledRows, _account_profile_summaries,
    _checkpoint_interval, _cross_account_wash_sale_matrix, _future_labels, _record_tax_fill,
    _portfolio_checkpoint, _portfolio_from_checkpoint, _timestamp, _walk_forward_results,
    load_bar_csv, run_replay, write_replay, write_replay_archive,
)
from research.replay_parity import compare_replay_to_live
from research.walk_forward import build_walk_forward_folds
from layers.layer_research_strategy import STRATEGIES


class HistoricalReplayTests(unittest.TestCase):
    def test_checkpoint_interval_becomes_more_frequent_near_completion(self):
        self.assertEqual(10, _checkpoint_interval(0.50, 10))
        self.assertEqual(5, _checkpoint_interval(0.75, 10))
        self.assertEqual(1, _checkpoint_interval(0.90, 10))

    def test_tax_ledger_tracks_holding_period_and_forward_wash_exposure(self):
        portfolio = ReplayPortfolio(cash=100000.0)
        acquired = datetime(2024, 1, 2, tzinfo=timezone.utc)
        sold = datetime(2026, 1, 5, tzinfo=timezone.utc)
        _record_tax_fill(portfolio, "AAA", "BUY", 10, 100.0, acquired)
        _record_tax_fill(portfolio, "AAA", "SELL", 5, 120.0, sold)
        _record_tax_fill(portfolio, "AAA", "SELL", 5, 90.0, sold + timedelta(days=1))
        _record_tax_fill(portfolio, "AAA", "BUY", 5, 91.0, sold + timedelta(days=10))
        self.assertEqual(100.0, portfolio.realized_long_term_gain)
        self.assertEqual(50.0, portfolio.realized_loss)
        self.assertEqual(50.0, portfolio.wash_sale_loss_exposure)
        self.assertEqual(101.0, portfolio.tax_lots["AAA"][0]["cost_per_share"])

    def test_tax_events_are_compacted_by_symbol_and_day(self):
        portfolio = ReplayPortfolio(cash=100000.0)
        started = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        _record_tax_fill(portfolio, "AAA", "BUY", 2, 100.0, started)
        _record_tax_fill(
            portfolio, "AAA", "BUY", 3, 101.0, started + timedelta(minutes=5)
        )
        self.assertEqual(1, len(portfolio.buy_events))
        self.assertEqual(5.0, portfolio.buy_events[0]["qty"])

    def test_portfolio_checkpoint_does_not_deep_copy_large_event_lists(self):
        portfolio = ReplayPortfolio(
            cash=100000.0,
            buy_events=[{"symbol": "AAA", "bought_at": "2026-01-05T14:30:00+00:00", "qty": 2}],
        )
        checkpoint = _portfolio_checkpoint(portfolio)
        self.assertIs(portfolio.buy_events, checkpoint["buy_events"])

    def test_legacy_checkpoint_tax_events_are_compacted_during_restore(self):
        restored = _portfolio_from_checkpoint({
            "cash": 100000.0,
            "buy_events": [
                {"symbol": "AAA", "bought_at": "2026-01-05T14:30:00+00:00", "qty": 2},
                {"symbol": "AAA", "bought_at": "2026-01-05T15:30:00+00:00", "qty": 3},
            ],
            "loss_sale_events": [
                {"symbol": "AAA", "sold_at": "2026-01-06T14:30:00+00:00", "qty": 2, "loss_per_share": 5},
                {"symbol": "AAA", "sold_at": "2026-01-06T15:30:00+00:00", "qty": 3, "loss_per_share": 10},
            ],
        })
        self.assertEqual(1, len(restored.buy_events))
        self.assertEqual(5.0, restored.buy_events[0]["qty"])
        self.assertEqual(1, len(restored.loss_sale_events))
        self.assertEqual(8.0, restored.loss_sale_events[0]["loss_per_share"])

    def test_legacy_checkpoint_tax_lots_are_compacted_during_restore(self):
        restored = _portfolio_from_checkpoint({
            "cash": 100000.0,
            "tax_lots": {"AAA": [
                {"qty": 2, "cost_per_share": 100, "acquired_at": "2026-01-05T14:30:00+00:00"},
                {"qty": 3, "cost_per_share": 110, "acquired_at": "2026-01-05T15:30:00+00:00"},
                {"qty": 1, "cost_per_share": 120, "acquired_at": "2026-01-06T14:30:00+00:00"},
            ]},
        })
        self.assertEqual(2, len(restored.tax_lots["AAA"]))
        self.assertEqual(5.0, restored.tax_lots["AAA"][0]["qty"])
        self.assertEqual(106.0, restored.tax_lots["AAA"][0]["cost_per_share"])

    def test_account_profiles_report_taxable_and_roth_outputs(self):
        portfolio = ReplayPortfolio(
            cash=110000.0, realized_short_term_gain=10000.0,
        )
        summaries = [{
            "strategy_name": "TEST", "final_equity": 110000.0,
            "turnover": 20000.0, "trade_count": 2, "max_drawdown_pct": -0.1,
        }]
        rows = _account_profile_summaries(
            {"TEST": portfolio}, summaries, {},
            datetime(2026, 1, 5, tzinfo=timezone.utc),
            ReplayConfig(taxable_short_term_rate=0.30, taxable_state_rate=0.0),
        )
        by_profile = {row["account_profile"]: row for row in rows}
        self.assertEqual(107000.0, by_profile["CA_SINGLE_105K"]["estimated_after_tax_equity"])
        self.assertEqual(110000.0, by_profile["ROTH_IRA"]["estimated_after_tax_equity"])

    def test_cross_account_matrix_checks_buys_before_and_after_loss_sale(self):
        taxable = ReplayPortfolio(cash=100000.0)
        roth = ReplayPortfolio(cash=100000.0)
        sold_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
        taxable.loss_sale_events = [{
            "symbol": "AAA", "sold_at": sold_at.isoformat(),
            "qty": 10, "loss_per_share": 5.0,
        }]
        roth.buy_events = [
            {"symbol": "AAA", "bought_at": (sold_at - timedelta(days=10)).isoformat(), "qty": 4},
            {"symbol": "AAA", "bought_at": (sold_at + timedelta(days=10)).isoformat(), "qty": 6},
        ]
        rows = _cross_account_wash_sale_matrix({"TAXABLE": taxable, "ROTH": roth})
        match = next(
            row for row in rows
            if row["taxable_strategy"] == "TAXABLE" and row["roth_strategy"] == "ROTH"
        )
        self.assertEqual(10.0, match["matched_shares"])
        self.assertEqual(50.0, match["potential_permanently_disallowed_loss"])

    def _bars(self, count=76):
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        rows = []
        for index in range(count):
            ts = start + timedelta(minutes=5 * index)
            for symbol, offset in (("AAA", 0.0), ("BBB", 10.0)):
                price = 100.0 + offset + index * (0.05 if symbol == "AAA" else -0.02)
                rows.append({
                    "timestamp": ts, "symbol": symbol, "open": price,
                    "high": price + 0.1, "low": price - 0.1, "close": price + 0.02,
                    "volume": 1000 + index, "trade_count": 100 + index, "vwap": price,
                })
        return rows

    def test_replay_generates_stateful_outputs_and_future_labels(self):
        result = run_replay(self._bars(), ReplayConfig(warmup_bars=61, min_train_sessions=1, test_sessions=1, require_benchmark=False))
        self.assertTrue(result["cycles"])
        self.assertTrue(result["orders"])
        self.assertTrue(result["dataset"])
        self.assertIn("forward_return_30m", result["dataset"][0])
        self.assertEqual(
            len({row["strategy_name"] for row in result["cycles"]}),
            len(STRATEGIES),
        )
        first_order = result["orders"][0]
        self.assertGreater(first_order["timestamp"], first_order["source_bar_timestamp"])
        self.assertGreaterEqual(first_order["timestamp"], first_order["decision_available_at"])
        self.assertTrue(result["daily"])
        self.assertIn("overnight_pnl", result["daily"][0])

    def test_checkpoint_carries_portfolio_and_rolling_history(self):
        first = run_replay(
            self._bars(),
            ReplayConfig(warmup_bars=61, require_benchmark=False),
        )
        checkpoint = first["checkpoint"]
        self.assertTrue(checkpoint["portfolios"])
        self.assertEqual(61, len(checkpoint["history"]["AAA"]))
        self.assertTrue(checkpoint["walk_forward_daily_history"])
        second_rows = []
        start = datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc)
        for index in range(2):
            for symbol, offset in (("AAA", 0.0), ("BBB", 10.0)):
                price = 104 + offset + index * 0.05
                second_rows.append({
                    "timestamp": start + timedelta(minutes=5 * index),
                    "symbol": symbol, "open": price, "high": price + .1,
                    "low": price - .1, "close": price + .02,
                    "volume": 1000, "trade_count": 100, "vwap": price,
                })
        continued = run_replay(
            second_rows,
            ReplayConfig(
                warmup_bars=61, require_benchmark=False,
                minimum_eligible_symbols=2,
            ),
            initial_checkpoint=checkpoint,
        )
        self.assertTrue(continued["cycles"])
        self.assertGreater(continued["checkpoint"]["cycle_id"], checkpoint["cycle_id"])
        self.assertGreater(
            len(continued["checkpoint"]["walk_forward_daily_history"]),
            len(checkpoint["walk_forward_daily_history"]),
        )

    def test_future_label_stage_reports_progress_without_changing_rows(self):
        bars = self._bars(14)
        by_symbol = {}
        for row in bars:
            by_symbol.setdefault(row["symbol"], []).append(
                (row["timestamp"], row)
            )
        features = [{
            "timestamp": row["timestamp"].isoformat(),
            "symbol": row["symbol"], "close": row["close"],
        } for row in bars[:6]]
        progress = []
        labeled = _future_labels(
            features, by_symbol, progress_callback=progress.append,
            yield_every=2,
        )
        self.assertEqual(len(features), len(labeled))
        self.assertEqual(100.0, progress[-1]["stage_percent_complete"])
        self.assertEqual("building_future_labels", progress[-1]["stage"])

    def test_loader_rejects_duplicate_symbol_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                row = {"timestamp": "2026-01-05T14:30:00+00:00", "symbol": "AAA", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                writer.writerow(row); writer.writerow(row)
            with self.assertRaises(ValueError):
                load_bar_csv(path)

    def test_loader_rejects_timezone_naive_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                writer.writerow({"timestamp": "2026-01-05T14:30:00", "symbol": "AAA", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
            with self.assertRaises(ValueError):
                load_bar_csv(path)

    def test_loader_filters_inclusive_market_session_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "timestamp", "symbol", "open", "high", "low",
                        "close", "volume",
                    ],
                )
                writer.writeheader()
                for day in ("2024-12-31", "2025-01-02", "2026-01-02"):
                    writer.writerow({
                        "timestamp": f"{day}T14:30:00+00:00",
                        "symbol": "AAA", "open": 1, "high": 1,
                        "low": 1, "close": 1, "volume": 1,
                    })
            rows = load_bar_csv(
                path, start_date="2025-01-01", end_date="2025-12-31"
            )
            self.assertEqual(1, len(rows))
            self.assertEqual("2025-01-02", rows[0]["timestamp"].date().isoformat())

    def test_loader_filters_symbols_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "symbol", "open", "high", "low", "close", "volume",
                ])
                writer.writeheader()
                for symbol in ("AAA", "BBB"):
                    writer.writerow({
                        "timestamp": "2026-01-05T14:30:00+00:00", "symbol": symbol,
                        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
                    })
            rows = load_bar_csv(path, include_symbols={"AAA"})
            self.assertEqual(["AAA"], [row["symbol"] for row in rows])

    def test_replay_config_rejects_inverted_date_range(self):
        with self.assertRaisesRegex(ValueError, "data_start_date"):
            ReplayConfig(
                data_start_date="2026-01-01", data_end_date="2025-01-01"
            )

    def test_benchmark_is_context_not_candidate_by_default(self):
        rows = self._bars()
        for index in range(76):
            ts = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
            rows.append({"timestamp": ts, "symbol": "SPY", "open": 500, "high": 501, "low": 499, "close": 500.5, "volume": 10000, "trade_count": 1000, "vwap": 500})
        result = run_replay(rows, ReplayConfig(warmup_bars=61))
        self.assertEqual(result["symbols"], ["AAA", "BBB"])
        self.assertIn("benchmark_ret_60m", result["dataset"][0])
        self.assertTrue(result["benchmark_daily"])
        self.assertEqual(result["dataset_quality"]["minimum_coverage_pct"], 100.0)
        self.assertEqual(result["dataset_quality"]["average_coverage_pct"], 100.0)

    def test_walk_forward_is_strictly_chronological(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 11)]
        folds = build_walk_forward_folds(dates, min_train_sessions=4, test_sessions=2, step_sessions=2)
        self.assertEqual(len(folds), 3)
        for fold in folds:
            self.assertLess(fold.train_end, fold.test_start)
            self.assertTrue(set(fold.train_dates).isdisjoint(fold.test_dates))

    def test_walk_forward_selects_on_train_and_scores_held_out_test(self):
        days = ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04")
        folds = build_walk_forward_folds(days, min_train_sessions=2, test_sessions=2)
        daily = []
        for strategy, equities in {
            "TRAIN_WINNER": [100, 110, 99, 89.1],
            "TEST_WINNER": [100, 99, 108.9, 119.79],
        }.items():
            for day, equity in zip(days, equities):
                daily.append({
                    "session_date": day, "strategy_name": strategy,
                    "first_equity": 100, "last_equity": equity,
                })
        benchmark = [{"session_date": day, "session_return": 0.0} for day in days]
        results = _walk_forward_results(daily, benchmark, folds)
        self.assertEqual(results[0]["selected_strategy"], "TRAIN_WINNER")
        self.assertLess(results[0]["selected_test_return"], 0)

    def test_walk_forward_defaults_to_spy_when_tactical_edge_is_too_small(self):
        days = tuple(f"2026-01-{day:02d}" for day in range(1, 7))
        folds = build_walk_forward_folds(
            days, min_train_sessions=4, test_sessions=2, step_sessions=2,
        )
        daily = []
        equity = 100.0
        for day in days:
            start = equity
            equity *= 1.0015
            daily.append({
                "session_date": day, "strategy_name": "TINY_EDGE",
                "first_equity": start, "last_equity": equity,
                "gross_turnover": 0.0,
            })
        benchmark = [
            {"session_date": day, "session_return": 0.001}
            for day in days
        ]
        results = _walk_forward_results(daily, benchmark, folds)
        self.assertEqual("SPY_BUY_HOLD", results[0]["selected_strategy"])

    def test_walk_forward_allows_material_tactical_edge_over_spy(self):
        days = tuple(f"2026-01-{day:02d}" for day in range(1, 7))
        folds = build_walk_forward_folds(
            days, min_train_sessions=4, test_sessions=2, step_sessions=2,
        )
        daily = []
        equity = 100.0
        for day in days:
            start = equity
            equity *= 1.01
            daily.append({
                "session_date": day, "strategy_name": "CLEAR_EDGE",
                "first_equity": start, "last_equity": equity,
                "gross_turnover": 0.0,
            })
        benchmark = [
            {"session_date": day, "session_return": 0.001}
            for day in days
        ]
        results = _walk_forward_results(daily, benchmark, folds)
        self.assertEqual("CLEAR_EDGE", results[0]["selected_strategy"])

    def test_walk_forward_can_select_cash_when_trading_scores_are_negative(self):
        days = tuple(f"2026-01-{day:02d}" for day in range(1, 7))
        folds = build_walk_forward_folds(
            days, min_train_sessions=4, test_sessions=2, step_sessions=2,
        )
        daily = []
        equity = 100.0
        for day in days:
            start = equity
            equity *= 0.99
            daily.append({
                "session_date": day, "strategy_name": "LOSER",
                "first_equity": start, "last_equity": equity,
                "gross_turnover": 100.0,
            })
        benchmark = [
            {"session_date": day, "session_return": -0.001}
            for day in days
        ]
        results = _walk_forward_results(daily, benchmark, folds)
        self.assertEqual("CASH", results[0]["selected_strategy"])
        self.assertEqual(0.0, results[0]["selected_test_return"])

    def test_writer_produces_reproducibility_manifest(self):
        result = run_replay(self._bars(), ReplayConfig(warmup_bars=61, require_benchmark=False))
        with tempfile.TemporaryDirectory() as directory:
            output = write_replay(result, directory)
            self.assertTrue((output / "replay_manifest.json").exists())
            self.assertTrue((output / "ml_dataset.csv").exists())
            self.assertTrue((output / "dataset_quality.json").exists())
            self.assertTrue((output / "account_profile_results.csv").exists())
            self.assertTrue((output / "account_profile_assumptions.json").exists())
            self.assertTrue((output / "cross_account_wash_sale_matrix.csv").exists())
            self.assertTrue((output / "universe_metadata.json").exists())
            self.assertTrue((output / "universe_selection_diagnostics.csv").exists())

    def test_writer_accepts_precomputed_hash_after_cache_cleanup(self):
        result = run_replay(
            self._bars(), ReplayConfig(warmup_bars=61, require_benchmark=False)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bars.csv"
            source.write_bytes(b"cached historical bars")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            source.unlink()
            output = write_replay(
                result, root / "output", source_path=source,
                source_sha256=digest,
            )
            manifest = json.loads(
                (output / "replay_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(digest, manifest["source_sha256"])

    def test_spilled_replay_matches_in_memory_summary_and_outputs(self):
        rows = self._bars()
        config = ReplayConfig(warmup_bars=61, require_benchmark=False)
        expected = run_replay(rows, config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = run_replay(rows, config, spill_directory=root / "spill")
            self.assertEqual(actual["summary"], expected["summary"])
            self.assertEqual(actual["daily"], expected["daily"])
            self.assertIsInstance(actual["cycles"], SpilledRows)
            output = write_replay(actual, root / "output")
            self.assertGreater((output / "replay_cycles.csv").stat().st_size, 0)
            for value in actual.values():
                if isinstance(value, SpilledRows):
                    value.close()

    def test_direct_archive_releases_spills_after_each_member(self):
        rows = self._bars()
        config = ReplayConfig(warmup_bars=61, require_benchmark=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_replay(rows, config, spill_directory=root / "spill")
            spill_paths = [
                value.path for value in result.values()
                if isinstance(value, SpilledRows)
            ]
            archive = write_replay_archive(
                result, root / "results.zip", release_spills=True,
            )
            self.assertTrue(archive.is_file())
            self.assertTrue(all(not path.exists() for path in spill_paths))
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("replay_manifest.json", names)
                self.assertIn("ml_dataset.csv", names)

    def test_direct_archive_enables_zip64_for_csv_members(self):
        rows = self._bars()
        config = ReplayConfig(warmup_bars=61, require_benchmark=False)
        original_open = zipfile.ZipFile.open
        write_flags = []

        def tracking_open(bundle, name, mode="r", pwd=None, *, force_zip64=False):
            if mode == "w":
                write_flags.append((name, force_zip64))
            return original_open(
                bundle, name, mode=mode, pwd=pwd, force_zip64=force_zip64,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_replay(rows, config, spill_directory=root / "spill")
            with patch.object(zipfile.ZipFile, "open", new=tracking_open):
                write_replay_archive(result, root / "results.zip", release_spills=True)
        csv_write_flags = [
            force_zip64 for name, force_zip64 in write_flags
            if str(name).endswith(".csv")
        ]
        self.assertTrue(csv_write_flags)
        self.assertTrue(all(csv_write_flags))

    def test_replay_requires_benchmark_by_default(self):
        with self.assertRaisesRegex(ValueError, "required benchmark"):
            run_replay(self._bars(), ReplayConfig(warmup_bars=61))

    def test_replay_rejects_materially_incomplete_symbol_coverage(self):
        rows = self._bars()
        rows = [row for row in rows if not (row["symbol"] == "BBB" and row["timestamp"].minute % 10 == 0)]
        with self.assertRaisesRegex(ValueError, "coverage"):
            run_replay(rows, ReplayConfig(warmup_bars=61, require_benchmark=False))

    def test_future_labels_allow_sparse_symbol_at_decision_timestamp(self):
        rows = self._bars(80)
        missing_timestamp = datetime(2026, 1, 5, 19, 35, tzinfo=timezone.utc)
        rows = [
            row for row in rows
            if not (row["symbol"] == "BBB" and row["timestamp"] == missing_timestamp)
        ]
        result = run_replay(
            rows,
            ReplayConfig(
                warmup_bars=61, require_benchmark=False,
                minimum_average_coverage_pct=95.0,
            ),
        )
        sparse = [
            row for row in result["dataset"]
            if row["symbol"] == "BBB" and row["timestamp"] == missing_timestamp.isoformat()
        ]
        self.assertEqual(len(sparse), 1)
        self.assertIn("forward_return_30m", sparse[0])

    def test_timestamp_freshness_excludes_only_stale_symbol_cycles(self):
        rows = self._bars(80)
        missing = datetime(2026, 1, 5, 20, 55, tzinfo=timezone.utc)
        rows = [
            row for row in rows
            if not (row["symbol"] == "BBB" and row["timestamp"] == missing)
        ]
        result = run_replay(
            rows,
            ReplayConfig(
                warmup_bars=61, require_benchmark=False,
                maximum_candidate_bar_age_minutes=0,
                minimum_eligible_symbols=1,
            ),
        )
        stale = [
            row for row in result["eligibility"]
            if row["symbol"] == "BBB" and row["timestamp"] == missing.isoformat()
        ]
        self.assertEqual(1, len(stale))
        self.assertFalse(stale[0]["eligible"])
        self.assertEqual(5.0, stale[0]["bar_age_minutes"])

    def test_overnight_strategy_is_flat_until_late_session(self):
        result = run_replay(
            self._bars(),
            ReplayConfig(warmup_bars=61, require_benchmark=False),
        )
        rows = [
            row for row in result["cycles"]
            if row["strategy_name"] == "OVERNIGHT_REGIME_300M"
        ]
        self.assertTrue(rows)
        early = [row for row in rows if _timestamp(row["timestamp"]).hour < 20]
        self.assertTrue(all(row["target_cash_pct"] == 1.0 for row in early))

    def test_replay_parity_matches_on_source_bar_and_strategy(self):
        replay = [{
            "timestamp": "2026-01-05T15:00:00+00:00", "strategy_name": "LOOKBACK_60M",
            "equity": 100010, "turnover": 5000, "trade_count": 2, "drawdown_pct": -0.001,
        }]
        live = [{
            "source_bar_timestamp": "2026-01-05T15:00:00+00:00", "strategy_name": "LOOKBACK_60M",
            "shadow_equity": 100000, "cumulative_gross_turnover": 4900,
            "cumulative_trade_count": 2, "drawdown_pct": -0.0012,
        }]
        result = compare_replay_to_live(replay, live)
        self.assertEqual(result["matched_cycle_count"], 1)
        summary = result["strategy_summaries"][0]
        self.assertEqual(summary["equity_final_difference"], 10.0)
        self.assertEqual(summary["turnover_final_difference"], 100.0)


if __name__ == "__main__":
    unittest.main()
