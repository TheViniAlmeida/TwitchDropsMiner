from __future__ import annotations

import os
import json
import asyncio
import re
import socket
from aiohttp import ClientSession
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def run_python(*args: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["TDM_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [str(PYTHON), *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class CLIImportTests(unittest.TestCase):
    def test_rejected_device_login_exits_without_traceback(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are not allowed here: PermissionError")

        class DeviceHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/oauth2/device":
                    self.send_error(404)
                    return
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"status":400,"message":"invalid client"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), DeviceHandler)
            except PermissionError:
                self.skipTest("loopback sockets are not allowed here: PermissionError")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/oauth2/device"
            code = f"""
import runpy
import sys
from contextlib import asynccontextmanager
for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None
import twitch
original_init = twitch.Twitch.__init__
original_request = twitch.Twitch.request
def local_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self._auth_state.device_id = "local-device-id"
@asynccontextmanager
async def local_request(self, method, url, **kwargs):
    if method != "POST" or str(url) != "https://id.twitch.tv/oauth2/device":
        raise AssertionError("Unexpected external request")
    async with original_request(self, method, {endpoint!r}, **kwargs) as response:
        yield response
twitch.Twitch.__init__ = local_init
twitch.Twitch.request = local_request
sys.argv = ["main.py", "cli", "run"]
runpy.run_path("main.py", run_name="__main__")
"""
            environment = os.environ.copy()
            environment["TDM_DATA_DIR"] = directory
            try:
                result = subprocess.run(
                    [str(PYTHON), "-u", "-c", code], cwd=ROOT, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "Twitch rejected the device login for client ANDROID_APP: 400 invalid client",
            result.stdout,
        )
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("KeyError", result.stdout)

    @unittest.skipIf(sys.platform == "win32", "POSIX SIGINT subprocess test")
    def test_dashboard_websocket_sigint_and_no_token_leak(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are not allowed here: PermissionError")

        class DeviceHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/oauth2/device":
                    self.send_error(404)
                    return
                self.rfile.read(int(self.headers["Content-Length"]))
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
            code = f"""
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
sys.argv = ["main.py", "cli", "run", "--dashboard", "--dashboard-host", "127.0.0.1", "--dashboard-port", "0"]
runpy.run_path("main.py", run_name="__main__")
"""
            environment = os.environ.copy()
            environment["TDM_DATA_DIR"] = str(data_dir)
            environment["TDM_DASHBOARD_TOKEN"] = "private-test-token"
            process = subprocess.Popen(
                [str(PYTHON), "-u", "-c", code], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            lines: queue.Queue[str] = queue.Queue()
            assert process.stdout is not None
            reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout],
                                      daemon=True)
            reader.start()
            output = []
            try:
                deadline = time.monotonic() + 8
                url = None
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    output.append(line)
                    match = re.search(r"Dashboard: (http://127\.0\.0\.1:\d+/)", line)
                    if match:
                        url = match.group(1)
                        break
                self.assertIsNotNone(url, "".join(output))

                async def connect_and_stop():
                    async with ClientSession() as session:
                        async with session.ws_connect(url + "api/ws") as ws:
                            await ws.send_json({"auth": "private-test-token"})
                            message = await asyncio.wait_for(ws.receive_json(), 2)
                            self.assertEqual(message["type"], "state")
                            self.assertNotIn("private-test-token", json.dumps(message))
                            process.send_signal(signal.SIGINT)
                            return await asyncio.to_thread(process.wait, 8)

                self.assertEqual(asyncio.run(connect_and_stop()), 0)
                reader.join(timeout=1)
                while not lines.empty():
                    output.append(lines.get_nowait())
                self.assertNotIn("private-test-token", "".join(output))
                log_file = data_dir / "log.txt"
                if log_file.exists():
                    self.assertNotIn("private-test-token", log_file.read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    @unittest.skipIf(sys.platform == "win32", "POSIX SIGINT subprocess test")
    def test_cli_run_without_tk_closes_cleanly_on_sigint(self) -> None:
        activation_url = "https://www.twitch.tv/activate?device-code=LOCAL123"
        user_code = "LOCAL123"

        class DeviceHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/oauth2/device":
                    self.send_error(404)
                    return
                self.rfile.read(int(self.headers["Content-Length"]))
                response = json.dumps({
                    "device_code": "local-device-code",
                    "user_code": user_code,
                    "verification_uri": activation_url,
                    "interval": 1,
                    "expires_in": 1800,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), DeviceHandler)
            except PermissionError:
                # Restricted sandboxes can forbid even loopback sockets.
                self.skipTest("loopback sockets are not allowed here")
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            device_endpoint = f"http://127.0.0.1:{server.server_port}/oauth2/device"
            code = f"""
import runpy
import sys
from contextlib import asynccontextmanager

# Block GUI modules before anything imports the app code.
for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None

import twitch
from exceptions import ExitRequest

# Test-only bootstrap: use a local device endpoint and reject every other request.
original_init = twitch.Twitch.__init__
original_request = twitch.Twitch.request

def local_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self._auth_state.device_id = "local-device-id"

@asynccontextmanager
async def local_request(self, method, url, **kwargs):
    if method == "POST" and str(url) == "https://id.twitch.tv/oauth2/token":
        await self.gui.wait_until_closed()
        raise ExitRequest()
    if method != "POST" or str(url) != "https://id.twitch.tv/oauth2/device":
        raise AssertionError("Unexpected external request")
    async with original_request(self, method, {device_endpoint!r}, **kwargs) as response:
        yield response

twitch.Twitch.__init__ = local_init
twitch.Twitch.request = local_request

sys.argv = ["main.py", "-vv", "cli", "run"]
runpy.run_path("main.py", run_name="__main__")
"""
            environment = os.environ.copy()
            environment["TDM_DATA_DIR"] = str(data_dir)
            process = subprocess.Popen(
                [str(PYTHON), "-u", "-c", code],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            lines: queue.Queue[str] = queue.Queue()
            assert process.stdout is not None
            reader = threading.Thread(
                target=lambda: [lines.put(line) for line in process.stdout], daemon=True
            )
            reader.start()
            output: list[str] = []
            try:
                deadline = time.monotonic() + 8
                saw_url = False
                saw_code = False
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    output.append(line)
                    saw_url |= activation_url in line
                    saw_code |= f"Enter this code: {user_code}" in line
                    if saw_url and saw_code:
                        break
                else:
                    self.fail("CLI did not show device activation: " + "".join(output[-20:]))
                self.assertTrue(saw_url and saw_code, "".join(output[-20:]))
                self.assertIsNone(process.poll(), "".join(output[-20:]))
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=5), 0, "".join(output[-20:]))
                reader.join(timeout=1)
                while not lines.empty():
                    output.append(lines.get_nowait())
                self.assertNotIn("ImportError", "".join(output))
                self.assertNotIn("Traceback", "".join(output))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=1)

    def test_cli_without_subcommand_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_python("main.py", "cli", data_dir=Path(directory))
        self.assertEqual(result.returncode, 2, result.stderr)
