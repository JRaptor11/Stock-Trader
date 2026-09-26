import unittest
import uuid
from pathlib import Path

from research.prospective_shadow_coordinator import ProspectiveShadowCoordinator, STRATEGIES


class Store:
    durable = False


class CoordinatorTests(unittest.TestCase):
    evidence = {"trend_200d_distance": .10, "trend_63d_return": .05,
                "trend_20d_return": .02, "trend_acceleration_5d": .01,
                "volatility_20d_bucket": "Q3", "breadth_50d": .70}
    def test_two_sessions_freeze_then_execute_without_broker_orders(self):
        root = Path(".test-prospective-coordinator") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            coordinator = ProspectiveShadowCoordinator(root, Store())
            symbols = coordinator.state["histories"]
            coordinator.state["histories"] = {
                symbol: [100 + index * .1 for index in range(260)] for symbol in symbols
            }
            def payload(session, next_session, price):
                return {"session": session, "next_session": next_session,
                        "source_observed_at": session + "T21:05:00Z",
                        "code_revision": "test", "state_evidence": self.evidence,
                        "bars": {
                            symbol: {"open": price, "close": price, "volume": 1_000_000}
                            for symbol in symbols}}
            first = coordinator.process_session(payload("2026-09-25", "2026-09-28", 100))
            second = coordinator.process_session(payload("2026-09-28", "2026-09-29", 101))
            self.assertEqual("processed", first["status"])
            self.assertEqual("processed", second["status"])
            self.assertEqual(len(STRATEGIES), second["strategies"])
            self.assertFalse(coordinator.status()["broker_orders_enabled"])
            self.assertEqual({}, coordinator.status()["pending_execution_sessions"])
            self.assertEqual("unchanged", coordinator.process_session(
                payload("2026-09-28", "2026-09-29", 101))["status"])
            restored = ProspectiveShadowCoordinator(root, Store())
            self.assertEqual("2026-09-28", restored.status()["last_session"])
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()

    def test_bootstrap_is_bounded_and_only_accepted_once(self):
        root = Path(".test-prospective-coordinator") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            coordinator = ProspectiveShadowCoordinator(root, Store())
            symbols = tuple(coordinator.state["histories"])
            payload = {"session": "2026-09-25", "next_session": "2026-09-28",
                       "source_observed_at": "2026-09-25T21:05:00Z",
                       "code_revision": "test", "state_evidence": self.evidence,
                       "bootstrap_histories": {symbol: [100.0] * 260 for symbol in symbols},
                       "bars": {symbol: {"open": 100, "close": 100, "volume": 1000000}
                                for symbol in symbols}}
            coordinator.process_session(payload)
            payload["session"], payload["next_session"] = "2026-09-28", "2026-09-29"
            with self.assertRaisesRegex(ValueError, "only before the first"):
                coordinator.process_session(payload)
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()

    def test_rebalance_calendar_matches_strategy_cadence(self):
        root = Path(".test-prospective-coordinator") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            coordinator = ProspectiveShadowCoordinator(root, Store())
            symbols = tuple(coordinator.state["histories"])
            coordinator.state["histories"] = {
                symbol: [100 + index * .1 for index in range(260)] for symbol in symbols
            }
            def submit(session, next_session):
                return coordinator.process_session({
                    "session": session, "next_session": next_session,
                    "source_observed_at": session + "T21:05:00Z", "code_revision": "test",
                    "state_evidence": self.evidence,
                    "bars": {symbol: {"open": 100, "close": 100, "volume": 1_000_000}
                             for symbol in symbols}})
            submit("2026-09-25", "2026-09-28")
            submit("2026-09-28", "2026-09-29")
            self.assertEqual({}, coordinator.status()["pending_execution_sessions"])
            submit("2026-09-29", "2026-09-30")
            submit("2026-09-30", "2026-10-01")
            submit("2026-10-01", "2026-10-02")
            pending = coordinator.status()["pending_execution_sessions"]
            self.assertIn("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", pending)
            self.assertIn("VALUE_QUALITY_STATIC", pending)
            self.assertEqual(set(STRATEGIES), set(pending))
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()

    def test_incomplete_coverage_and_state_mismatch_fail_closed(self):
        root = Path(".test-prospective-coordinator") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            coordinator = ProspectiveShadowCoordinator(root, Store())
            symbols = tuple(coordinator.state["histories"])
            payload = {"session": "2026-09-25", "next_session": "2026-09-28",
                       "source_observed_at": "2026-09-25T21:05:00Z", "code_revision": "test",
                       "state_evidence": self.evidence,
                       "bars": {symbol: {"open": 100, "close": 100, "volume": 1_000_000}
                                for symbol in symbols[:-1]}}
            with self.assertRaisesRegex(ValueError, "incomplete prospective"):
                coordinator.process_session(payload)
            payload["bars"][symbols[-1]] = {"open": 100, "close": 100, "volume": 1_000_000}
            payload["raw_state"] = "BEAR_DETERIORATING__HIGH_VOL__NARROW_BREADTH"
            with self.assertRaisesRegex(ValueError, "disagrees"):
                coordinator.process_session(payload)
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()

    def test_skipped_expected_session_fails_closed(self):
        root = Path(".test-prospective-coordinator") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            coordinator = ProspectiveShadowCoordinator(root, Store())
            symbols = tuple(coordinator.state["histories"])
            def payload(session, next_session):
                return {"session": session, "next_session": next_session,
                        "source_observed_at": session + "T21:05:00Z", "code_revision": "test",
                        "state_evidence": self.evidence,
                        "bars": {symbol: {"open": 100, "close": 100, "volume": 1_000_000}
                                 for symbol in symbols}}
            coordinator.process_session(payload("2026-09-25", "2026-09-28"))
            with self.assertRaisesRegex(ValueError, "expected 2026-09-28"):
                coordinator.process_session(payload("2026-09-29", "2026-09-30"))
        finally:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()


if __name__ == "__main__": unittest.main()
