from dataclasses import dataclass
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
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


if __name__ == "__main__":
    unittest.main()
