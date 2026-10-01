from __future__ import annotations

import asyncio
import unittest

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
