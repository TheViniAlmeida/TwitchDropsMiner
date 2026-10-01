from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_failed_logout_confirmation_keeps_console_reader_alive(self) -> None:
        self.manager._logout = AsyncMock(side_effect=RuntimeError("logout failed"))
        await self.manager.dispatch_command("logout")
        with self.assertLogs("TwitchDrops", level="ERROR"):
            await self.manager.dispatch_command("y")
        self.assertEqual(self.messages[-1], "error: logout failed")
        self.assertFalse(self.twitch.closed)

        await self.manager.dispatch_command("quit")
        self.assertTrue(self.twitch.closed)
        self.assertTrue(self.manager.close_requested)

    async def test_logout_restarts_after_success_or_http_error(self) -> None:
        for status in (200, 400):
            with self.subTest(status=status):
                auth = SimpleNamespace(access_token="sample", invalidate=Mock())
                self.twitch.get_auth = AsyncMock(return_value=auth)
                self.twitch._client_type = SimpleNamespace(CLIENT_ID="test-client")
                response = SimpleNamespace(status=status)

                class RequestContext:
                    async def __aenter__(self):
                        return response

                    async def __aexit__(self, *_):
                        return None

                self.twitch.request = Mock(return_value=RequestContext())
                self.twitch.states.clear()
                self.messages.clear()
                with patch.dict("os.environ", {"TDM_ALLOW_LOGOUT": "1"}), patch("cli_actions.ensure_backup"):
                    await self.manager._logout()

                self.twitch.request.assert_called_once_with(
                    "POST", "https://id.twitch.tv/oauth2/revoke",
                    data={"client_id": "test-client", "token": "sample"},
                )
                if status == 200:
                    auth.invalidate.assert_called_once_with(delete_cookies=True)
                    self.assertEqual(self.messages, ["logged out"])
                else:
                    auth.invalidate.assert_not_called()
                    self.assertEqual(self.messages, ["error: logout failed (HTTP 400)"])
                self.assertEqual(self.twitch.states, [State.RESTART])
