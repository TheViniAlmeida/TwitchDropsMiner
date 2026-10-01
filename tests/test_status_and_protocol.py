from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import get_type_hints
import unittest
from unittest.mock import patch

from cli import CLIManager, _Status
from ui_base import (
    CampaignProgress,
    ChannelList,
    InventoryOverview,
    LoginForm,
    StatusBar,
    TrayIcon,
    UIManager,
    WebsocketStatus,
)


class StatusThrottlingTests(unittest.TestCase):
    def test_counter_statuses_print_first_and_last_within_five_seconds(self) -> None:
        messages: list[str] = []
        status = _Status(SimpleNamespace(print=messages.append))
        with patch("cli.monotonic", side_effect=(0.0, 1.0, 2.0)):
            status.update("Loading (1/3)")
            status.update("Loading (2/3)")
            status.update("Loading (3/3)")
        self.assertEqual(messages, ["[status] Loading (1/3)", "[status] Loading (3/3)"])

    def test_non_counter_statuses_print_only_on_change(self) -> None:
        messages: list[str] = []
        status = _Status(SimpleNamespace(print=messages.append))
        status.update("Connected")
        status.update("Connected")
        status.update("Disconnected")
        self.assertEqual(messages, ["[status] Connected", "[status] Disconnected"])


class UIProtocolConformanceTests(unittest.TestCase):
    def test_cli_manager_implements_ui_manager_and_subprotocols(self) -> None:
        twitch = SimpleNamespace(settings=SimpleNamespace(tray_notifications=True))
        manager = CLIManager(twitch)
        self.addCleanup(manager.close_window)
        self.assert_methods_compatible(UIManager, manager)
        annotations = get_type_hints(UIManager)
        for attribute, protocol in annotations.items():
            self.assertTrue(hasattr(manager, attribute), f"CLIManager.{attribute} is missing")
            self.assert_methods_compatible(protocol, getattr(manager, attribute), attribute)

    def assert_methods_compatible(self, protocol, implementation, attribute: str = "") -> None:
        for name, expected in protocol.__dict__.items():
            if name.startswith("_"):
                continue
            actual = getattr(implementation, name, None)
            self.assertIsNotNone(actual, f"{protocol.__name__}.{name} is missing")
            if isinstance(expected, property):
                continue
            if not callable(expected):
                continue
            expected_params = list(inspect.signature(expected).parameters.values())[1:]
            actual_params = list(inspect.signature(actual).parameters.values())
            self.assertEqual(
                [(parameter.kind, parameter.name) for parameter in actual_params],
                [(parameter.kind, parameter.name) for parameter in expected_params],
                f"{protocol.__name__}.{name}",
            )
