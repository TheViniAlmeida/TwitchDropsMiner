from __future__ import annotations

from collections import abc
from dataclasses import dataclass
import asyncio
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop
    from utils import Game
    from yarl import URL


@dataclass
class LoginData:
    username: str
    password: str
    token: str


class StatusBar(Protocol):
    def update(self, text: str) -> None:
        ...


class TrayIcon(Protocol):
    def change_icon(self, state: str) -> None:
        ...

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> asyncio.Task[None] | None:
        ...


class LoginForm(Protocol):
    def clear(self, login: bool = False, password: bool = False, token: bool = False) -> None:
        ...

    async def ask_login(self) -> LoginData:
        ...

    async def ask_enter_code(self, page_url: URL, user_code: str) -> None:
        ...

    def update(self, status: str, user_id: int | None) -> None:
        ...


class ChannelList(Protocol):
    def display(self, channel: Channel, *, add: bool = False) -> None:
        ...

    def remove(self, channel: Channel) -> None:
        ...

    def clear(self) -> None:
        ...

    def set_watching(self, channel: Channel) -> None:
        ...

    def clear_watching(self) -> None:
        ...

    def get_selection(self) -> Channel | None:
        ...


class CampaignProgress(Protocol):
    def minute_almost_done(self) -> bool:
        ...

    def stop_timer(self) -> None:
        ...


class InventoryOverview(Protocol):
    def clear(self) -> None:
        ...

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        ...

    def update_drop(self, drop: TimedDrop) -> None:
        ...


class WebsocketStatus(Protocol):
    def update(self, idx: int, status: str | None = None, topics: int | None = None) -> None:
        ...

    def remove(self, idx: int) -> None:
        ...


_T = TypeVar("_T")


class UIManager(Protocol):
    status: StatusBar
    tray: TrayIcon
    login: LoginForm
    channels: ChannelList
    progress: CampaignProgress
    inv: InventoryOverview
    websockets: WebsocketStatus

    @property
    def close_requested(self) -> bool:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def close(self, *args: object) -> int:
        ...

    def close_window(self) -> None:
        ...

    async def wait_until_closed(self) -> None:
        ...

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        ...

    def prevent_close(self) -> None:
        ...

    def grab_attention(self, *, sound: bool = True) -> None:
        ...

    def save(self, *, force: bool = False) -> None:
        ...

    def set_games(self, games: set[Game]) -> None:
        ...

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        ...

    def clear_drop(self) -> None:
        ...

    def print(self, message: str) -> None:
        ...

    def set_logged_in(self, logged_in: bool) -> None:
        ...
