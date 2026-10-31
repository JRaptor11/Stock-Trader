import asyncio
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from layers.layer_schedule import sleep_until_next_layer_boundary


class LayerScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_boundary_wait_renews_heartbeat(self):
        shutdown = threading.Event()
        heartbeats = []
        next_run = datetime.now(timezone.utc) + timedelta(seconds=0.06)
        with patch("layers.layer_schedule.next_wall_clock_run_time", return_value=next_run):
            await sleep_until_next_layer_boundary(
                shutdown_event=shutdown,
                interval_seconds=1,
                min_spacing_seconds=0,
                heartbeat_callback=lambda scheduled: heartbeats.append(scheduled),
                heartbeat_interval_seconds=0.01,
            )
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertTrue(all(value == next_run for value in heartbeats))


if __name__ == "__main__":
    unittest.main()
