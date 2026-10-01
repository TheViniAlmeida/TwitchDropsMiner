from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

from cli import CLIManager
from control import ControlServer, open_control, send_command
from tests.test_console_dispatcher import FakeTwitch


class ControlTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_unix_permissions_stale_cleanup_and_stop(self) -> None:
        if self.server.tcp:
            self.skipTest("Unix socket test")
        path = self.server.path
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            await ControlServer(self.manager, self.data_dir).start()
        await self.server.stop()
        self.assertFalse(path.exists())
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(path))
        stale.close()
        await self.server.start()
        self.assertTrue(path.exists())

    async def test_long_data_dir_path(self) -> None:
        if self.server.tcp or not Path("/proc/self/fd").is_dir():
            self.skipTest("Linux Unix socket test")
        deep = self.data_dir / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        server = ControlServer(self.manager, deep)
        await server.start()
        try:
            self.assertGreater(len(str(server.path)), 108)
            self.assertEqual(server.path.stat().st_mode & 0o777, 0o600)
            output: list[str] = []
            self.assertEqual(await send_command(deep, "help", output=output.append), 0)
            self.assertTrue(any("commands:" in line for line in output))
        finally:
            await server.stop()
        self.assertFalse(server.path.exists())

    async def test_output_is_private_and_bad_requests_are_bounded(self) -> None:
        output: list[str] = []
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(await send_command(self.data_dir, "help", output=output.append), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(any("commands:" in line for line in output))
        reader, writer = await open_control(self.data_dir)
        try:
            writer.write(b"not json\n")
            await writer.drain()
            self.assertIn("error:", (await reader.readline()).decode())
            self.assertFalse(json.loads(await reader.readline())["ok"])
            writer.write(json.dumps({"cmd": "badcmd"}).encode() + b"\n")
            await writer.drain()
            self.assertIn("error:", (await reader.readline()).decode())
            self.assertFalse(json.loads(await reader.readline())["ok"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_escaped_long_line_fits_the_client_reader(self) -> None:
        # every quote doubles once JSON-escaped, so the raw cap alone overflows the packet
        self.manager._command_progress = lambda: self.manager.print('"' * 16000)
        output: list[str] = []
        self.assertEqual(await send_command(self.data_dir, "progress", output=output.append), 0)
        self.assertTrue(output)
        self.assertTrue(set(output[0]) <= {'"'})

    async def test_watch_stop_and_disconnect(self) -> None:
        self.manager._command_progress = lambda: self.manager.print("tick")
        reader, writer = await open_control(self.data_dir)
        writer.write(b'{"cmd":"watch 1"}\n')
        await writer.drain()
        self.assertIn("tick", (await reader.readline()).decode())
        self.assertIn("tick", (await asyncio.wait_for(reader.readline(), 2)).decode())
        writer.write(b'{"cmd":"stop"}\n')
        await writer.drain()
        self.assertTrue(json.loads(await reader.readline())["done"])
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        self.assertFalse(self.manager._remote_watches)

    async def test_remote_logout_requires_confirmation(self) -> None:
        self.manager._logout = AsyncMock()
        self.assertEqual(await send_command(self.data_dir, "logout", output=lambda _: None), 1)
        self.manager._logout.assert_not_awaited()
        self.assertEqual(await send_command(self.data_dir, "logout", confirm=True, output=lambda _: None), 0)
        self.manager._logout.assert_awaited_once()

    async def test_tcp_token_file_and_wrong_token(self) -> None:
        await self.server.stop()
        self.server = ControlServer(self.manager, self.data_dir, tcp=True)
        await self.server.start()
        self.assertEqual(self.server.path.stat().st_mode & 0o777, 0o600)
        info = json.loads(self.server.path.read_text())
        reader, writer = await asyncio.open_connection("127.0.0.1", info["port"])
        writer.write(b'{"auth":"wrong"}\n{"cmd":"help"}\n')
        await writer.drain()
        self.assertFalse(json.loads(await reader.readline())["ok"])
        writer.close()
        await writer.wait_closed()
        self.assertEqual(await send_command(self.data_dir, "help", tcp=True, output=lambda _: None), 0)


class RemoteDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = CLIManager(FakeTwitch())

    async def asyncTearDown(self) -> None:
        self.manager.close_window()
        await asyncio.sleep(0)

    async def test_output_capture_without_redirecting_console(self) -> None:
        output: list[str] = []
        console = io.StringIO()
        with redirect_stdout(console):
            self.assertTrue(await self.manager.dispatch_command("help", writer=output.append))
            self.manager.print("local message")
        self.assertTrue(any("commands:" in line for line in output))
        self.assertNotIn("commands:", console.getvalue())
        self.assertIn("local message", console.getvalue())

    async def test_remote_logout_confirm_watch_and_redaction(self) -> None:
        output: list[str] = []
        self.manager._logout = AsyncMock(return_value=True)
        self.assertFalse(await self.manager.dispatch_command("logout", writer=output.append))
        self.manager._logout.assert_not_awaited()
        self.assertTrue(await self.manager.dispatch_command("logout", writer=output.append, confirm=True))
        self.manager._logout.assert_awaited_once()
        self.manager._command_progress = lambda: self.manager.print("tick")
        self.assertTrue(await self.manager.dispatch_command("watch 1", writer=output.append))
        self.assertIn("tick", output[-1])
        await asyncio.sleep(1.05)
        self.assertIn("tick", output[-1])
        self.manager.stop_remote_watch(output.append)
        self.assertFalse(self.manager._remote_watches)
        self.manager._command_progress = lambda: self.manager.print("Enter this code: SECRET")
        self.assertTrue(await self.manager.dispatch_command("progress", writer=output.append))
        self.assertNotIn("SECRET", output[-1])
        self.assertIn("<redacted>", output[-1])


class ControlSubprocessTests(unittest.TestCase):
    def test_ctl_without_running_miner_exits_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "TDM_DATA_DIR": directory}
            for arguments in (("ctl", "status"), ("ctl",)):
                result = subprocess.run(
                    [sys.executable, "main.py", "cli", *arguments],
                    cwd=Path(__file__).resolve().parents[1], env=env,
                    input="quit\n", capture_output=True, text=True, timeout=8,
                )
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("no running miner control endpoint", result.stderr)

    def test_offline_command_with_lock_but_no_control_exits_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "TDM_DATA_DIR": directory}
            locker = subprocess.Popen(
                [sys.executable, "-u", "-c", """
import sys
from pathlib import Path
from utils import lock_file
locked, handle = lock_file(Path(sys.argv[1]))
assert locked
print("READY", flush=True)
sys.stdin.readline()
handle.close()
""", str(Path(directory) / "lock.file")],
                cwd=Path(__file__).resolve().parents[1], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(locker.stdout.readline().strip(), "READY")
                result = subprocess.run(
                    [sys.executable, "main.py", "cli", "priority", "add", "X"],
                    cwd=Path(__file__).resolve().parents[1], env=env,
                    capture_output=True, text=True, timeout=8,
                )
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("without a control endpoint", result.stderr)
            finally:
                locker.communicate("\n", timeout=5)

    @unittest.skipIf(sys.platform == "win32", "POSIX SIGINT test")
    def test_cli_ctl_and_offline_forwarding(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are forbidden in this sandbox")

        class DeviceHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps({
                    "device_code": "local-device-code", "user_code": "LOCAL123",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "interval": 1, "expires_in": 1800,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), DeviceHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/oauth2/device"
            code = f'''
import runpy
import sys
from contextlib import asynccontextmanager
for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None
import twitch
from exceptions import ExitRequest
original_init = twitch.Twitch.__init__
original_request = twitch.Twitch.request
def local_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self._auth_state.device_id = "local-device-id"
@asynccontextmanager
async def local_request(self, method, url, **kwargs):
    if method == "POST" and str(url) == "https://id.twitch.tv/oauth2/token":
        await self.gui._close_requested.wait()
        raise ExitRequest()
    if method != "POST" or str(url) != "https://id.twitch.tv/oauth2/device":
        raise AssertionError("Unexpected external request")
    async with original_request(self, method, {endpoint!r}, **kwargs) as response:
        yield response
twitch.Twitch.__init__ = local_init
twitch.Twitch.request = local_request
sys.argv = ["main.py", "cli", "run"]
runpy.run_path("main.py", run_name="__main__")
'''
            env = os.environ.copy()
            env["TDM_DATA_DIR"] = str(data_dir)
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code], env=env, cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while not (data_dir / "control.sock").exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(f"miner exited early: {process.stdout.read()}")
                    time.sleep(0.05)
                self.assertTrue((data_dir / "control.sock").exists())

                def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [sys.executable, "main.py", "cli", *arguments], env=env,
                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=8,
                    )

                status = cli("ctl", "status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn("state:", status.stdout)
                filters = cli("ctl", "filters")
                self.assertEqual(filters.returncode, 0, filters.stderr)
                self.assertIn("not_linked", filters.stdout)
                added = cli("priority", "add", "X")
                self.assertEqual(added.returncode, 0, added.stderr)
                listed = cli("ctl", "priority", "list")
                self.assertIn("X", listed.stdout)
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    output, _ = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate()
                    self.fail(f"miner did not stop: {output[-1000:]}")
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(process.returncode, 0, output[-1000:])
            self.assertFalse((data_dir / "control.sock").exists())


class WindowsAclTests(unittest.TestCase):
    def test_acl_goes_to_the_process_sid_and_fails_closed(self) -> None:
        from unittest.mock import patch
        import private_file

        def fake_run(command, **kwargs):
            if command[0] == "whoami":
                return SimpleNamespace(returncode=0, stdout='"home\\alice","S-1-5-21-1-2-3-1001"\n')
            return SimpleNamespace(returncode=0, stdout="")

        with patch.object(private_file, "WINDOWS", True), \
                patch("private_file.subprocess.run", side_effect=fake_run) as run:
            private_file.restrict_to_owner(Path("secret.json"))
            # no /reset: that would pass through the inherited (possibly wider) ACL
            self.assertEqual(len(run.call_args_list), 2)
            self.assertEqual(run.call_args_list[1].args[0],
                             ["icacls", "secret.json", "/inheritance:r", "/grant:r", "*S-1-5-21-1-2-3-1001:F"])
            run.side_effect = lambda command, **kwargs: SimpleNamespace(returncode=5, stdout="")
            with self.assertRaises(OSError):
                private_file.restrict_to_owner(Path("secret.json"))
        with patch("private_file.subprocess.run") as run:
            private_file.restrict_to_owner(Path("secret.json"))
            run.assert_not_called()


class RewritePrivateTests(unittest.TestCase):
    def test_rewrite_uses_a_fresh_restricted_file_and_keeps_original_on_failure(self) -> None:
        import tempfile
        from unittest.mock import patch
        import private_file
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dashboard.token"
            target.write_text("old\n")
            original_inode = target.stat().st_ino
            with patch.object(private_file, "restrict_to_owner") as restrict:
                private_file.rewrite_private(target, "token\n")
            self.assertNotEqual(restrict.call_args.args[0], target)
            self.assertEqual(target.read_text(), "token\n")
            self.assertNotEqual(target.stat().st_ino, original_inode)
            with patch.object(private_file, "restrict_to_owner", side_effect=OSError("icacls failed")):
                with self.assertRaises(OSError):
                    private_file.rewrite_private(target, "other\n")
            self.assertEqual(target.read_text(), "token\n")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["dashboard.token"])


class WindowsAclStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_acl_failure_closes_and_removes_control_file(self) -> None:
        import tempfile
        from unittest.mock import patch
        import control
        with tempfile.TemporaryDirectory() as directory:
            server = control.ControlServer(SimpleNamespace(), Path(directory), tcp=True)
            with patch.object(control, "_WINDOWS", True), \
                    patch.object(control, "_restrict_windows_acl", side_effect=OSError("icacls failed")), \
                    patch.object(control.os, "close", wraps=os.close) as close:
                with self.assertRaises(OSError):
                    await server.start()
            close.assert_called_once()
            self.assertFalse((Path(directory) / "control.json").exists())
