from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dashboard import _HISTORY_SAMPLES, Dashboard, DashboardConfig
from tests.test_dashboard import FakeManager, FakeTwitch
from tests.test_dashboard_v2 import FakeActions


def _sample(t: int, progress: float = .5) -> dict:
    return {"t": t, "drop_id": "drop-1", "campaign": "Campaign", "progress": progress,
            "remaining_minutes": 12, "claimed": 0, "total": 0}


class HistoryFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name, "dashboard-history.jsonl")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def dashboard(self, path: Path | None = None) -> Dashboard:
        manager = FakeManager()
        manager.actions = FakeActions()
        return Dashboard(manager, FakeTwitch(), DashboardConfig(history_file=path or self.path))

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_samples_survive_a_restart(self) -> None:
        first = self.dashboard()
        first._load_history()
        with patch("dashboard.time.time", return_value=time.time() - 60):
            first._sample_history()
        self.assertEqual(len(self.lines()), 1)
        second = self.dashboard()
        second._load_history()
        second._sample_history()
        self.assertEqual(len(second.history), 2)
        self.assertLess(second.history[0]["t"], second.history[1]["t"])
        self.assertEqual(self.lines(), list(second.history))

    def test_owner_only_file(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        dashboard = self.dashboard()
        dashboard._sample_history()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_old_invalid_and_future_samples_are_dropped(self) -> None:
        now = int(time.time())
        rows = [_sample(now - 2 * 86400), _sample(now - 120), "not json", json.dumps({"t": now - 90}),
                _sample(now - 100, progress=7), _sample(now - 130), _sample(now - 60), _sample(now + 3600)]
        self.path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n"
                                     for row in rows), encoding="utf-8")
        dashboard = self.dashboard()
        dashboard._load_history()
        # older than a day, malformed, out of range, out of order and future rows are all skipped
        self.assertEqual([item["t"] for item in dashboard.history], [now - 120, now - 60])

    def test_file_is_compacted(self) -> None:
        dashboard = self.dashboard()
        for _ in range(2 * _HISTORY_SAMPLES + 5):
            dashboard._sample_history()
        self.assertLessEqual(len(self.lines()), 2 * _HISTORY_SAMPLES)
        self.assertLessEqual(len(dashboard.history), _HISTORY_SAMPLES)

    def test_too_large_file_is_replaced(self) -> None:
        with patch("dashboard._HISTORY_MAX_BYTES", 10):
            self.path.write_text(json.dumps(_sample(int(time.time()) - 60)) + "\n", encoding="utf-8")
            dashboard = self.dashboard()
            with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
                dashboard._load_history()
            self.assertEqual(len(dashboard.history), 0)
            dashboard._sample_history()
        self.assertEqual(len(self.lines()), 1)

    def test_symlink_is_not_followed(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX symlink test")
        target = Path(self.directory.name, "elsewhere")
        target.write_text("keep\n", encoding="utf-8")
        os.symlink(target, self.path)
        dashboard = self.dashboard()
        with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
            dashboard._load_history()
            dashboard._sample_history()
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_save_failure_warns_once_and_keeps_sampling(self) -> None:
        dashboard = self.dashboard(Path(self.directory.name, "missing", "history.jsonl"))
        with self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
            dashboard._sample_history()
            dashboard._sample_history()
        self.assertEqual(len(warnings.output), 1)
        self.assertEqual(len(dashboard.history), 2)

    def test_memory_only_without_a_file(self) -> None:
        manager = FakeManager()
        manager.actions = FakeActions()
        dashboard = Dashboard(manager, FakeTwitch(), DashboardConfig())
        dashboard._load_history()
        dashboard._sample_history()
        self.assertEqual(len(dashboard.history), 1)
        self.assertEqual(os.listdir(self.directory.name), [])
