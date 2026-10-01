from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from yarl import URL

from cli import CLIManager
from cli_actions import ActionError, ActionRejected
from constants import PriorityMode, State
from utils import Game


class FakeTwitch:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            proxy=URL("http://alice:secret@localhost:8080"),
            language="English",
            connection_quality=3,
            priority_mode=PriorityMode.PRIORITY_ONLY,
            enable_badges_emotes=False,
            available_drops_check=True,
            tray_notifications=True,
            priority=[],
            exclude=set(),
            alter=Mock(),
        )
        self.channels = {}
        self._state = State.IDLE
        self.states = []
        self.websocket = SimpleNamespace(websockets=[])

    def state_change(self, state):
        def record():
            self.states.append(state)
        return record

    def change_state(self, state):
        self.states.append(state)


class ActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.twitch = FakeTwitch()
        self.output = io.StringIO()
        with patch("sys.stdout", self.output):
            self.manager = CLIManager(self.twitch)
        self.actions = self.manager.actions

    async def asyncTearDown(self) -> None:
        self.manager.close_window()
        await asyncio.sleep(0)

    async def test_snapshots_are_json_and_do_not_include_credentials(self) -> None:
        channel = SimpleNamespace(
            id=25, name="Streamer", game=SimpleNamespace(name="Game"), viewers=120,
            online=True, drops_enabled=True, acl_based=False,
        )
        self.twitch.channels[channel.id] = channel
        self.manager.channels.display(channel, add=True)
        self.manager.channels.set_watching(channel)
        self.manager.set_logged_in(True)
        self.manager.set_games({Game({"id": 1, "name": "Zed"}), Game({"id": 2, "name": "Alpha"})})
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        drop = SimpleNamespace(
            id="drop1", name="Reward", progress=0.5, current_minutes=5,
            required_minutes=10, remaining_minutes=5, is_claimed=False,
            starts_at=now, ends_at=now, benefits=[], rewards_text=lambda: "Reward",
        )
        campaign = SimpleNamespace(
            id="campaign1", name="Campaign", game=SimpleNamespace(name="Game"),
            progress=0.5, claimed_drops=0, total_drops=1, starts_at=now, ends_at=now,
            image_url=URL("https://static-cdn.jtvnw.net/game.jpg?secret=1"), finished=False,
            expired=False, drops=[drop],
        )
        drop.campaign = campaign
        await self.manager.inv.add_campaign(campaign)
        self.manager._current_drop = drop
        snapshots = [self.actions.state(), self.actions.channels(), self.actions.inventory(),
                     self.actions.games(), self.actions.settings()]
        serialized = json.dumps(snapshots)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("user_id", serialized)
        self.assertIn("***", serialized)
        self.assertEqual(snapshots[0]["current_drop"]["remaining_minutes"], 5)
        self.assertEqual(snapshots[1][0]["watching"], True)
        self.assertEqual(snapshots[2][0]["drops"][0]["progress"], 0.5)
        self.assertEqual(snapshots[2][0]["image_url"], "https://static-cdn.jtvnw.net/game.jpg")
        self.assertEqual(snapshots[3], ["Alpha", "Zed"])

        campaign.finished = True
        self.assertEqual(self.actions.inventory(), [])
        self.assertEqual(len(self.actions.inventory(all=True)), 1)

    async def test_switch_and_reload_share_console_state_paths(self) -> None:
        channel = SimpleNamespace(id=1, name="Streamer")
        self.twitch.channels[1] = channel
        self.manager.channels.display(channel, add=True)
        self.assertEqual(self.actions.switch("streamer"), {"channel": "Streamer"})
        self.assertIs(self.manager.channels.get_selection(), channel)
        self.assertEqual(self.actions.reload(), {"requested": True})
        self.assertEqual(self.twitch.states, [State.CHANNEL_SWITCH, State.INVENTORY_FETCH])
        with self.assertRaises(ActionError):
            self.actions.switch("missing")

    async def test_priority_and_exclude_mutations_reload_only_on_change(self) -> None:
        self.assertTrue(self.actions.priority("add", "Game One")["changed"])
        self.assertFalse(self.actions.priority("add", "Game One")["changed"])
        self.actions.priority("add", "Game Two")
        self.actions.priority("move", "Game Two", 1)
        self.assertEqual(self.actions.priority("list"), {"priority": ["Game Two", "Game One"]})
        self.actions.priority("remove", "Game One")
        self.assertTrue(self.actions.exclude("add", "Ignored")["changed"])
        self.assertFalse(self.actions.exclude("add", "Ignored")["changed"])
        self.actions.exclude("remove", "Ignored")
        self.assertEqual(len(self.twitch.states), 6)
        self.assertEqual(self.twitch.settings.alter.call_count, 6)
        for call in (
            lambda: self.actions.priority("move", "Game Two", 0),
            lambda: self.actions.priority("remove", "missing"),
            lambda: self.actions.exclude("remove", "missing"),
            lambda: self.actions.priority("wrong", "Game"),
        ):
            with self.assertRaises(ActionError):
                call()

    async def test_set_setting_bounds_and_masking(self) -> None:
        with self.assertRaises(ActionError):
            self.actions.set_setting("connection_quality", "7")
        with self.assertRaises(ActionError):
            self.actions.set_setting("unknown", "value")
        self.assertEqual(self.actions.set_setting("connection_quality", "6")["value"], "6")
        result = self.actions.set_setting("proxy", "http://alice:topsecret@localhost:8080")
        self.assertNotIn("topsecret", json.dumps(result))
        self.assertIn("***", result["value"])
        with self.assertRaises(ActionError) as context:
            self.actions.set_setting("proxy", "http://alice:topsecret@[invalid")
        self.assertNotIn("topsecret", str(context.exception))

    async def test_logout_rejection_is_typed_and_restart_is_preserved(self) -> None:
        auth = SimpleNamespace(access_token="token-value", invalidate=Mock())
        self.twitch.get_auth = AsyncMock(return_value=auth)
        self.twitch._client_type = SimpleNamespace(CLIENT_ID="client-id")

        class Request:
            async def __aenter__(self):
                return SimpleNamespace(status=403)

            async def __aexit__(self, *args):
                return None

        self.twitch.request = Mock(return_value=Request())
        with self.assertRaises(ActionRejected) as context:
            await self.actions.logout()
        self.assertEqual(str(context.exception), "logout failed (HTTP 403)")
        self.assertNotIn("token-value", str(context.exception))
        self.assertEqual(self.twitch.states, [State.RESTART])
        auth.invalidate.assert_not_called()

    async def test_inventory_events_are_emitted(self) -> None:
        events = []
        self.manager.subscribe(lambda event, data: events.append(event))
        drop = SimpleNamespace(id="drop")
        campaign = SimpleNamespace(id="campaign", drops=[drop])
        await self.manager.inv.add_campaign(campaign)
        self.manager.inv.update_drop(drop)
        self.manager.inv.clear()
        self.assertEqual(events, ["change", "change", "change"])

    async def test_console_status_text_stays_unchanged(self) -> None:
        messages = []
        self.manager.print = messages.append
        self.manager._command_status()
        self.assertEqual(messages, [
            "state: idle", "watched channel: -", "current drop: -",
            "websockets: 0/0 connected", "logged in: no",
        ])

    async def test_activation_code_is_not_kept_in_log_buffer(self) -> None:
        with patch("sys.stdout", self.output):
            await self.manager.login.ask_enter_code(
                URL("https://www.twitch.tv/activate?device-code=CODE-123", encoded=True),
                "CODE-123",
            )
        self.assertIn("CODE-123", self.output.getvalue())
        self.assertNotIn("CODE-123", "\n".join(self.manager.log_tail()))
        self.assertIn("device-code=<redacted>", "\n".join(self.manager.log_tail()))
        self.assertTrue(self.manager.log_tail(1)[0].endswith("Enter this code: <redacted>"))
        self.manager.print("Login successful, user ID: 123456789")
        self.assertTrue(self.manager.log_tail(1)[0].endswith("user ID: <redacted>"))
        self.manager.print("Websocket[0]: Adding topics: user-drop-events.123456789, onsite-notifications.123456789")
        self.assertNotIn("123456789", self.manager.log_tail(1)[0])
        self.assertIn("user-drop-events.<redacted>", self.manager.log_tail(1)[0])
        self.assertNotIn("123456789", self.output.getvalue())

    def test_id_filter_redacts_records_for_every_handler(self) -> None:
        import logging
        from cli import IdRedactingFilter
        record = logging.LogRecord("TwitchDrops", logging.INFO, __file__, 1,
                                   "Adding topics: %s", ("user-drop-events.987654321",), None)
        self.assertTrue(IdRedactingFilter().filter(record))
        self.assertEqual(record.getMessage(), "Adding topics: user-drop-events.<redacted>")

    async def test_encoded_device_code_url_is_masked_in_log_events(self) -> None:
        events = []
        self.manager.subscribe(lambda event, line: events.append(line) if event == "log" else None)
        url = "Open this activation URL: https://www.twitch.tv/activate?DEVICE%2DCode=CODE%2D123&mode=login"
        with patch("sys.stdout", self.output):
            self.manager.print(url)
        self.assertIn("CODE%2D123", self.output.getvalue())
        for line in (self.manager.log_tail(1)[0], events[-1]):
            self.assertNotIn("CODE%2D123", line)
            self.assertIn("DEVICE%2DCode=<redacted>&mode=login", line)


class ManagerEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.twitch = FakeTwitch()
        self.output = io.StringIO()
        with patch("sys.stdout", self.output):
            self.manager = CLIManager(self.twitch)

    def tearDown(self) -> None:
        self.manager.close_window()

    def test_log_buffer_maxlen_tail_and_handler(self) -> None:
        with patch("sys.stdout", self.output):
            for index in range(1005):
                self.manager.print(f"line {index}")
            self.manager._handler.emit(logging.LogRecord(
                "TwitchDrops", 20, __file__, 1, "handler line", (), None
            ))
        self.assertEqual(len(self.manager.log_tail(10000)), 1000)
        self.assertTrue(self.manager.log_tail(1)[0].endswith("handler line"))
        self.assertTrue(self.manager.log_tail(2)[0].endswith("line 1004"))
        self.assertEqual(self.manager.log_tail(0), [])
        with self.assertRaises(ActionError):
            self.manager.log_tail(-1)

    def test_change_events_and_listener_failure_isolated(self) -> None:
        events = []
        good = lambda event, data: events.append((event, data))
        bad = Mock(side_effect=RuntimeError("private details"))
        self.manager.subscribe(good)
        self.manager.subscribe(bad)
        channel = SimpleNamespace(id=1)
        with patch("sys.stdout", self.output):
            self.manager.status.update("Ready")
            self.manager.channels.display(channel, add=True)
            self.manager.channels.set_watching(channel)
            self.manager.websockets.update(1, status="connected")
            self.manager.set_logged_in(True)
            self.manager.set_games({Game({"id": 1, "name": "Game"})})
            self.manager.progress.display = Mock()
            drop = SimpleNamespace(
                id="drop", remaining_minutes=5, campaign=SimpleNamespace(
                    game=SimpleNamespace(name="Game")
                ), rewards_text=lambda: "Reward", progress=0.5,
            )
            self.manager.display_drop(drop)
            self.manager.clear_drop()
            self.manager.print("manual line")
        self.assertGreaterEqual(sum(event == "change" for event, _ in events), 8)
        self.assertTrue(any(event == "log" and data.endswith("manual line") for event, data in events))
        self.assertEqual(bad.call_count, 1)
        self.assertEqual(self.output.getvalue().count("CLI listener failed"), 1)
        self.manager.unsubscribe(good)
        before = len(events)
        with patch("sys.stdout", self.output):
            self.manager.print("after unsubscribe")
        self.assertEqual(len(events), before)
