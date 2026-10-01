from __future__ import annotations

import asyncio
import getpass
import logging
import sys
from collections import OrderedDict
from collections import abc
from datetime import datetime
from typing import Any, TYPE_CHECKING, TypeVar

from constants import OUTPUT_FORMATTER
from exceptions import ExitRequest, LoginException
from progress_timer import ProgressTimer
from ui_base import LoginData
from utils import webopen

if TYPE_CHECKING:
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop
    from twitch import Twitch
    from utils import Game
    from yarl import URL


logger = logging.getLogger("TwitchDrops")
_T = TypeVar("_T")


class _CLIOutputHandler(logging.Handler):
    def __init__(self, output: CLIManager) -> None:
        super().__init__()
        self._output = output

    def emit(self, record: logging.LogRecord) -> None:
        self._output.print(self.format(record))


class _Status:
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager
        self._text: str | None = None

    def update(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self._manager.print(f"[status] {text}")


class _Tray:
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager

    def change_icon(self, state: str) -> None:
        pass

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> None:
        if self._manager._twitch.settings.tray_notifications:
            self._manager.print(f"[drop] {title or 'Twitch Drops'}: {message}")


class _Login:
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager

    def clear(self, login: bool = False, password: bool = False, token: bool = False) -> None:
        pass

    async def ask_enter_code(self, page_url: URL, user_code: str) -> None:
        self._manager.print(f"Open this activation URL: {page_url}")
        self._manager.print(f"Enter this code: {user_code}")
        if self._manager._open_browser:
            webopen(page_url)

    async def ask_login(self) -> LoginData:
        if not sys.stdin.isatty():
            raise LoginException("Interactive login requires a TTY; use device activation instead.")
        username = input("Twitch username: ").strip()
        password = getpass.getpass("Twitch password: ")
        token = getpass.getpass("Access token (optional): ").strip()
        return LoginData(username, password, token)

    def update(self, status: str, user_id: int | None) -> None:
        self._manager.print(f"[login] {status}")


class _Channels:
    def __init__(self) -> None:
        self._channels: OrderedDict[int, Channel] = OrderedDict()
        self._watching: int | None = None
        self._selection: Channel | None = None

    def display(self, channel: Channel, *, add: bool = False) -> None:
        if add or channel.id in self._channels:
            self._channels[channel.id] = channel

    def remove(self, channel: Channel) -> None:
        self._channels.pop(channel.id, None)
        if self._watching == channel.id:
            self._watching = None
        if self._selection is channel:
            self._selection = None

    def clear(self) -> None:
        self._channels.clear()
        self._watching = None
        self._selection = None

    def set_watching(self, channel: Channel) -> None:
        self._watching = channel.id

    def clear_watching(self) -> None:
        self._watching = None

    def select(self, channel: Channel) -> None:
        if channel.id in self._channels:
            self._selection = channel

    def get_selection(self) -> Channel | None:
        selection = self._selection
        self._selection = None
        return selection


class _Progress:
    def __init__(self) -> None:
        self.seconds: int = 0
        self._timer = ProgressTimer(self._update)

    def _update(self, seconds: int) -> None:
        self.seconds = seconds

    def start_timer(self, remaining_minutes: int = 0) -> None:
        self._timer.start_timer(remaining_minutes)

    def stop_timer(self) -> None:
        self._timer.stop_timer()

    def minute_almost_done(self) -> bool:
        return self._timer.minute_almost_done()

    def display(
        self, remaining_minutes: int | None, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self._timer.display(remaining_minutes, countdown=countdown, subone=subone)


class _Inventory:
    def __init__(self) -> None:
        self._campaigns: OrderedDict[str, DropsCampaign] = OrderedDict()
        self._drops: dict[str, TimedDrop] = {}

    def clear(self) -> None:
        self._campaigns.clear()
        self._drops.clear()

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        self._campaigns[campaign.id] = campaign
        for drop in campaign.drops:
            self._drops[drop.id] = drop

    def update_drop(self, drop: TimedDrop) -> None:
        self._drops[drop.id] = drop


class _Websockets:
    def __init__(self) -> None:
        self._states: dict[int, tuple[str | None, int | None]] = {}

    def update(self, idx: int, status: str | None = None, topics: int | None = None) -> None:
        if status is None and topics is None:
            raise TypeError("You need to provide at least one of: status, topics")
        old_status, old_topics = self._states.get(idx, (None, None))
        state = (
            status if status is not None else old_status,
            topics if topics is not None else old_topics,
        )
        if self._states.get(idx) != state:
            self._states[idx] = state
            logger.debug("Websocket %s: status=%s topics=%s", idx, status, topics)

    def remove(self, idx: int) -> None:
        if idx in self._states:
            del self._states[idx]
            logger.debug("Websocket %s removed", idx)


class CLIManager:
    def __init__(self, twitch: Twitch, *, open_browser: bool = False) -> None:
        self._twitch = twitch
        self._open_browser = open_browser
        self._close_requested = asyncio.Event()
        self._prevented_close = False
        self._current_drop: TimedDrop | None = None
        self._games: set[Game] = set()
        self._logged_in = False
        self.status = _Status(self)
        self.tray = _Tray(self)
        self.login = _Login(self)
        self.channels = _Channels()
        self.progress = _Progress()
        self.inv = _Inventory()
        self.websockets = _Websockets()
        self._handler = _CLIOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger.addHandler(self._handler)
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.progress.stop_timer()

    def close(self, *args: object) -> int:
        self._close_requested.set()
        self._twitch.close()
        return 0

    def close_window(self) -> None:
        logger.removeHandler(self._handler)

    async def wait_until_closed(self) -> None:
        return None

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self) -> None:
        self._prevented_close = True
        self._close_requested.clear()

    def grab_attention(self, *, sound: bool = True) -> None:
        if sound and sys.stdout.isatty():
            sys.stdout.write("\a")
            sys.stdout.flush()

    def save(self, *, force: bool = False) -> None:
        pass

    def set_games(self, games: set[Game]) -> None:
        self._games = games

    def set_logged_in(self, logged_in: bool) -> None:
        self._logged_in = logged_in

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        changed = self._current_drop is None or self._current_drop.id != drop.id
        self._current_drop = drop
        self.progress.display(drop.remaining_minutes, countdown=countdown, subone=subone)
        if changed:
            self.print(
                f"[drop] {drop.rewards_text()} — {drop.progress:.1%} — "
                f"{drop.remaining_minutes} min remaining"
            )

    def clear_drop(self) -> None:
        self._current_drop = None
        self.progress.display(None)

    def print(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in str(message).splitlines() or [""]:
            sys.stdout.write(f"{stamp}: {line}\n")
        sys.stdout.flush()
