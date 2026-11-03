import asyncio
import time
import unittest

from utils.async_utils import run_sync_with_heartbeat


class SyncHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_work_renews_heartbeat_without_blocking_loop(self):
        heartbeats = []
        loop_ticks = []

        async def tick_loop():
            for _ in range(4):
                await asyncio.sleep(0.01)
                loop_ticks.append(time.monotonic())

        ticker = asyncio.create_task(tick_loop())
        result = await run_sync_with_heartbeat(
            lambda: (time.sleep(0.07), "done")[1],
            heartbeat_callback=lambda: heartbeats.append(time.monotonic()),
            heartbeat_interval_seconds=0.01,
        )
        await ticker

        self.assertEqual("done", result)
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertEqual(4, len(loop_ticks))


if __name__ == "__main__":
    unittest.main()
