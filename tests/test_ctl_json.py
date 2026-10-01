from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict

from cli import CLIManager
from cli_actions import CampaignFilters
from cli_commands import CommandError, json_command_allowed
from control import MAX_REQUEST, ControlServer, send_command
from tests.test_console_dispatcher import FakeTwitch


class CommandDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = CLIManager(FakeTwitch())

    def tearDown(self) -> None:
        self.manager.close_window()

    def test_read_commands_return_structured_data(self) -> None:
        self.manager.actions.state = lambda: {"state": "IDLE", "current_drop": None}
        self.assertEqual(self.manager.command_data("status"), {"state": "IDLE", "current_drop": None})
        self.manager.actions.game = lambda name: {"name": name}
        self.assertEqual(self.manager.command_data("game Two Words"), {"name": "Two Words"})
        self.manager._campaign_filters = CampaignFilters()
        self.assertEqual(self.manager.command_data("filters"), asdict(CampaignFilters()))

    def test_changes_and_unknown_commands_are_refused(self) -> None:
        for line in ("logout", "switch someone", "set language English", "priority add Game",
                     "watch", "status extra", "drops", "", "unknown"):
            with self.subTest(line=line), self.assertRaises(CommandError):
                self.manager.command_data(line)

    def test_error_messages_are_redacted(self) -> None:
        def missing(target: str) -> None:
            raise CommandError(f"no campaign or game named {target}")

        self.manager.actions.drops = missing
        with self.assertRaises(CommandError) as raised:
            self.manager.command_data("drops https://www.twitch.tv/activate?device-code=SECRET")
        self.assertNotIn("SECRET", str(raised.exception))

    def test_strings_are_redacted_like_the_text_output(self) -> None:
        self.manager.actions.progress = lambda: {"name": "Enter this code: SECRET", "items": ["Enter this code: X"]}
        data = self.manager.command_data("progress")
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertEqual(data["items"], ["Enter this code: <redacted>"])

    def test_unexpected_failure_is_a_generic_error(self) -> None:
        def broken() -> None:
            raise RuntimeError("internal detail")

        self.manager.actions.channels = broken
        with self.assertLogs("TwitchDrops", "ERROR"), self.assertRaisesRegex(CommandError, "^command failed$"):
            self.manager.command_data("channels")


class ControlJSONTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.directory.name)
        self.manager = CLIManager(FakeTwitch())
        self.server = ControlServer(self.manager, self.data_dir)
        try:
            await self.server.start()
        except PermissionError:
            self.manager.close_window()
            self.directory.cleanup()
            self.skipTest("local sockets are forbidden in this sandbox")

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        self.manager.close_window()
        self.directory.cleanup()

    async def test_large_reply_arrives_whole(self) -> None:
        # quotes, backslashes and non-ASCII grow when escaped: the reply must still pass the packet limit
        value = [{"name": f'"jogo\\ção" {index}', "minutes": index} for index in range(2000)]
        self.manager.actions.channels = lambda: value
        output: list[str] = []
        errors: list[str] = []
        code = await send_command(self.data_dir, "channels", output=output.append,
                                  json_output=True, errors=errors.append)
        self.assertEqual(code, 0, errors)
        self.assertGreater(len(output[0]), MAX_REQUEST)
        self.assertEqual(json.loads(output[0]), value)
        self.assertEqual(errors, [])

    async def test_error_goes_to_errors_only(self) -> None:
        output: list[str] = []
        errors: list[str] = []
        code = await send_command(self.data_dir, "logout", output=output.append,
                                  json_output=True, errors=errors.append)
        self.assertEqual(code, 1)
        self.assertEqual(output, [])
        self.assertTrue(errors and errors[0].startswith("error: --json supports"))

    async def test_text_mode_is_unchanged(self) -> None:
        output: list[str] = []
        self.assertEqual(await send_command(self.data_dir, "help", output=output.append), 0)
        self.assertTrue(any("commands:" in line for line in output))


class OlderMinerTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_reply_to_json_request_is_an_error(self) -> None:
        if sys.platform == "win32":
            self.skipTest("Unix socket test")

        async def old_miner(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(b'{"line": "state: IDLE"}\n{"done": true, "ok": true}\n')
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            server = await asyncio.start_unix_server(old_miner, str(Path(directory, "control.sock")))
            try:
                output: list[str] = []
                errors: list[str] = []
                code = await send_command(Path(directory), "status", output=output.append,
                                          json_output=True, errors=errors.append, tcp=False)
            finally:
                server.close()
                await server.wait_closed()
        self.assertEqual(code, 1)
        self.assertEqual(output, [])
        self.assertIn("does not support --json", errors[-1])


class JSONAllowListTests(unittest.TestCase):
    def test_only_reads_are_allowed(self) -> None:
        for words in (["status"], ["STATUS"], ["priority"], ["priority", "LIST"], ["exclude", "list"],
                      ["filters"], ["get", "language"], ["campaigns", "--all"], ["drops", "Game"]):
            with self.subTest(words=words):
                self.assertTrue(json_command_allowed(words))
        for words in ([], ["logout"], ["switch", "x"], ["set", "language", "English"], ["reload"],
                      ["priority", "add", "Game"], ["exclude", "remove", "Game"], ["filters", "all=on"],
                      ["watch"], ["quit"], ["help"]):
            with self.subTest(words=words):
                self.assertFalse(json_command_allowed(words))


class CtlArgumentTests(unittest.TestCase):
    def test_json_without_command_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "main.py", "cli", "ctl", "--json"],
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TDM_DATA_DIR": directory},
                capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--json needs a command", result.stderr)

    def test_change_is_refused_before_contacting_the_miner(self) -> None:
        # exit 2, not 3 (no miner): an older miner never gets the chance to run it as text
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "main.py", "cli", "ctl", "--json", "set", "language", "English"],
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TDM_DATA_DIR": directory},
                capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--json supports", result.stderr)
        self.assertEqual(result.stdout, "")
