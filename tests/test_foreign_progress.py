from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_console_dispatcher import FakeTwitch  # noqa: F401  (blocks GUI modules)
import twitch


class ForeignProgressWarningTests(unittest.TestCase):
    def test_warns_once_per_game_every_half_hour(self) -> None:
        miner = SimpleNamespace(_foreign_progress_warned={}, print=MagicMock())
        drop = SimpleNamespace(name="Tier 1", campaign=SimpleNamespace(game="AION 2"))
        with patch.object(twitch, "time", side_effect=[1000.0, 1100.0, 3000.0]):
            for _ in range(3):
                twitch.Twitch._warn_foreign_progress(miner, drop)
        self.assertEqual(miner.print.call_count, 2)
        self.assertIn("Another session of this account is earning AION 2", miner.print.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
