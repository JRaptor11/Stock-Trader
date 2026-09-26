import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.state import app_state
from core.app_state_init import initialize_layer_state
from core.layer_monitor_supervisor import run_layer_monitor_supervisor


class LayerInitializationTests(unittest.TestCase):
    def test_initialize_replaces_none_paper_portfolio_placeholder(self):
        original_layers = app_state.get("layers")
        original_buffer = app_state.get("market_data", {}).get("buffer")
        try:
            app_state.setdefault("market_data", {})["buffer"] = Mock()
            app_state["layers"] = {"paper_portfolio": None, "engine": None}
            with patch("core.app_state_init.Layer2PortfolioEngine", return_value=Mock()):
                initialize_layer_state()
            self.assertIsNotNone(app_state["layers"]["paper_portfolio"])
            self.assertEqual("initialized", app_state["layers"]["monitor"]["status"])
        finally:
            app_state["layers"] = original_layers
            app_state.setdefault("market_data", {})["buffer"] = original_buffer


class LayerMonitorSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_main = app_state.get("main")
        self.original_layers = app_state.get("layers")
        self.original_stream = app_state.get("stream")
        self.original_execution = app_state.get("execution")
        app_state["main"] = {"layer_monitor_task": None}
        app_state["layers"] = {"monitor": {}}
        app_state["stream"] = {"shutdown_event": threading.Event()}
        app_state["execution"] = {"layer_monitor_run_24_7": True}

    async def asyncTearDown(self):
        app_state["main"] = self.original_main
        app_state["layers"] = self.original_layers
        app_state["stream"] = self.original_stream
        app_state["execution"] = self.original_execution

    async def test_supervisor_restarts_failed_child_and_records_exact_reason(self):
        calls = 0

        async def fake_monitor(*, interval_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic monitor failure")
            app_state["stream"]["shutdown_event"].set()

        alert = AsyncMock()
        await asyncio.wait_for(
            run_layer_monitor_supervisor(
                fake_monitor,
                alert,
                interval_seconds=1,
                check_seconds=0.01,
                stall_seconds=0.1,
                restart_delay_base_seconds=0.01,
            ),
            timeout=1,
        )

        monitor = app_state["layers"]["monitor"]
        self.assertEqual(2, calls)
        self.assertEqual(1, monitor["restart_count"])
        self.assertIn("RuntimeError", monitor["last_restart_reason"])
        self.assertIn("synthetic monitor failure", monitor["last_restart_reason"])
        alert.assert_awaited_once()

    async def test_supervisor_cancels_and_restarts_child_without_heartbeat(self):
        calls = 0

        async def fake_monitor(*, interval_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            app_state["stream"]["shutdown_event"].set()

        alert = AsyncMock()
        await asyncio.wait_for(
            run_layer_monitor_supervisor(
                fake_monitor,
                alert,
                interval_seconds=1,
                check_seconds=0.01,
                stall_seconds=0.02,
                restart_delay_base_seconds=0.01,
            ),
            timeout=1,
        )

        monitor = app_state["layers"]["monitor"]
        self.assertEqual(2, calls)
        self.assertEqual("missing_heartbeat", monitor["last_restart_reason"])
        alert.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
