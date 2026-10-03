from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from core.state import app_state
from diagnostics import daily_review


@dataclass
class Account:
    equity: float = 101000
    last_equity: float = 100000
    cash: float = 50000
    buying_power: float = 200000
    portfolio_value: float = 101000
    long_market_value: float = 51000
    short_market_value: float = 0


@dataclass
class Position:
    symbol: str = "AMD"
    qty: float = 10
    avg_entry_price: float = 100
    current_price: float = 105
    market_value: float = 1050
    cost_basis: float = 1000
    unrealized_pl: float = 50
    unrealized_plpc: float = 0.05
    unrealized_intraday_pl: float = 20
    unrealized_intraday_plpc: float = 0.02
    change_today: float = 0.02
    side: str = "long"


class Client:
    def get_account(self):
        return Account()

    def get_all_positions(self):
        return [Position()]

    def get_orders(self, filter=None):
        return []

    def get_clock(self):
        return types.SimpleNamespace(is_open=False)


class DailyReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_and_package_contains_review_artifacts(self):
        app_state["trading_client"] = Client()
        app_state["stock_data_client"] = None
        app_state["daily_review"] = {
            "trade_date": None,
            "snapshots": {},
            "package_created_for": None,
            "latest_package": None,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(daily_review, "layer_csv_path", side_effect=lambda name: root / name),
                patch.object(daily_review, "REVIEW_PACKAGE_DIR", root / "packages"),
                patch.object(
                    daily_review,
                    "_fetch_daily_snapshot_sync",
                    return_value=(Account(), [Position()], []),
                ),
            ):
                (root / "fail_safe_position_observations.csv").write_text(
                    "timestamp,symbol,loss_percent\n"
                    "2026-07-31T15:00:00+00:00,AMD,4.0\n",
                    encoding="utf-8",
                )
                (root / "layer_live_cohort_diagnostics.csv").write_text(
                    "timestamp,symbol,reason_code\n"
                    "2026-07-31T15:00:00+00:00,COST,LAGGING_BUCKET\n",
                    encoding="utf-8",
                )
                await daily_review.capture_daily_snapshot(
                    "open",
                    capture_reason="test_open",
                    market_is_open=True,
                )
                await daily_review.capture_daily_snapshot(
                    "close",
                    capture_reason="test_close",
                    market_is_open=False,
                )
                package = daily_review.build_daily_review_package("2026-07-31")

                self.assertTrue(package.exists())
                with zipfile.ZipFile(package) as archive:
                    names = set(archive.namelist())
                    self.assertIn("manifest.json", names)
                    self.assertIn("snapshots.json", names)
                    self.assertIn("daily_summary.json", names)
                    self.assertIn("execution_analytics.json", names)
                    self.assertIn("config_redacted.json", names)
                    self.assertIn("daily_account_snapshots.csv", names)
                    self.assertIn("daily_position_snapshots.csv", names)
                    self.assertIn(
                        "fail_safe_position_observations.csv",
                        names,
                    )
                    self.assertIn("layer_live_cohort_diagnostics.csv", names)

    def test_diagnostic_reader_filters_large_file_while_streaming(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write("timestamp,symbol\n")
                for index in range(20_000):
                    handle.write(f"2026-07-30T15:00:{index % 60:02d}+00:00,OLD\n")
                handle.write("2026-07-31T15:00:00+00:00,AMD\n")

            with patch.object(daily_review, "layer_csv_path", return_value=path):
                rows = daily_review._read_diagnostic_rows(
                    "orders.csv", "2026-07-31"
                )

            self.assertEqual([row["symbol"] for row in rows], ["AMD"])

    def test_package_build_is_deferred_above_memory_safety_limit(self):
        app_state["daily_review"] = {"snapshots": {}}
        with (
            patch.object(daily_review, "_process_rss_mb", return_value=450.0),
            patch.dict(
                daily_review.os.environ,
                {"DAILY_REVIEW_MAX_BUILD_RSS_MB": "400"},
            ),
        ):
            with self.assertRaisesRegex(MemoryError, "deferred"):
                daily_review.build_daily_review_package("2026-07-31")

        state = app_state["daily_review"]
        self.assertIn("package_deferred_reason", state)
        self.assertEqual(state["package_build_rss_mb"], 450.0)

    async def test_after_hours_snapshot_does_not_rebuild_full_package_by_default(self):
        shutdown = __import__("asyncio").Event()
        app_state["stream"] = {"shutdown_event": shutdown}
        app_state["trading_client"] = Client()
        app_state["daily_review"] = {
            "trade_date": None,
            "snapshots": {},
            "package_created_for": None,
        }

        fixed_now = datetime(2026, 10, 3, 0, 5, tzinfo=timezone.utc)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        async def capture(snapshot_type, **_kwargs):
            app_state["daily_review"]["snapshots"][snapshot_type] = {
                "trade_date": "2026-10-02"
            }

        async def stop_after_pass(_seconds):
            shutdown.set()

        build = unittest.mock.Mock(return_value=Path("review.zip"))
        with (
            patch.object(daily_review, "datetime", FixedDateTime),
            patch.object(daily_review, "capture_daily_snapshot", side_effect=capture),
            patch.object(daily_review, "build_daily_review_package", build),
            patch.object(daily_review.asyncio, "sleep", side_effect=stop_after_pass),
            patch.dict(
                daily_review.os.environ,
                {"DAILY_REVIEW_REBUILD_AFTER_HOURS": "false"},
            ),
        ):
            await daily_review.run_daily_review_monitor(poll_seconds=0)

        self.assertEqual(build.call_count, 1)
        self.assertIn(
            "after_hours_close",
            app_state["daily_review"]["snapshots"],
        )
        self.assertIn(
            "after_hours_package_rebuild_skipped_at",
            app_state["daily_review"],
        )


if __name__ == "__main__":
    unittest.main()
