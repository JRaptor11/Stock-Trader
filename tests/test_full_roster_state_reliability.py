import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from research.full_roster_state_reliability import _bh_adjust, build


def _archive(path: Path, start: date) -> None:
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(30)]
    daily = []
    for strategy, daily_return in (
        ("SPY_BUY_HOLD", 0.001), ("ALPHA", 0.002), ("BETA", 0.0005),
    ):
        for cost in (10.0, 20.0):
            equity = 100000.0
            for day in dates:
                equity *= 1.0 + daily_return
                daily.append({
                    "date": day, "strategy": strategy,
                    "cost_bps": cost, "equity": equity,
                })
    labels = []
    for index, day in enumerate(dates):
        labels.append({
            "date": day,
            "trend_state": "STATE_A" if (index // 3) % 2 == 0 else "STATE_B",
        })
    trades = [
        {"date": dates[0], "strategy": strategy, "cost_bps": cost,
         "symbol": symbol, "notional": 100000.0}
        for strategy, symbol in (("ALPHA", "QQQ"), ("BETA", "SHY"))
        for cost in (10.0, 20.0)
    ]
    with zipfile.ZipFile(path, "w") as bundle:
        for name, rows in (
            ("tier1_daily.csv", daily),
            ("tier1_market_state_labels.csv", labels),
            ("tier1_trades.csv", trades),
        ):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            bundle.writestr(name, stream.getvalue())


def _declaration(path: Path) -> None:
    path.write_text(json.dumps({
        "study_id": "TEST_FULL_ROSTER",
        "benchmark_strategy": "SPY_BUY_HOLD",
        "strategies": ["ALPHA", "BETA"],
        "trend_states": ["STATE_A", "STATE_B"],
        "family_size": 4,
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
            "minimum_positive_block_fraction": 0.5,
            "minimum_primary_episode_win_rate": 0.5,
            "minimum_primary_profit_factor": 1.0,
            "maximum_benjamini_hochberg_q": 1.0,
            "minimum_stress_episode_win_rate": 0.5,
        },
    }), encoding="utf-8")


class FullRosterStateReliabilityTests(unittest.TestCase):
    def test_bh_adjustment_is_monotone_in_rank(self):
        adjusted = _bh_adjust([0.01, 0.03, 0.02, 1.0])
        ranked = sorted(zip([0.01, 0.03, 0.02, 1.0], adjusted))
        self.assertEqual(sorted(value for _, value in ranked), [value for _, value in ranked])
        self.assertAlmostEqual(0.04, adjusted[0])

    def test_complete_family_and_boundary_exclusions_are_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, modern = root / "old.zip", root / "modern.zip"
            declaration = root / "declaration.json"
            _archive(old, date(2008, 1, 1))
            _archive(modern, date(2012, 1, 1))
            _declaration(declaration)
            summary = build(declaration, old, modern, root / "out")

            self.assertEqual(4, summary["family_size"])
            self.assertEqual("none", summary["router_effect"])
            with (root / "out" / "full_roster_state_acceptance.csv").open() as handle:
                acceptance = list(csv.DictReader(handle))
            self.assertEqual(4, len(acceptance))
            self.assertEqual(
                {("ALPHA", "STATE_A"), ("ALPHA", "STATE_B"),
                 ("BETA", "STATE_A"), ("BETA", "STATE_B")},
                {(row["strategy"], row["trend_state"]) for row in acceptance},
            )
            with (root / "out" / "full_roster_state_excluded_episodes.csv").open() as handle:
                excluded = list(csv.DictReader(handle))
            self.assertIn("touches_archive_boundary", {row["reason"] for row in excluded})
            self.assertTrue((root / "out" / "frozen_declaration.json").is_file())

    def test_sample_insufficient_cells_enter_the_family_with_p_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, modern = root / "old.zip", root / "modern.zip"
            declaration = root / "declaration.json"
            _archive(old, date(2008, 1, 1))
            _archive(modern, date(2012, 1, 1))
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["reliability_gate"]["minimum_complete_episodes"] = 100
            declaration.write_text(json.dumps(payload))
            build(declaration, old, modern, root / "out")
            with (root / "out" / "full_roster_state_acceptance.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({"1.0"}, {row["primary_family_inference_p"] for row in rows})
            self.assertEqual({"1.0"}, {row["primary_bh_q"] for row in rows})

    def test_optional_economic_and_stress_profit_gates_reject_dust(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, modern = root / "old.zip", root / "modern.zip"
            declaration = root / "declaration.json"
            _archive(old, date(2008, 1, 1))
            _archive(modern, date(2012, 1, 1))
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["reliability_gate"].update({
                "minimum_primary_median_episode_excess": 0.50,
                "minimum_stress_median_episode_excess": 0.50,
                "minimum_stress_profit_factor": 1.0,
            })
            declaration.write_text(json.dumps(payload))
            build(declaration, old, modern, root / "out")
            with (root / "out" / "full_roster_state_acceptance.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({"False"}, {row["primary_median_pass"] for row in rows})
            self.assertEqual({"False"}, {row["stress_median_pass"] for row in rows})
            self.assertIn("stress_profit_factor_pass", rows[0])

    def test_named_archives_preserve_strategy_specific_state_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha_archive, beta_archive = root / "alpha.zip", root / "beta.zip"
            declaration = root / "declaration.json"
            _archive(alpha_archive, date(2008, 1, 1))
            _archive(beta_archive, date(2012, 1, 1))
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["fixed_calendar_blocks"] = [
                {"id": "ALPHA_BLOCK", "source": "alpha_source",
                 "start": "2008-01-01", "end": "2008-12-31"},
                {"id": "BETA_BLOCK", "source": "beta_source",
                 "start": "2012-01-01", "end": "2012-12-31"},
            ]
            payload["strategy_sources"] = {
                "ALPHA": ["alpha_source"], "BETA": ["beta_source"],
            }
            payload["strategy_role_contracts"] = [
                {"strategy": "ALPHA", "active_symbols": ["QQQ"],
                 "role_overlap_required": True},
                {"strategy": "BETA", "active_symbols": ["QQQ"],
                 "role_overlap_required": True},
            ]
            payload["reliability_gate"]["minimum_active_holding_fraction"] = 0.5
            declaration.write_text(json.dumps(payload))
            summary = build(
                declaration, None, None, root / "out",
                archives={"alpha_source": alpha_archive,
                          "beta_source": beta_archive},
            )
            self.assertEqual(
                {"alpha_source", "beta_source"}, set(summary["source_sha256"])
            )
            with (root / "out" / "full_roster_state_acceptance.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            alpha = next(row for row in rows if row["strategy"] == "ALPHA")
            beta = next(row for row in rows if row["strategy"] == "BETA")
            self.assertEqual("True", alpha["active_holding_fraction_pass"])
            self.assertEqual("False", beta["active_holding_fraction_pass"])

    def test_rejects_an_incomplete_declared_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration = root / "declaration.json"
            _declaration(declaration)
            payload = json.loads(declaration.read_text())
            payload["family_size"] = 3
            declaration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "family_size"):
                build(declaration, root / "missing.zip", root / "missing2.zip", root / "out")


if __name__ == "__main__":
    unittest.main()
