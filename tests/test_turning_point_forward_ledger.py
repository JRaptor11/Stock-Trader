import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.turning_point_forward_ledger import append_observations


def _csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue()


class TurningPointForwardLedgerTests(unittest.TestCase):
    def _fixtures(self, root: Path):
        declaration = root / "declaration.json"
        archive = root / "archive.zip"
        ledger = root / "ledger.jsonl"
        declaration.write_text(json.dumps({
            "study_id": "FORWARD", "forward_start": "2026-10-30",
            "benchmark_strategy": "SPY_BUY_HOLD", "state_confirmation_sessions": 3,
            "target_confirmed_trend_state": "TRANSITION_DETERIORATING",
            "cost_ladder_bps": [10, 20],
            "forward_hypotheses": [{"hypothesis_id": "fast", "role": "primary",
                                    "strategy": "TURNING_POINT_FAST_EQUITY"}],
            "ledger": {"broker_orders_enabled": False},
        }), encoding="utf-8")
        daily = []
        for cost in (10, 20):
            for strategy, equities in (
                ("SPY_BUY_HOLD", (100, 101, 100)),
                ("TURNING_POINT_FAST_EQUITY", (100, 102, 103)),
            ):
                daily.extend(
                    {"date": day, "strategy": strategy, "cost_bps": cost, "equity": equity}
                    for day, equity in zip(("2026-10-29", "2026-10-30", "2026-11-02"), equities)
                )
        labels = [
            {"date": "2026-10-29", "trend_state": "OTHER", "core_state": "OTHER"},
            {"date": "2026-10-30", "trend_state": "TRANSITION_DETERIORATING", "core_state": "TARGET"},
            {"date": "2026-11-02", "trend_state": "OTHER", "core_state": "OTHER"},
        ]
        cycles = [{"date": row["date"], "turning_point_cycle": "CORRECTION"} for row in labels]
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("tier1_manifest.json", json.dumps({
                "experiment": {"hypothesis_id": "FORWARD"}, "config": {"fixed": 1},
            }))
            bundle.writestr("tier1_market_state_definition.json", json.dumps({
                "state_change_confirmation_sessions": 3,
            }))
            bundle.writestr("tier1_daily.csv", _csv(daily))
            bundle.writestr("tier1_market_state_labels.csv", _csv(labels))
            bundle.writestr("tier1_turning_point_cycle_labels.csv", _csv(cycles))
        return archive, declaration, ledger

    def test_appends_both_costs_idempotently_and_only_compounds_active_state(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            result = append_observations(archive, declaration, ledger)
            self.assertEqual(4, result["new_rows"])
            self.assertEqual(2, result["active_new_rows"])
            self.assertFalse(result["broker_orders_enabled"])
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            active = [row for row in rows if row["state_active"]]
            self.assertEqual({10.0, 20.0}, {row["cost_bps"] for row in active})
            self.assertTrue(all(row["active_strategy_compounded_return"] > 0 for row in active))
            self.assertEqual("unchanged", append_observations(
                archive, declaration, ledger)["status"])

    def test_fails_closed_on_declaration_change(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            append_observations(archive, declaration, ledger)
            payload = json.loads(declaration.read_text())
            payload["forward_start"] = "2026-11-02"
            declaration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "declaration or configuration changed"):
                append_observations(archive, declaration, ledger)


if __name__ == "__main__":
    unittest.main()
