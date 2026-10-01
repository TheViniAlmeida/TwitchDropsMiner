from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from progress_timer import ProgressTimer


class ProgressTimerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.updates: list[int] = []
        self.timer = ProgressTimer(self.updates.append)

    async def asyncTearDown(self) -> None:
        self.timer.stop_timer()
        await asyncio.sleep(0)

    async def test_display_cases(self) -> None:
        self.timer.display(5)
        await asyncio.sleep(0)
        self.assertEqual(self.timer.seconds, 60)

        self.timer.display(0)
        self.assertEqual(self.timer.seconds, 60)

        self.timer.display(5, countdown=False, subone=True)
        self.assertEqual(self.timer.seconds, 0)

        self.timer.display(5, countdown=False)
        self.assertEqual(self.timer.seconds, 60)

        self.timer.display(None)
        self.assertEqual(self.timer.seconds, 0)
        self.assertEqual(self.updates[-5:], [60, 60, 0, 60, 0])

    async def test_minute_almost_done_before_and_after_start(self) -> None:
        self.assertTrue(self.timer.minute_almost_done())
        self.timer.start_timer(1)
        await asyncio.sleep(0)
        self.assertFalse(self.timer.minute_almost_done())

    async def test_countdown_edges_stop_and_restart(self) -> None:
        ticks: asyncio.Queue[None] = asyncio.Queue()
        real_sleep = asyncio.sleep

        async def controlled_sleep(seconds: int) -> None:
            self.assertEqual(seconds, 1)
            await ticks.get()

        async def advance(count: int) -> None:
            for _ in range(count):
                ticks.put_nowait(None)
                await real_sleep(0)

        with patch("progress_timer.asyncio.sleep", new=controlled_sleep):
            self.timer.start_timer(1)
            await real_sleep(0)
            await advance(49)
            self.assertEqual(self.timer.seconds, 11)
            self.assertFalse(self.timer.minute_almost_done())
            await advance(1)
            self.assertEqual(self.timer.seconds, 10)
            self.assertTrue(self.timer.minute_almost_done())
            await advance(10)
            self.assertEqual(self.timer.seconds, 0)
            self.assertTrue(self.timer.minute_almost_done())

            self.timer.start_timer(1)
            await advance(2)
            self.assertEqual(self.timer.seconds, 58)
            self.timer.stop_timer()
            self.assertTrue(self.timer.minute_almost_done())
            self.timer.start_timer(1)
            await advance(1)
            self.assertEqual(self.timer.seconds, 59)
            self.assertFalse(self.timer.minute_almost_done())
