from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
import shlex
import sys
import threading
from contextvars import ContextVar
from collections import OrderedDict, deque
from collections import abc
from contextlib import suppress
from dataclasses import fields, replace
from datetime import datetime
from time import monotonic
from typing import Any, TYPE_CHECKING, Callable, TypeVar
from urllib.parse import unquote

from cli_actions import Actions, ActionError, ActionRejected, CampaignFilters
from cli_commands import CommandError
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
_COUNTER_STATUS = re.compile(r"\((\d+)/(\d+)\)\s*$")
_DEVICE_CODE_QUERY = re.compile(r"([?&])([^=&#\s]+)=([^&#\s]*)")
_command_writer: ContextVar[Callable[[str], None] | None] = ContextVar("command_writer", default=None)


def _redact_device_code_query(match: re.Match[str]) -> str:
    if unquote(match.group(2)).casefold() == "device-code":
        return f"{match.group(1)}{match.group(2)}=<redacted>"
    return match.group(0)


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
        self._last_counter_print: float | None = None

    def update(self, text: str) -> None:
        counter = _COUNTER_STATUS.search(text)
        changed = text != self._text
        self._text = text
        if changed and (notify := getattr(self._manager, "_notify_change", None)) is not None:
            notify()
        if counter is not None:
            current, total = (int(value) for value in counter.groups())
            now = monotonic()
            if (
                current == total
                or self._last_counter_print is None
                or now - self._last_counter_print >= 5
            ):
                self._last_counter_print = now
                self._manager.print(f"[status] {text}")
        elif changed:
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
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager
        self._channels: OrderedDict[int, Channel] = OrderedDict()
        self._watching: int | None = None
        self._selection: Channel | None = None

    def display(self, channel: Channel, *, add: bool = False) -> None:
        if add or channel.id in self._channels:
            self._channels[channel.id] = channel
            self._manager._notify_change()

    def remove(self, channel: Channel) -> None:
        removed = self._channels.pop(channel.id, None)
        changed = removed is not None or self._watching == channel.id
        if self._watching == channel.id:
            self._watching = None
        if self._selection is channel:
            self._selection = None
        if changed:
            self._manager._notify_change()

    def clear(self) -> None:
        changed = bool(self._channels or self._watching is not None or self._selection is not None)
        self._channels.clear()
        self._watching = None
        self._selection = None
        if changed:
            self._manager._notify_change()

    def set_watching(self, channel: Channel) -> None:
        changed = self._watching != channel.id
        self._watching = channel.id
        if changed:
            self._manager._notify_change()

    def clear_watching(self) -> None:
        changed = self._watching is not None
        self._watching = None
        if changed:
            self._manager._notify_change()

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
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager
        self._campaigns: OrderedDict[str, DropsCampaign] = OrderedDict()
        self._drops: dict[str, TimedDrop] = {}

    def clear(self) -> None:
        changed = bool(self._campaigns or self._drops)
        self._campaigns.clear()
        self._drops.clear()
        if changed:
            self._manager._notify_change()

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        self._campaigns[campaign.id] = campaign
        for drop in campaign.drops:
            self._drops[drop.id] = drop
        self._manager._notify_change()

    def update_drop(self, drop: TimedDrop) -> None:
        self._drops[drop.id] = drop
        self._manager._notify_change()


class _Websockets:
    def __init__(self, manager: CLIManager) -> None:
        self._manager = manager
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
            self._manager._notify_change()

    def remove(self, idx: int) -> None:
        if idx in self._states:
            del self._states[idx]
            logger.debug("Websocket %s removed", idx)
            self._manager._notify_change()


