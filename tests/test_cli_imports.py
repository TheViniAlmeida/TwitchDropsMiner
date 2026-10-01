from __future__ import annotations

import os
import json
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
PYTHON = ROOT / ".venv" / "bin" / "python"


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

for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None
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
