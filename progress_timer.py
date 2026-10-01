from __future__ import annotations

import asyncio
from collections import abc


class ProgressTimer:
    ALMOST_DONE_SECONDS = 10

    def __init__(self, on_update: abc.Callable[[int], None]):
        self._on_update = on_update
        self._seconds: int = 0
        self._timer_task: asyncio.Task[None] | None = None

    @property
    def seconds(self) -> int:
        return self._seconds

    def _update(self, seconds: int) -> None:
        self._seconds = seconds
        self._on_update(seconds)

    async def _timer_loop(self) -> None:
        self._update(60)
        while self._seconds > 0:
            await asyncio.sleep(1)
            self._update(self._seconds - 1)
        self._timer_task = None

    def start_timer(self, remaining_minutes: int) -> None:
        if self._timer_task is None:
            if remaining_minutes <= 0:
                self._update(60)
            else:
                self._timer_task = asyncio.create_task(self._timer_loop())

    def stop_timer(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    def minute_almost_done(self) -> bool:
        return self._timer_task is None or self._seconds <= self.ALMOST_DONE_SECONDS

    def display(
        self, remaining_minutes: int | None, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.stop_timer()
        if remaining_minutes is None:
            self._update(0)
        elif countdown:
            self.start_timer(remaining_minutes)
        elif subone:
            self._update(0)
        else:
            self._update(60)