class CLIManager:
    def __init__(self, twitch: Twitch, *, open_browser: bool = False) -> None:
        self._twitch = twitch
        self._open_browser = open_browser
        self._close_requested = asyncio.Event()
        self._prevented_close = False
        self._current_drop: TimedDrop | None = None
        self._last_drop_remaining: int | None = None
        self._games: set[Game] = set()
        self._logged_in = False
        self._logs: deque[str] = deque(maxlen=1000)
        self._listeners: set[Callable[[str, Any], None]] = set()
        self._failed_listeners: set[Callable[[str, Any], None]] = set()
        self.status = _Status(self)
        self.tray = _Tray(self)
        self.login = _Login(self)
        self.channels = _Channels(self)
        self.progress = _Progress()
        self.inv = _Inventory(self)
        self.websockets = _Websockets(self)
        self.actions = Actions(twitch, self)
        self._handler = _CLIOutputHandler(self)
        self._console_task: asyncio.Task[None] | None = None
        self._console_transport: Any = None
        self._console_stop = threading.Event()
        self._console_queue: asyncio.Queue[str | None] | None = None
        self._stdin_fd: int | None = None
        self._stdin_blocking: bool | None = None
        self._logout_confirmation = False
        self._campaign_filters = CampaignFilters()
        self._watch_task: asyncio.Task[None] | None = None
        self._remote_watches: dict[Callable[[str], None], asyncio.Task[None]] = {}
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger.addHandler(self._handler)
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    def start(self) -> None:
        if sys.stdin.isatty() and self._console_task is None:
            self._console_stop.clear()
            self._console_task = asyncio.create_task(self._console_loop())

    def stop(self) -> None:
        self.progress.stop_timer()
        self._stop_console()

    def close(self, *args: object) -> int:
        self._close_requested.set()
        self._twitch.close()
        return 0

    def close_window(self) -> None:
        self._stop_console()
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
        changed = self._games != games
        self._games = games
        if changed:
            self._notify_change()

    def set_logged_in(self, logged_in: bool) -> None:
        changed = self._logged_in != logged_in
        self._logged_in = logged_in
        if changed:
            self._notify_change()

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        changed = self._current_drop is None or self._current_drop.id != drop.id
        self._current_drop = drop
        self.progress.display(drop.remaining_minutes, countdown=countdown, subone=subone)
        self._notify_change()
        remaining = drop.remaining_minutes
        if changed or (remaining % 10 == 0 and remaining != self._last_drop_remaining):
            self.print(
                f"[drop] {drop.campaign.game.name} | {drop.rewards_text()} — "
                f"{drop.progress:.1%} — {remaining} min remaining"
            )
            self._last_drop_remaining = remaining

    def clear_drop(self) -> None:
        changed = self._current_drop is not None
        self._current_drop = None
        self._last_drop_remaining = None
        self.progress.display(None)
        if changed:
            self._notify_change()

    def subscribe(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.discard(listener)
        self._failed_listeners.discard(listener)

    def log_tail(self, count: int = 100) -> list[str]:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ActionError("tail must be a non-negative integer")
        return list(self._logs)[-count:] if count else []

    def _notify_change(self) -> None:
        self._notify("change", None)

    def _notify(self, event: str, data: Any) -> None:
        if not self._listeners:
            return
        for listener in tuple(self._listeners):
            if listener in self._failed_listeners:
                continue
            try:
                listener(event, data)
            except Exception:
                self._failed_listeners.add(listener)
                logger.error("CLI listener failed; further callbacks suppressed")

    def print(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        writer = _command_writer.get()
        for line in str(message).splitlines() or [""]:
            output = f"{stamp}: {line}"
            if writer is not None:
                # remote output is a command reply: no timestamp
                writer(
                    "Enter this code: <redacted>"
                    if line.startswith("Enter this code: ")
                    else _DEVICE_CODE_QUERY.sub(_redact_device_code_query, line)
                )
                continue
            sys.stdout.write(f"{output}\n")
            recorded = (
                f"{stamp}: Enter this code: <redacted>"
                if line.startswith("Enter this code: ")
                else _DEVICE_CODE_QUERY.sub(_redact_device_code_query, output)
            )
            self._logs.append(recorded)
            self._notify("log", recorded)
        sys.stdout.flush()

    def _stop_console(self) -> None:
        self._console_stop.set()
        self._cancel_watch()
        for task in self._remote_watches.values():
            task.cancel()
        self._remote_watches.clear()
        self._restore_stdin_blocking()
        if self._console_transport is not None:
            self._console_transport.close()
            self._console_transport = None
        if self._console_task is not None and not self._console_task.done():
            self._console_task.cancel()
        self._console_task = None

    def _restore_stdin_blocking(self) -> None:
        fd, blocking = self._stdin_fd, self._stdin_blocking
        self._stdin_fd = None
        self._stdin_blocking = None
        if fd is not None and blocking is not None:
            with suppress(OSError, ValueError):
                os.set_blocking(fd, blocking)

    async def _console_loop(self) -> None:
        try:
            if sys.platform == "win32":
                await self._windows_console_loop()
            else:
                await self._posix_console_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.print(f"console error: {exc}")
        finally:
            self._cancel_watch()

    async def _posix_console_loop(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport = None
        try:
            fd = sys.stdin.fileno()
            self._stdin_fd = fd
            self._stdin_blocking = os.get_blocking(fd)
            transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            self._console_transport = transport
            while not self._console_stop.is_set():
                line = await reader.readline()
                if not line:
                    self.print("console stopped (stdin EOF)")
                    return
                await self.dispatch_command(line.decode(errors="replace").strip())
        finally:
            self._restore_stdin_blocking()
            if transport is not None:
                transport.close()
            if self._console_transport is transport:
                self._console_transport = None

    async def _windows_console_loop(self) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._console_queue = queue

        def read_stdin() -> None:
            while not self._console_stop.is_set():
                line = sys.stdin.readline()
                loop.call_soon_threadsafe(queue.put_nowait, line or None)
                if not line:
                    return

        threading.Thread(target=read_stdin, name="tdm-cli-console", daemon=True).start()
        while not self._console_stop.is_set():
            line = await queue.get()
            if line is None:
                self.print("console stopped (stdin EOF)")
                return
            await self.dispatch_command(line.strip())

    def _reload_inventory(self) -> None:
        self.actions.reload()

    async def dispatch_command(
        self, line: str, *, writer: Callable[[str], None] | None = None,
        confirm: bool = False,
    ) -> bool:
        context = _command_writer.set(writer)
        try:
            return await self._dispatch_command(line, confirm=confirm, remote=writer is not None)
        finally:
            _command_writer.reset(context)

    async def _dispatch_command(self, line: str, *, confirm: bool, remote: bool) -> bool:
        self._cancel_watch()
        if not line:
            return True
        if self._logout_confirmation and not remote:
            self._logout_confirmation = False
            if line.casefold() == "y":
                try:
                    if not await self._logout():
                        return False
                except CommandError as exc:
                    self.print(f"error: {exc}")
                except Exception as exc:
                    self.print(f"error: {exc}")
                    logger.exception("Logout command failed")
            else:
                self.print("logout cancelled")
            return True
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self.print(f"error: {exc}")
            return False
        if not parts:
            return True
        try:
            command = parts[0].casefold()
            if command == "help":
                self.print(
                    "commands: help, status, channels, switch <channel>, inventory [all], games, "
                    "reload, priority, exclude, get [key], set <key> <value>, logout, quit; "
                    "campaigns [--all] [--filter k=v ...] [game], drops <campaign|game>, "
                    "game <name>, games --names, progress, watch [seconds], "
                    "filters [k=on|off ...], settings"
                )
            elif command == "status":
                self._command_status()
            elif command == "channels":
                self._command_channels()
            elif command == "switch":
                self._command_switch(parts[1:])
            elif command == "inventory":
                self._command_inventory(parts[1:])
            elif command == "games":
                self._command_games(parts[1:])
            elif command == "campaigns":
                self._command_campaigns(parts[1:])
            elif command == "drops":
                self._command_drops(parts[1:])
            elif command == "game":
                self._command_game(parts[1:])
            elif command == "progress":
                self._require_no_extra(parts[1:])
                self._command_progress()
            elif command == "watch":
                self._command_watch(parts[1:])
            elif command == "filters":
                self._command_filters(parts[1:])
            elif command == "settings":
                self._require_no_extra(parts[1:])
                self._command_settings()
            elif command == "reload":
                self._require_no_extra(parts[1:])
                self._reload_inventory()
            elif command == "priority":
                self._command_priority(parts[1:])
            elif command == "exclude":
                self._command_exclude(parts[1:])
            elif command == "get":
                self._command_get(parts[1:])
            elif command == "set":
                self._command_set(parts[1:])
            elif command == "logout":
                self._require_no_extra(parts[1:])
                if remote:
                    if not confirm:
                        raise CommandError("logout requires confirmation (-y)")
                    if not await self._logout():
                        return False
                else:
                    self._logout_confirmation = True
                    self.print("Are you sure? [y/N]")
            elif command in ("quit", "exit"):
                self._require_no_extra(parts[1:])
                if remote:
                    raise CommandError("quit is only available in the local console")
                self.close()
            else:
                raise CommandError(f"unknown command: {parts[0]}")
        except CommandError as exc:
            self.print(f"error: {exc}")
            return False
        except Exception as exc:
            self.print("error: command failed")
            logger.exception("Console command failed")
            return False
        return True

    def _require_no_extra(self, values: list[str]) -> None:
        if values:
            raise CommandError("unexpected argument")

    def _command_status(self) -> None:
        state = self.actions.state()
        self.print(f"state: {state['state']}")
        self.print(f"watched channel: {state['watched_channel'] or '-'}")
        drop = state["current_drop"]
        if drop is None:
            self.print("current drop: -")
        else:
            self.print(
                f"current drop: {drop['game']} | {drop['reward']} | "
                f"{drop['progress']:.1%} | {drop['remaining_minutes']} min, "
                f"{drop['timer_seconds']}s"
            )
        self.print(f"websockets: {state['websockets']['connected']}/{state['websockets']['total']} connected")
        self.print(f"logged in: {'yes' if state['logged_in'] else 'no'}")

    def _command_channels(self) -> None:
        self.print("* name | state | game | viewers | drops-enabled | ACL-based")
        for channel in self.actions.channels():
            marker = "*" if channel["watching"] else " "
            game = channel["game"] or "-"
            viewers = str(channel["viewers"]) if channel["viewers"] is not None else "-"
            self.print(
                f"{marker} {channel['name']} | {'online' if channel['online'] else 'offline'} | {game} | "
                f"{viewers} | {'yes' if channel['drops_enabled'] else 'no'} | "
                f"{'yes' if channel['acl_based'] else 'no'}"
            )

    def _command_switch(self, values: list[str]) -> None:
        if len(values) != 1:
            raise CommandError("usage: switch <channel>")
        self.actions.switch(values[0])

    def _command_inventory(self, values: list[str]) -> None:
        if values not in ([], ["all"]):
            raise CommandError("usage: inventory [all]")
        self.print("game | name | progress | claimed/total | ends at")
        for campaign in self.actions.inventory(all=bool(values)):
            ends_at = datetime.fromisoformat(campaign["ends_at"]).astimezone().replace(
                microsecond=0
            ).isoformat(sep=" ")
            self.print(
                f"{campaign['game']} | {campaign['name']} | {campaign['progress']:.1%} | "
                f"{campaign['claimed_drops']}/{campaign['total_drops']} | {ends_at}"
            )

    def _command_games(self, values: list[str] | None = None) -> None:
        if values == ["--names"]:
            for game in self.actions.game_names():
                self.print(game)
            return
        self._require_no_extra(values or [])
        self.print("game | status | priority | excluded | campaigns active/upcoming | drops claimed/total | online")
        for game in self.actions.games():
            self.print(
                f"{game['name']} | {game['status']} | {game['priority_pos'] or '-'} | "
                f"{'yes' if game['excluded'] else 'no'} | "
                f"{game['active_campaigns']}/{game['upcoming_campaigns']} | "
                f"{game['claimed_drops']}/{game['total_drops']} | "
                f"{len(game['online_channels'])}"
            )

    @staticmethod
    def _filter_changes(values: list[str]) -> dict[str, bool]:
        allowed = {field.name for field in fields(CampaignFilters)}
        changes = {}
        for entry in values:
            key, separator, value = entry.partition("=")
            if not separator or key not in allowed or value.casefold() not in ("on", "off"):
                raise CommandError(f"invalid filter: {entry} (use k=on|off)")
            changes[key] = value.casefold() == "on"
        return changes

    def _command_campaigns(self, values: list[str]) -> None:
        include_all = False
        changes: list[str] = []
        game = None
        remaining = iter(values)
        for value in remaining:
            if value == "--all":
                include_all = True
            elif value == "--filter":
                try:
                    changes.append(next(remaining))
                except StopIteration as exc:
                    raise CommandError("usage: campaigns [--all] [--filter k=v ...] [game]") from exc
            elif value.startswith("--") or game is not None:
                raise CommandError("usage: campaigns [--all] [--filter k=v ...] [game]")
            else:
                game = value
        selected = replace(self._campaign_filters, **self._filter_changes(changes))
        self.print("game | campaign | status | linked | progress | claimed/total | ends at | link")
        for campaign in self.actions.campaigns(selected, game=game, include_all=include_all):
            self.print(
                f"{campaign['game']} | {campaign['name']} | {campaign['status']} | "
                f"{'yes' if campaign['linked'] else 'no'} | {campaign['progress']:.1%} | "
                f"{campaign['claimed_drops']}/{campaign['total_drops']} | "
                f"{campaign['ends_at']} | {campaign['link_url'] or '-'}"
            )

    def _command_drops(self, values: list[str]) -> None:
        if not values:
            raise CommandError("usage: drops <campaign|game>")
        drops = self.actions.drops(" ".join(values))
        self.print("game | campaign | drop | status | minutes current/required/remaining | progress")
        for drop in drops:
            self.print(
                f"{drop['game']} | {drop['campaign']} | {drop['name']} | {drop['status']} | "
                f"{drop['current_minutes']}/{drop['required_minutes']}/"
                f"{drop['remaining_minutes']} | {drop['progress']:.1%}"
            )

    def _command_game(self, values: list[str]) -> None:
        if not values:
            raise CommandError("usage: game <name>")
        game = self.actions.game(" ".join(values))
        self.print(f"game: {game['name']} | {game['status']}")
        self.print(f"priority: {game['priority_pos'] or '-'} | excluded: {'yes' if game['excluded'] else 'no'}")
        self.print(f"campaigns: {', '.join(campaign['name'] for campaign in game['campaigns']) or '-'}")
        self.print(f"online channels: {', '.join(channel['name'] for channel in game['online_channels']) or '-'}")

    def _command_progress(self) -> None:
        drop = self.actions.progress()
        if drop is None:
            self.print("current drop: -")
            return
        progress = max(0.0, min(1.0, drop["progress"]))
        filled = round(progress * 20)
        self.print(
            f"{drop['game']} | {drop['campaign']} | {drop['name']} | "
            f"[{'#' * filled}{'-' * (20 - filled)}] {progress:.1%} | "
            f"{drop['current_minutes']}/{drop['required_minutes']} min "
            f"({drop['remaining_minutes']} min remaining, {drop['timer_seconds']}s)"
        )

    def _cancel_watch(self) -> None:
        writer = _command_writer.get()
        if writer is not None:
            task = self._remote_watches.pop(writer, None)
            if task is not None:
                task.cancel()
            return
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None

    def _command_watch(self, values: list[str]) -> None:
        if len(values) > 1:
            raise CommandError("usage: watch [seconds]")
        try:
            seconds = int(values[0]) if values else 5
        except ValueError as exc:
            raise CommandError("watch seconds must be a positive integer") from exc
        if seconds < 1:
            raise CommandError("watch seconds must be a positive integer")
        self._command_progress()

        async def repeat() -> None:
            while True:
                await asyncio.sleep(seconds)
                self._command_progress()

        task = asyncio.create_task(repeat())
        writer = _command_writer.get()
        if writer is None:
            self._watch_task = task
        else:
            self._remote_watches[writer] = task

    def stop_remote_watch(self, writer: Callable[[str], None]) -> None:
        task = self._remote_watches.pop(writer, None)
        if task is not None:
            task.cancel()

    def _command_filters(self, values: list[str]) -> None:
        self._campaign_filters = replace(self._campaign_filters, **self._filter_changes(values))
        for field in fields(CampaignFilters):
            self.print(f"{field.name} = {'on' if getattr(self._campaign_filters, field.name) else 'off'}")

    def _command_settings(self) -> None:
        self.print("key | type | value | choices")
        for key, setting in self.actions.settings_schema().items():
            choices = ", ".join(map(str, setting.get("choices", []))) or "-"
            self.print(f"{key} | {setting['type']} | {setting['value']} | {choices}")

    def _command_priority(self, values: list[str]) -> None:
        if not values:
            raise CommandError("usage: priority list|add|remove|move")
        action = values[0].casefold()
        if action == "list" and len(values) == 1:
            for index, game in enumerate(self.actions.priority("list")["priority"], 1):
                self.print(f"{index}. {game}")
            return
        if action in ("add", "remove") and len(values) >= 2:
            game = " ".join(values[1:])
            self.actions.priority(action, game)
            self.print(f"priority: {action} {game}")
        elif action == "move" and len(values) >= 3:
            game = " ".join(values[1:-1])
            self.actions.priority(action, game, values[-1])
            self.print(f"priority: move {game} to {values[-1]}")
        else:
            raise CommandError("usage: priority list|add <game>|remove <game>|move <game> <pos>")

    def _command_exclude(self, values: list[str]) -> None:
        if not values:
            raise CommandError("usage: exclude list|add|remove")
        action = values[0].casefold()
        if action == "list" and len(values) == 1:
            for game in self.actions.exclude("list")["exclude"]:
                self.print(game)
            return
        if action in ("add", "remove") and len(values) >= 2:
            game = " ".join(values[1:])
            self.actions.exclude(action, game)
            self.print(f"exclude: {action} {game}")
        else:
            raise CommandError("usage: exclude list|add <game>|remove <game>")

    def _command_get(self, values: list[str]) -> None:
        if len(values) > 1:
            raise CommandError("usage: get [key]")
        if values:
            self.print(f"{values[0]} = {self.actions.get_setting(values[0])}")
            return
        for key, value in self.actions.settings(include_gui_only=True).items():
            self.print(f"{key} = {value}")

    def _command_set(self, values: list[str]) -> None:
        if len(values) < 2:
            raise CommandError("usage: set <key> <value>")
        warning = self.actions.set_setting(values[0], " ".join(values[1:]))["warning"]
        if warning:
            self.print(warning)

    async def _logout(self) -> bool:
        try:
            await self.actions.logout()
        except ActionRejected as exc:
            self.print(f"error: {exc}")
            return False
        else:
            self.print("logged out")
            return True
