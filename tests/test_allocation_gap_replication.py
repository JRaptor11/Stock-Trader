import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from research.allocation_gap_replication import build


def _archive(path: Path, start: date) -> None:
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(36)]
    daily = []
    for strategy, daily_return in (
        ("SPY_BUY_HOLD", 0.001),
        ("RISK_ON", 0.002),
        ("DEFENSIVE", 0.0005),
    ):
        for cost in (10.0, 20.0):
            equity = 100000.0
            for day in dates:
                equity *= 1.0 + daily_return
                daily.append({
                    "date": day, "strategy": strategy,
                    "cost_bps": cost, "equity": equity,
                })
    labels = [
        {
            "date": day,
            "trend_state": "BULL" if (index // 3) % 2 == 0 else "BEAR",
        }
        for index, day in enumerate(dates)
    ]
    with zipfile.ZipFile(path, "w") as bundle:
        for name, rows in (
            ("tier1_daily.csv", daily),
            ("tier1_market_state_labels.csv", labels),
        ):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            bundle.writestr(name, stream.getvalue())


def _declaration(path: Path) -> None:
    path.write_text(json.dumps({
        "study_id": "TEST_ALLOCATION_GAP",
        "benchmark_strategy": "SPY_BUY_HOLD",
        "candidates": [
            {"strategy": "RISK_ON", "target_state": "BULL", "role": "risk"},
            {"strategy": "DEFENSIVE", "target_state": "BEAR", "role": "defense"},
        ],
        "trend_states": ["BULL", "BEAR"],
        "primary_family_size": 2,
        "descriptive_family_size": 4,
        "fixed_calendar_blocks": [
            {"id": "OLD", "source": "prehistory", "start": "2008-01-01", "end": "2008-12-31"},
            {"id": "NEW", "source": "modern", "start": "2012-01-01", "end": "2012-12-31"},
        ],
        "costs_bps": [10.0, 20.0],
        "activation_lags_sessions": [0, 1, 3],
        "primary": {"cost_bps": 10.0, "activation_lag_sessions": 0},
        "stress": {"cost_bps": 20.0, "activation_lag_sessions": 1},
        "eligible_block": {"minimum_complete_episodes": 1, "minimum_scored_sessions": 1},
        "reliability_gate": {
            "minimum_complete_episodes": 1,
            "minimum_eligible_blocks": 1,
            "minimum_positive_block_fraction": 0.0,
            "minimum_positive_prehistory_blocks": 0,
            "minimum_positive_modern_blocks": 0,
            "minimum_primary_episode_win_rate": 0.0,
            "minimum_primary_profit_factor": 0.0,
            "maximum_benjamini_hochberg_q": 1.0,
            "minimum_stress_episode_win_rate": 0.0,
        },
    }), encoding="utf-8")


class AllocationGapReplicationTests(unittest.TestCase):
    def test_only_predeclared_role_matched_cells_enter_primary_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, modern = root / "old.zip", root / "modern.zip"
            declaration = root / "declaration.json"
            _archive(old, date(2008, 1, 1))
            _archive(modern, date(2012, 1, 1))
            _declaration(declaration)
            summary = build(declaration, old, modern, root / "out")

            self.assertEqual(2, summary["primary_family_size"])
            self.assertEqual(4, summary["descriptive_family_size"])
            with (root / "out" / "allocation_gap_primary_acceptance.csv").open() as handle:
                primary = list(csv.DictReader(handle))
            self.assertEqual(
                {("RISK_ON", "BULL"), ("DEFENSIVE", "BEAR")},
                {(row["strategy"], row["target_state"]) for row in primary},
            )
            with (root / "out" / "allocation_gap_all_state_scorecard.csv").open() as handle:
                descriptive = list(csv.DictReader(handle))
            primary_rows = [
                row for row in descriptive
                if row["predeclared_primary"] == "True"
                and row["cost_bps"] == "10.0"
                and row["activation_lag_sessions"] == "0"
            ]
            self.assertEqual(2, len(primary_rows))

    def test_insufficient_primary_cells_remain_in_family_with_p_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, modern = root / "old.zip", root / "modern.zip"
            declaration = root / "declaration.json"
            _archive(old, date(2008, 1, 1))
            _archive(modern, date(2012, 1, 1))
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["reliability_gate"]["minimum_complete_episodes"] = 100
            declaration.write_text(json.dumps(payload), encoding="utf-8")
            build(declaration, old, modern, root / "out")
            with (root / "out" / "allocation_gap_primary_acceptance.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({"1.0"}, {row["primary_family_inference_p"] for row in rows})
            self.assertEqual({"1.0"}, {row["primary_bh_q"] for row in rows})

    def test_rejects_incomplete_primary_or_descriptive_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration = root / "declaration.json"
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["primary_family_size"] = 1
            declaration.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "primary_family_size"):
                build(declaration, root / "missing.zip", root / "missing2.zip", root / "out")


if __name__ == "__main__":
    unittest.main()
