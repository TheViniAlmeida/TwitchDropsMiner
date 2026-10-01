from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
import unittest

from cli import CLIManager
from constants import State


class FakeTwitch:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            priority=[],
            exclude=set(),
            tray_notifications=True,
            alter=lambda: None,
        )
        self.channels = OrderedDict()
        self.inventory = SimpleNamespace()
        self.websocket = SimpleNamespace(websockets=[])
        self.states: list[State] = []
        self.closed = False

    def state_change(self, state: State):
        def record() -> None:
            self.states.append(state)

        return record

    def change_state(self, state: State) -> None:
        self.states.append(state)

    def close(self) -> None:
        self.closed = True


class ConsoleDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.twitch = FakeTwitch()
        self.channel = SimpleNamespace(
            id=1,
            name="Streamer",
            game=None,
            viewers=None,
            online=True,
            drops_enabled=True,
            acl_based=False,
        )
        self.twitch.channels[self.channel.id] = self.channel
        self.manager = CLIManager(self.twitch)
        self.messages: list[str] = []
        self.manager.print = self.messages.append

    async def asyncTearDown(self) -> None:
        self.manager.close_window()
        await asyncio.sleep(0)

    async def test_switch_reload_priority_and_quit_use_gui_states(self) -> None:
        # gui.py:840 wires Switch to CHANNEL_SWITCH; gui.py:1817 wires Reload to INVENTORY_FETCH.
        self.manager.channels.display(self.channel, add=True)
        await self.manager.dispatch_command("switch streamer")
        self.assertIs(self.manager.channels.get_selection(), self.channel)
        self.assertEqual(self.twitch.states, [State.CHANNEL_SWITCH])

        await self.manager.dispatch_command("reload")
        await self.manager.dispatch_command('priority add "Game One"')
        self.assertEqual(
            self.twitch.states,
            [State.CHANNEL_SWITCH, State.INVENTORY_FETCH, State.INVENTORY_FETCH],
        )
        self.assertEqual(self.twitch.settings.priority, ["Game One"])

        await self.manager.dispatch_command("quit")
        self.assertTrue(self.twitch.closed)
        self.assertTrue(self.manager.close_requested)

    async def test_unknown_command_and_bad_usage_print_errors(self) -> None:
        await self.manager.dispatch_command("unknown")
        await self.manager.dispatch_command("switch")
        await self.manager.dispatch_command("reload extra")
        self.assertEqual(len(self.messages), 3)
        self.assertTrue(all(message.startswith("error:") for message in self.messages))
