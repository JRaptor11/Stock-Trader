import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.strategy_condition_validation import validate_bundle


def _csv(rows):
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows); return output.getvalue()


class StrategyConditionValidationTests(unittest.TestCase):
    def test_training_qualification_never_uses_test_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.zip"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "volatility_20d_bucket": "Q5_HIGH"} for day in dates]
            daily = []
            for strategy, values in (("SPY_BUY_HOLD", [0] * 12), ("TEST", [.02] * 6 + [-.50] * 6)):
                daily.extend({"source": "test", "hypothesis_id": "TEST", "trial_id": "one", "strategy": strategy, "date": day, "cost_bps": 10, "daily_return": value} for day, value in zip(dates, values))
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", json.dumps({}))
            first, _, _ = validate_bundle(path, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1)
            qualification = first[0]["qualified_on_train"]
            daily[-1]["daily_return"] = .99
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", json.dumps({}))
            second, _, _ = validate_bundle(path, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1)
            self.assertEqual(qualification, second[0]["qualified_on_train"])

    def test_requires_enough_chronological_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", "source,strategy,date,daily_return\nx,SPY_BUY_HOLD,2026-01-01,0\n")
                bundle.writestr("causal_market_conditions.csv", "date,volatility_20d_bucket\n2026-01-01,Q1_LOW\n")
                bundle.writestr("strategy_evidence_manifest.json", "{}")
            with self.assertRaisesRegex(ValueError, "insufficient"):
                validate_bundle(path, train_sessions=2, test_sessions=1)

    def test_focused_declaration_limits_trials_and_labels_reused_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); evidence = root / "evidence.zip"; declaration = root / "declaration.json"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "volatility_20d_bucket": "Q1_LOW"} for day in dates]
            daily = [{"source": "x", "strategy": strategy, "date": day, "daily_return": value}
                     for strategy, value in (("SPY_BUY_HOLD", 0), ("TEST", .01)) for day in dates]
            with zipfile.ZipFile(evidence, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", "{}")
            declaration.write_text(json.dumps({"frozen_at": "2026-12-31T00:00:00Z", "evidence_observed_through": "2026-12-31", "hypotheses": [{"id": "one", "cohort": "test", "strategy": "TEST", "dimension": "volatility_20d_bucket", "bucket": "Q1_LOW"}]}))
            rows, _summary, metadata = validate_bundle(evidence, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1, declaration_path=declaration, cohort="test")
            self.assertEqual(1, metadata["family_trials"])
            self.assertTrue(all(row["hypothesis_id"] == "one" for row in rows))
            self.assertTrue(all(row["evidence_class"] == "retrospective_reuse" for row in rows))

    def test_focused_declaration_rejects_unknown_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); declaration = root / "declaration.json"
            declaration.write_text(json.dumps({"frozen_at": "2026-01-01", "evidence_observed_through": "2025-01-01", "hypotheses": [{"id": "bad", "cohort": "test", "strategy": "MISSING", "dimension": "volatility_20d_bucket", "bucket": "Q1_LOW"}]}))
            with self.assertRaisesRegex(ValueError, "no hypotheses for cohort other"):
                from research.strategy_condition_validation import _load_declaration
                _load_declaration(declaration, "other")


if __name__ == "__main__":
    unittest.main()
