from __future__ import annotations

import asyncio
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

    @staticmethod
    def record(dashboard: Dashboard) -> None:
        dashboard._sample_history()
        dashboard._save_history()

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_samples_survive_a_restart(self) -> None:
        first = self.dashboard()
        first._load_history()
        with patch("dashboard.time.time", return_value=time.time() - 60):
            self.record(first)
        self.assertEqual(len(self.lines()), 1)
        second = self.dashboard()
        second._load_history()
        self.record(second)
        self.assertEqual(len(second.history), 2)
        self.assertLess(second.history[0]["t"], second.history[1]["t"])
        self.assertEqual(self.lines(), list(second.history))

    def test_save_is_flushed_to_disk_before_the_rename(self) -> None:
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with patch("private_file.os.fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))), \
                patch("private_file.os.replace", side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))):
            self.record(self.dashboard())
        expected = ["fsync", "replace"] + ([] if sys.platform == "win32" else ["fsync"])
        self.assertEqual(calls, expected)

    def test_owner_only_file(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        dashboard = self.dashboard()
        self.record(dashboard)
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

    def test_file_holds_exactly_what_the_panel_shows(self) -> None:
        dashboard = self.dashboard()
        for _ in range(_HISTORY_SAMPLES + 5):
            self.record(dashboard)
        self.assertEqual(len(dashboard.history), _HISTORY_SAMPLES)
        self.assertEqual(self.lines(), list(dashboard.history))

    def test_too_large_file_is_replaced(self) -> None:
        with patch("dashboard._HISTORY_MAX_BYTES", 10):
            self.path.write_text(json.dumps(_sample(int(time.time()) - 60)) + "\n", encoding="utf-8")
            dashboard = self.dashboard()
            with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
                dashboard._load_history()
            self.assertEqual(len(dashboard.history), 0)
            self.record(dashboard)
        self.assertEqual(len(self.lines()), 1)

    def test_symlink_is_neither_read_nor_followed(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX symlink test")
        target = Path(self.directory.name, "elsewhere")
        target.write_text(json.dumps(_sample(int(time.time()) - 60)) + "\n", encoding="utf-8")
        os.symlink(target, self.path)
        dashboard = self.dashboard()
        with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
            dashboard._load_history()
        self.assertEqual(len(dashboard.history), 0)
        before = target.read_text(encoding="utf-8")
        self.record(dashboard)
        # the link itself is replaced by a private file; its target stays as it was
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertFalse(self.path.is_symlink())

    def test_fifo_does_not_hang_the_start(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs here")
        os.mkfifo(self.path)
        dashboard = self.dashboard()
        with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
            dashboard._load_history()
        self.assertEqual(len(dashboard.history), 0)

    def test_loose_permissions_do_not_survive_a_save(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX mode bits")
        self.path.write_text("", encoding="utf-8")
        os.chmod(self.path, 0o644)
        self.record(self.dashboard())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_torn_last_line_costs_only_itself(self) -> None:
        now = int(time.time())
        self.path.write_text(json.dumps(_sample(now - 120)) + "\n" + json.dumps(_sample(now - 60))[:30],
                             encoding="utf-8")
        dashboard = self.dashboard()
        dashboard._load_history()
        self.record(dashboard)
        self.assertEqual([item["t"] for item in self.lines()], [now - 120, dashboard.history[-1]["t"]])

    def test_deeply_nested_line_is_skipped(self) -> None:
        now = int(time.time())
        deep = "[" * 100000 + "]" * 100000
        with patch("dashboard._HISTORY_MAX_LINE", 10 ** 6):
            self.path.write_text(deep + "\n" + json.dumps(_sample(now - 60)) + "\n", encoding="utf-8")
            dashboard = self.dashboard()
            dashboard._load_history()
        self.assertEqual([item["t"] for item in dashboard.history], [now - 60])

    def test_wrong_types_are_rejected(self) -> None:
        now = int(time.time())
        bad = [{"progress": True}, {"t": True}, {"claimed": "1"}, {"campaign": 5},
               {"drop_id": ["x"]}, {"remaining_minutes": False}, {"total": None}]
        rows = [{**_sample(now - 200 + index), **change} for index, change in enumerate(bad)]
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        dashboard = self.dashboard()
        dashboard._load_history()
        self.assertEqual(len(dashboard.history), 0)

    def test_restart_of_the_same_dashboard_drops_old_samples(self) -> None:
        dashboard = self.dashboard()
        dashboard.history.append(_sample(int(time.time()) - 2 * 86400))
        dashboard.history.append(_sample(int(time.time()) - 60))
        dashboard._load_history()
        self.assertEqual(len(dashboard.history), 1)

    def test_clock_set_back_keeps_time_order(self) -> None:
        dashboard = self.dashboard()
        now = int(time.time())
        dashboard.history.append(_sample(now + 600))
        self.record(dashboard)
        times = [item["t"] for item in dashboard.history]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(times[-1], now + 1)

    def test_non_finite_numbers_are_rejected(self) -> None:
        now = int(time.time())
        rows = ['{"t": %d, "drop_id": null, "campaign": null, "progress": 0.5, "remaining_minutes": %s, '
                '"claimed": 0, "total": 0}' % (now - 100 + index, value)
                for index, value in enumerate(("1e999", "NaN", "-Infinity"))]
        self.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        dashboard = self.dashboard()
        dashboard._load_history()
        self.assertEqual(len(dashboard.history), 0)

    def test_huge_integer_is_not_a_crash(self) -> None:
        now = int(time.time())
        row = {**_sample(now - 60), "claimed": 10 ** 309}
        self.path.write_text(json.dumps(row) + "\n" + json.dumps(_sample(now - 30)) + "\n", encoding="utf-8")
        dashboard = self.dashboard()
        dashboard._load_history()
        self.assertEqual(len(dashboard.history), 2)
        self.assertEqual(json.loads(dashboard._history_text().splitlines()[0])["claimed"], 10 ** 309)

    def test_stop_waits_for_a_write_in_flight(self) -> None:
        import threading
        dashboard = self.dashboard()
        release = threading.Event()
        finished = []

        def slow_write(text: str) -> None:
            release.wait(5)
            finished.append(text)

        dashboard._write_history = slow_write

        async def scenario() -> None:
            recorder = asyncio.create_task(dashboard._record_history())
            await asyncio.sleep(.05)
            dashboard._history_task = recorder
            stopping = asyncio.create_task(dashboard.stop())
            await asyncio.sleep(.05)
            self.assertFalse(stopping.done())
            release.set()
            await stopping

        asyncio.run(scenario())
        self.assertEqual(len(finished), 1)
        self.assertIsNone(dashboard._history_write)

    def test_day_old_samples_leave_while_running(self) -> None:
        dashboard = self.dashboard()
        dashboard.history.append(_sample(int(time.time()) - 25 * 3600))
        dashboard._sample_history()
        self.assertEqual(len(dashboard.history), 1)
        self.assertGreater(dashboard.history[0]["t"], time.time() - 60)

    def test_recording_saves_off_the_event_loop(self) -> None:
        dashboard = self.dashboard()
        threads = []

        def write(text: str) -> None:
            import threading
            threads.append(threading.current_thread() is threading.main_thread())
            Dashboard._write_history(dashboard, text)

        dashboard._write_history = write
        asyncio.run(dashboard._record_history())
        self.assertEqual(threads, [False])
        self.assertEqual(len(self.lines()), 1)

    def test_repeated_save_failure_with_random_names_warns_once(self) -> None:
        dashboard = self.dashboard()
        names = iter(range(10))

        def failing(path, content):
            raise OSError(f"cannot restrict access to .tmp{next(names)} (icacls exit 5)")

        with patch("dashboard.rewrite_private", failing), \
                self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
            for _ in range(3):
                self.record(dashboard)
        self.assertEqual(len(warnings.output), 1)

    def test_hung_acl_tool_fails_instead_of_freezing(self) -> None:
        import subprocess
        import private_file

        with patch.object(private_file, "WINDOWS", True), \
                patch("private_file.subprocess.run", side_effect=subprocess.TimeoutExpired("whoami", 30)):
            with self.assertRaisesRegex(OSError, "timed out"):
                private_file.restrict_to_owner(self.path)

    def test_save_failure_warns_once_and_keeps_sampling(self) -> None:
        dashboard = self.dashboard(Path(self.directory.name, "missing", "history.jsonl"))
        with self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
            self.record(dashboard)
            self.record(dashboard)
        self.assertEqual(len(warnings.output), 1)
        self.assertEqual(len(dashboard.history), 2)

    def test_memory_only_without_a_file(self) -> None:
        manager = FakeManager()
        manager.actions = FakeActions()
        dashboard = Dashboard(manager, FakeTwitch(), DashboardConfig())
        dashboard._load_history()
        self.record(dashboard)
        self.assertEqual(len(dashboard.history), 1)
        self.assertEqual(os.listdir(self.directory.name), [])
