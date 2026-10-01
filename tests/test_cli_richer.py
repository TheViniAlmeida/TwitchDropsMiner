from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
from itertools import product
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yarl import URL

from cli import CLIManager
from cli_actions import ActionError, CampaignFilters, campaign_visible
from constants import PriorityMode
from utils import Game


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_campaign(name: str = "Campaign", game: str = "Alpha", *, phase: str = "active"):
    campaign = SimpleNamespace(
        id=name.lower(), name=name, game=SimpleNamespace(name=game),
        active=phase == "active", upcoming=phase == "upcoming", expired=phase == "expired",
        eligible=True, linked=True, link_url="https://example.org/link?game=alpha",
        finished=False, required_minutes=20, remaining_minutes=15,
        starts_at=NOW, ends_at=NOW + timedelta(days=1),
        image_url=URL("https://static-cdn.jtvnw.net/game.jpg"),
        progress=0.25, claimed_drops=0, total_drops=1, allowed_channels=[], drops=[],
    )
    drop = SimpleNamespace(
        id=name.lower() + "-drop", name="Reward", campaign=campaign,
        is_claimed=False, can_claim=False, preconditions_met=True, can_earn=lambda: True,
        current_minutes=5, required_minutes=20, remaining_minutes=15, progress=0.25,
        starts_at=NOW, ends_at=NOW + timedelta(days=1),
        benefits=[SimpleNamespace(name="Emote", image_url="https://static-cdn.jtvnw.net/emote.png")],
    )
    campaign.drops = [drop]
    return campaign


class RichActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = SimpleNamespace(
            proxy=URL("http://alice:secret@localhost:8080"), language="English",
            connection_quality=3, priority_mode=PriorityMode.ENDING_SOONEST,
            enable_badges_emotes=False, available_drops_check=True,
            tray_notifications=False, dark_mode=False, autostart_tray=False,
            priority=[], exclude=set(),
        )
        self.twitch = SimpleNamespace(settings=self.settings, channels={}, websocket=SimpleNamespace(websockets=[]))
        with patch("sys.stdout", io.StringIO()):
            self.manager = CLIManager(self.twitch)
        self.actions = self.manager.actions
        self.messages: list[str] = []
        self.manager.print = self.messages.append

    async def asyncTearDown(self):
        self.manager.close_window()
        await asyncio.sleep(0)

    async def test_visibility_truth_table_matches_gui_expression(self):
        campaign = make_campaign()
        names = ("not_linked", "upcoming", "expired", "excluded", "finished")
        for switches, phase, eligible, finished, sub_only, excluded, prioritized, mode in product(
            product((False, True), repeat=5), ("active", "upcoming", "expired"),
            (False, True), (False, True), (False, True), (False, True),
            (False, True), (PriorityMode.PRIORITY_ONLY, PriorityMode.ENDING_SOONEST),
        ):
            filters = CampaignFilters(**dict(zip(names, switches)))
            campaign.active = phase == "active"
            campaign.upcoming = phase == "upcoming"
            campaign.expired = phase == "expired"
            campaign.eligible = eligible
            campaign.finished = finished
            campaign.required_minutes = 0 if sub_only else 20
            self.settings.exclude = {"Alpha"} if excluded else set()
            self.settings.priority = ["Alpha"] if prioritized else []
            self.settings.priority_mode = mode
            expected = (
                campaign.required_minutes > 0
                and (filters.not_linked or campaign.eligible)
                and (campaign.active or filters.upcoming and campaign.upcoming
                     or filters.expired and campaign.expired)
                and (filters.excluded or (
                    campaign.game.name not in self.settings.exclude
                    and mode is not PriorityMode.PRIORITY_ONLY
                    or campaign.game.name in self.settings.priority
                ))
                and (filters.finished or not campaign.finished)
            )
            self.assertEqual(campaign_visible(campaign, filters, self.settings), expected)

    async def test_campaigns_and_drop_statuses_and_target_errors(self):
        campaign = make_campaign()
        await self.manager.inv.add_campaign(campaign)
        drop = campaign.drops[0]
        self.manager._current_drop = drop
        self.manager.progress.seconds = 42
        for claimed, claimable, prerequisites, earnable, minutes, expected in (
            (True, True, False, False, 20, "claimed"),
            (False, True, False, False, 20, "claimable"),
            (False, False, False, True, 5, "locked"),
            (False, False, True, True, 5, "in_progress"),
            (False, False, True, True, 0, "pending"),
            (False, False, True, False, 0, "locked"),
        ):
            drop.is_claimed = claimed
            drop.can_claim = claimable
            drop.preconditions_met = prerequisites
            drop.can_earn = lambda: earnable
            drop.current_minutes = minutes
            self.assertEqual(self.actions.campaigns()[0]["drops"][0]["status"], expected)
        drop.can_earn = lambda: True
        rows = self.actions.campaigns()
        self.assertTrue(rows[0]["drops"][0]["watching"])
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual(rows[0]["link_url"], "https://example.org/link?game=alpha")
        self.assertEqual(self.actions.progress()["timer_seconds"], 42)
        self.assertEqual(len(self.actions.drops("ALPHA")), 1)
        self.assertEqual(len(self.actions.drops("CAMPAIGN")), 1)
        self.assertEqual(len(self.actions.drops("campaign")), 1)
        self.assertEqual(self.actions.campaigns({"not_linked": True})[0]["id"], campaign.id)
        campaign.required_minutes = 0
        self.assertEqual(self.actions.campaigns(include_all=True), [])
        campaign.required_minutes = 20
        with self.assertRaises(ActionError):
            self.actions.campaigns({"unknown": True})
        with self.assertRaises(ActionError):
            self.actions.drops("missing")
        duplicate = make_campaign("Campaign", "Beta")
        duplicate.id = "different-id"
        await self.manager.inv.add_campaign(duplicate)
        with self.assertRaisesRegex(ActionError, "ambiguous"):
            self.actions.drops("campaign")

    async def test_games_aggregation_choices_and_settings_schema(self):
        self.manager.set_games({Game({"id": 1, "name": "Alpha"}), Game({"id": 2, "name": "Lonely"})})
        campaign = make_campaign()
        await self.manager.inv.add_campaign(campaign)
        self.twitch.channels[1] = SimpleNamespace(
            id=1, name="Online", online=True, game=SimpleNamespace(name="Alpha"),
            viewers=10, drops_enabled=True, acl_based=False,
        )
        self.assertEqual(self.actions.game_names(), ["Alpha", "Lonely"])
        self.assertEqual(self.actions.game_choices(), ["Alpha", "Lonely"])
        self.assertEqual(self.actions.game("alpha")["status"], "available")
        self.assertEqual(self.actions.game("Alpha")["active_campaigns"], 1)
        self.assertEqual(self.actions.game("Alpha")["online_channels"][0]["name"], "Online")
        self.manager.channels.set_watching(self.twitch.channels[1])
        self.assertTrue(self.actions.game("Alpha")["watching"])
        self.twitch.channels[1].online = False
        self.assertTrue(self.actions.game("Alpha")["watching"])
        self.assertEqual(self.actions.game("Alpha")["online_channels"], [])
        self.twitch.channels[1].online = True
        self.assertEqual(self.actions.game("Lonely")["status"], "not_linked")
        campaign.active, campaign.upcoming = False, True
        self.assertEqual(self.actions.game("Alpha")["status"], "upcoming")
        campaign.finished = True
        self.assertEqual(self.actions.game("Alpha")["status"], "finished")
        campaign.finished = False
        campaign.eligible = False
        self.assertEqual(self.actions.game("Alpha")["status"], "not_linked")
        self.settings.exclude.add("Alpha")
        self.assertEqual(self.actions.game("Alpha")["status"], "excluded")
        self.settings.priority = ["Alpha"]
        self.assertEqual(self.actions.game("Alpha")["status"], "not_linked")
        self.settings.priority = []
        self.manager._current_drop = campaign.drops[0]
        self.assertEqual(self.actions.game("Alpha")["status"], "mining")
        with self.assertRaises(ActionError):
            self.actions.game("missing")
        schema = self.actions.settings_schema()
        self.assertEqual(set(schema["language"]["choices"]),
                         {path.stem for path in Path("lang").glob("*.json")})
        self.assertEqual(schema["connection_quality"]["choices"], list(range(1, 7)))
        self.assertEqual(schema["priority_mode"]["choices"], [mode.name.lower() for mode in PriorityMode])
        self.assertEqual(schema["enable_badges_emotes"]["value"], False)
        self.assertNotIn("secret", json.dumps(schema))

    async def test_console_commands_and_filter_session(self):
        campaign = make_campaign()
        await self.manager.inv.add_campaign(campaign)
        self.manager.set_games({Game({"id": 1, "name": "Alpha"})})
        for command, expected in (
            ("help", "watch [seconds]"),
            ("campaigns", "Alpha | Campaign | active"),
            ("drops alpha", "Reward | in_progress"),
            ("game alpha", "game: Alpha | available"),
            ("games", "Alpha | available"),
            ("games --names", "Alpha"),
            ("progress", "current drop: -"),
            ("filters", "not_linked = off"),
            ("settings", "language | choice | English"),
        ):
            self.messages.clear()
            await self.manager.dispatch_command(command)
            self.assertTrue(any(expected in message for message in self.messages), (command, self.messages))
        campaign.eligible = False
        self.messages.clear()
        await self.manager.dispatch_command("campaigns")
        self.assertEqual(len(self.messages), 1)
        await self.manager.dispatch_command("filters not_linked=on")
        await self.manager.dispatch_command("campaigns")
        self.assertTrue(any("Alpha | Campaign" in message for message in self.messages))
        self.messages.clear()
        await self.manager.dispatch_command("campaigns --all --filter upcoming=on Alpha")
        self.assertTrue(any("Alpha | Campaign" in message for message in self.messages))
        self.messages.clear()
        with patch.object(self.actions, "campaigns", wraps=self.actions.campaigns) as campaigns:
            await self.manager.dispatch_command("campaigns --all Dead by Daylight")
        self.assertEqual(campaigns.call_args.kwargs["game"], "Dead by Daylight")
        self.manager._current_drop = campaign.drops[0]
        self.manager.progress.seconds = 15
        self.messages.clear()
        await self.manager.dispatch_command("progress")
        self.assertIn("[#####---------------] 25.0%", self.messages[0])
        self.assertIn("15s", self.messages[0])

    async def test_watch_start_stop_and_bad_interval(self):
        await self.manager.dispatch_command("watch 0")
        self.assertIn("error: watch seconds must be a positive integer", self.messages)
        self.messages.clear()
        await self.manager.dispatch_command("watch 1")
        self.assertEqual(self.messages, ["current drop: -"])
        self.assertIsNotNone(self.manager._watch_task)
        await asyncio.sleep(1.05)
        self.assertEqual(self.messages, ["current drop: -", "current drop: -"])
        await self.manager.dispatch_command("")
        self.assertIsNone(self.manager._watch_task)
        self.messages.clear()
        await asyncio.sleep(0.05)
        self.assertEqual(self.messages, [])
        await self.manager.dispatch_command("watch")
        self.assertIsNotNone(self.manager._watch_task)
        self.manager.close_window()
        self.assertIsNone(self.manager._watch_task)


class LinkUrlTests(unittest.TestCase):
    def test_link_url_keeps_query_but_drops_credentials(self) -> None:
        from cli_actions import Actions
        self.assertEqual(
            Actions._safe_url("https://user:pw@example.com/link?game=1#x", keep_query=True),
            "https://example.com/link?game=1",
        )
        self.assertEqual(Actions._safe_url("https://cdn.example/a.png?x=1"), "https://cdn.example/a.png")
        self.assertEqual(
            Actions._safe_url(
                "https://example.com/link?game=1&access_token=a&Device-Code=b&code=c", keep_query=True
            ),
            "https://example.com/link?game=1",
        )
        self.assertEqual(
            Actions._safe_url(
                "https://example.com/link?ref=tw&user_id=7&Cookie=x&api_key=y&session_id=z",
                keep_query=True,
            ),
            "https://example.com/link?ref=tw",
        )
