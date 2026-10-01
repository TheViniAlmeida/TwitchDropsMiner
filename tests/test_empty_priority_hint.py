from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests.test_console_dispatcher import FakeTwitch  # noqa: F401  (blocks GUI modules)
from constants import PriorityMode
import twitch


def _miner(mode: PriorityMode, priority: list[str]) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(priority_mode=mode, priority=priority))


class EmptyPriorityHintTests(unittest.TestCase):
    def test_priority_only_with_an_empty_list_explains_the_idle_state(self) -> None:
        hint = twitch.Twitch._empty_priority_hint(_miner(PriorityMode.PRIORITY_ONLY, []))
        self.assertIsNotNone(hint)
        self.assertIn("priority list is empty", hint)
        self.assertIn("priority_mode ending_soonest", hint)

    def test_no_hint_when_games_can_be_eligible(self) -> None:
        for mode, priority in ((PriorityMode.PRIORITY_ONLY, ["VALORANT"]),
                               (PriorityMode.ENDING_SOONEST, []),
                               (PriorityMode.LOW_AVBL_FIRST, [])):
            with self.subTest(mode=mode, priority=priority):
                self.assertIsNone(twitch.Twitch._empty_priority_hint(_miner(mode, priority)))


if __name__ == "__main__":
    unittest.main()
