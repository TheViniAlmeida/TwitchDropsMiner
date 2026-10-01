from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


class OfflineCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        self.environment = os.environ.copy()
        self.environment["TDM_DATA_DIR"] = str(self.data_dir)
        self.environment.pop("TDM_ALLOW_LOGOUT", None)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PYTHON), "main.py", "cli", *args],
            cwd=ROOT,
            env=self.environment,
            text=True,
            input="",
            capture_output=True,
            check=False,
        )

    def test_set_get_every_editable_key_and_invalid_values(self) -> None:
        values = {
            "proxy": "http://localhost:8080",
            "language": "English",
            "connection_quality": "6",
            "priority_mode": "ending_soonest",
            "enable_badges_emotes": "true",
            "available_drops_check": "true",
            "tray_notifications": "false",
        }
        for key, value in values.items():
            with self.subTest(key=key):
                result = self.cli("settings", "set", key, value)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.cli("settings", "get", key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"{key} = {value}")

        invalid = {
            "proxy": "not-a-url",
            "language": "Klingon",
            "connection_quality": "7",
            "priority_mode": "unknown",
            "enable_badges_emotes": "maybe",
            "available_drops_check": "maybe",
            "tray_notifications": "maybe",
        }
        for key, value in invalid.items():
            with self.subTest(invalid_key=key):
                result = self.cli("settings", "set", key, value)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_priority_and_exclude_commands(self) -> None:
        for command in (
            ("priority", "add", "Game One"),
            ("priority", "add", "Game Two"),
            ("priority", "move", "Game Two", "1"),
        ):
            result = self.cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("priority", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["1. Game Two", "2. Game One"])
        result = self.cli("priority", "remove", "Game One")
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.cli("exclude", "add", "Ignored Game")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("exclude", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["Ignored Game"])
        result = self.cli("exclude", "remove", "Ignored Game")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_logout_is_disabled_by_default_and_allowed_moves_cookies(self) -> None:
        cookies = self.data_dir / "cookies.jar"
        cookies.write_text("cookie data", encoding="utf8")
        result = self.cli("logout", "--yes")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("logout disabled: Twitch blocks new device logins for ANDROID_APP", result.stderr)
        self.assertTrue(cookies.exists())

        self.environment["TDM_ALLOW_LOGOUT"] = "1"
        result = self.cli("logout", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cookies.exists())
        self.assertEqual((self.data_dir / "cookies.jar.bak").read_text(encoding="utf8"), "cookie data")

        result = self.cli("logout")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("requires --yes", result.stderr)

    def test_logout_archives_previous_backup_before_removing_current(self) -> None:
        cookies = self.data_dir / "cookies.jar"
        backup = self.data_dir / "cookies.jar.bak"
        cookies.write_bytes(b"new login")
        backup.write_bytes(b"previous login")
        self.environment["TDM_ALLOW_LOGOUT"] = "1"
        result = self.cli("logout", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cookies.exists())
        self.assertEqual(backup.read_bytes(), b"new login")
        self.assertEqual(len(list(self.data_dir.glob("cookies.jar.bak.[0-9]*"))), 1)
        self.assertEqual(next(self.data_dir.glob("cookies.jar.bak.[0-9]*")).read_bytes(), b"previous login")

    def test_lock_held_exits_3(self) -> None:
        code = """
import sys
from constants import LOCK_PATH
from utils import lock_file

locked, handle = lock_file(LOCK_PATH)
assert locked
print("ready", flush=True)
sys.stdin.read()
handle.close()
"""
        holder = subprocess.Popen(
            [str(PYTHON), "-c", code],
            cwd=ROOT,
            env=self.environment,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            result = self.cli("settings", "show")
            self.assertEqual(result.returncode, 3, result.stderr)
        finally:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=5)
            if holder.stdout is not None:
                holder.stdout.close()
            if holder.stderr is not None:
                holder.stderr.close()

    def test_saved_format_matches_settings_save(self) -> None:
        for command in (
            ("settings", "set", "connection_quality", "6"),
            ("settings", "set", "priority_mode", "low_avbl_first"),
            ("priority", "add", "Game One"),
            ("exclude", "add", "Ignored Game"),
        ):
            result = self.cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)

        expected_path = self.data_dir / "settings.json"
        copied_path = self.data_dir / "settings-copy.json"
        code = """
from pathlib import Path
from types import SimpleNamespace

from settings import Settings
from utils import json_save

settings = Settings(SimpleNamespace())
json_save(Path(__import__("os").environ["TDM_DATA_DIR"]) / "settings-copy.json", settings._settings, sort=True)
"""
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(expected_path.read_bytes(), copied_path.read_bytes())
        self.assertEqual(json.loads(expected_path.read_text(encoding="utf8"))["connection_quality"], 6)
