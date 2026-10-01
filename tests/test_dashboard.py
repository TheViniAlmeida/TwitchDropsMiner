from __future__ import annotations

import argparse
import asyncio
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, WSCloseCode, web
from aiohttp.test_utils import make_mocked_request

from cli_actions import Actions, ActionRejected
from cli_commands import CommandError
from constants import State
from dashboard import Dashboard, DashboardConfig, DashboardError, is_loopback, resolve_dashboard_config


class ConfigTests(unittest.TestCase):
    def test_origins_config_validation(self) -> None:
        args = argparse.Namespace(dashboard=False)
        config = resolve_dashboard_config(args, {
            "TDM_DASHBOARD_ORIGINS": "https://drops.example.com, http://localhost:8787"
        })
        self.assertEqual(config.origins, ("https://drops.example.com", "http://localhost:8787"))
        for invalid in ("https://drops.example.com/path", "https://drops.example.com/",
                        "ftp://drops.example.com", "https://user@drops.example.com",
                        "https://drops.example.com:99999", "https://drops.example.com?x=1",
                        "https://drops.example.com,", "https://drops.example.com,,http://ok.test",
                        "https://-", "https://.", "https://a..b"):
            with self.subTest(invalid=invalid), self.assertRaises(argparse.ArgumentError):
                resolve_dashboard_config(args, {"TDM_DASHBOARD_ORIGINS": invalid})

    def test_defaults_and_env_flags(self) -> None:
        args = argparse.Namespace(dashboard=False, dashboard_host=None, dashboard_port=None,
                                  dashboard_readonly=False)
        config = resolve_dashboard_config(args, {})
        self.assertEqual((config.enabled, config.host, config.port, config.readonly, config.token),
                         (False, "127.0.0.1", 8787, False, None))
        with tempfile.TemporaryDirectory() as directory, patch("dashboard.DATA_DIR", Path(directory)):
            args.dashboard = True
            args.dashboard_host = "127.0.0.1"
            args.dashboard_port = "0"
            args.dashboard_readonly = True
            config = resolve_dashboard_config(args, {"TDM_DASHBOARD_HOST": "0.0.0.0",
                                                     "TDM_DASHBOARD_PORT": "9000",
                                                     "TDM_DASHBOARD_TOKEN": "from-env"})
            self.assertEqual((config.host, config.port, config.readonly, config.token),
                             ("127.0.0.1", 0, True, "from-env"))

    def test_invalid_port_and_loopback(self) -> None:
        args = argparse.Namespace(dashboard_port=None)
        with self.assertRaises(argparse.ArgumentError):
            resolve_dashboard_config(args, {"TDM_DASHBOARD_PORT": "nope"})
        for host in ("localhost", "127.0.0.1", "::1"):
            self.assertTrue(is_loopback(host))
        for host in ("0.0.0.0", "::", "192.168.1.1", "example.test"):
            self.assertFalse(is_loopback(host))
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "TDM_DATA_DIR": directory}
            result = subprocess.run([sys.executable, "main.py", "cli", "run",
                                     "--dashboard-port", "nope"], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid dashboard port", result.stderr)

    def test_exposed_config_creates_private_token_and_prints_path_only(self) -> None:
        args = argparse.Namespace(dashboard=True, dashboard_host="0.0.0.0", dashboard_port=0,
                                  dashboard_readonly=False)
        with tempfile.TemporaryDirectory() as directory, patch("dashboard.DATA_DIR", Path(directory)):
            output = io.StringIO()
            with redirect_stdout(output):
                config = resolve_dashboard_config(args, {})
            self.assertEqual(config.token_file, Path(directory) / "dashboard.token")
            self.assertTrue(config.token)
            self.assertNotIn(config.token, output.getvalue())
            self.assertEqual(config.token_file.read_text().strip(), config.token)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)
            self.assertEqual(resolve_dashboard_config(args, {}).token, config.token)
            if os.name == "posix":
                config.token_file.chmod(0o644)
                with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
                    self.assertEqual(resolve_dashboard_config(args, {}).token, config.token)
                self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)

    def test_cli_run_without_dashboard_does_not_start_listener(self) -> None:
        code = """
import runpy, sys
import dashboard, twitch
async def forbidden_start(self):
    raise AssertionError("dashboard started without flag")
async def fake_run(self):
    self.gui.close()
dashboard.Dashboard.start = forbidden_start
twitch.Twitch.run = fake_run
sys.argv = ["main.py", "cli", "run"]
runpy.run_path("main.py", run_name="__main__")
"""
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "TDM_DATA_DIR": directory}
            for key in ("TDM_DASHBOARD", "TDM_DASHBOARD_HOST", "TDM_DASHBOARD_PORT",
                        "TDM_DASHBOARD_READONLY", "TDM_DASHBOARD_TOKEN"):
                env.pop(key, None)
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Dashboard:", result.stdout)

    def test_cli_dashboard_bind_error_exits_before_miner(self) -> None:
        code = """
import runpy, sys
import dashboard, twitch
async def failed_start(self):
    raise dashboard.DashboardError("Dashboard cannot bind: port in use")
async def forbidden_run(self):
    raise AssertionError("miner started after dashboard bind failed")
dashboard.Dashboard.start = failed_start
twitch.Twitch.run = forbidden_run
sys.argv = ["main.py", "cli", "run", "--dashboard", "--dashboard-port", "0"]
runpy.run_path("main.py", run_name="__main__")
"""
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "TDM_DATA_DIR": directory}
            env["TDM_DASHBOARD_HOST"] = "127.0.0.1"
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("Dashboard cannot bind: port in use", result.stdout)
            self.assertNotIn("miner started", result.stdout + result.stderr)

    def test_dashboard_error_handler_is_guarded_by_cli_mode(self) -> None:
        module = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        main = next(node for node in ast.walk(module)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
        handlers = [node for node in ast.walk(main)
                    if isinstance(node, ast.ExceptHandler)
                    and isinstance(node.type, ast.Name) and node.type.id == "DashboardError"]
        guards = [node for node in ast.walk(main)
                  if isinstance(node, ast.If) and ast.unparse(node.test) == "cli_mode and dashboard_config.enabled"]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(len(guards), 1)
        self.assertTrue(any(node is handlers[0] for node in ast.walk(guards[0])))

    def test_dashboard_stop_failure_or_timeout_still_shuts_down_client(self) -> None:
        code = """
import asyncio, runpy, sys
import dashboard, twitch
async def fake_start(self):
    pass
async def fake_stop(self):
    if sys.argv[-1] == "raise":
        raise RuntimeError("close failed")
    await asyncio.Event().wait()
async def fake_run(self):
    self.gui.close()
async def tracked_shutdown(self):
    print("CLIENT_SHUTDOWN_REACHED")
dashboard.Dashboard.start = fake_start
dashboard.Dashboard.stop = fake_stop
twitch.Twitch.run = fake_run
twitch.Twitch.shutdown = tracked_shutdown
mode = sys.argv[-1]
sys.argv = ["main.py", "cli", "run", "--dashboard"]
runpy.run_path("main.py", run_name="__main__")
"""
        for mode in ("raise", "slow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, "TDM_DATA_DIR": directory}
                result = subprocess.run([sys.executable, "-c", code, mode], env=env,
                                        capture_output=True, text=True, check=False, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CLIENT_SHUTDOWN_REACHED", result.stdout)


class FakeManager:
    def __init__(self) -> None:
        self.channels = SimpleNamespace(_watching=None, _channels={})
        self.websockets = SimpleNamespace(_states={})
        self.inv = SimpleNamespace(_campaigns={})
        self.progress = SimpleNamespace(seconds=0)
        self._current_drop = None
        self._logged_in = False
        self._games = set()
        self.listeners = set()
        self.lines = []

    def print(self, message):
        self.lines.append(message)
        print(message)

    def subscribe(self, listener):
        self.listeners.add(listener)

    def unsubscribe(self, listener):
        self.listeners.discard(listener)

    def emit(self, event, value=None):
        for listener in tuple(self.listeners):
            listener(event, value)

    def log_tail(self, count=100):
        return self.lines[-count:] if count else []


class FakeTwitch:
    def __init__(self) -> None:
        self._state = State.IDLE
        self.channels = {}
        self.transitions = []
        self.settings = SimpleNamespace(proxy="", language="English", connection_quality=1,
                                        priority_mode=SimpleNamespace(name="PRIORITY"),
                                        enable_badges_emotes=False, available_drops_check=False,
                                        tray_notifications=False, priority=[], exclude=set())

    def state_change(self, state):
        self.transitions.append(state)
        return lambda: None


class DashboardMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = FakeManager()
        self.twitch = FakeTwitch()
        self.manager.actions = Actions(self.twitch, self.manager)
        self.dashboard = Dashboard(self.manager, self.twitch,
                                   DashboardConfig(port=8787, token="private-token",
                                                   readonly=True, enabled=True))

    async def test_static_routes_without_sockets(self) -> None:
        async def call_static(path):
            request = make_mocked_request("GET", path, headers={"Host": "127.0.0.1:8787"},
                                          app=self.dashboard.app)
            request._match_info = await self.dashboard.app.router.resolve(request)
            handler = request.match_info.handler
            for middleware in reversed(self.dashboard.app.middlewares):
                previous = handler

                async def wrapped(req, middleware=middleware, previous=previous):
                    return await middleware(req, previous)

                handler = wrapped
            return await handler(request)

        for path in ("/", "/static/app.js", "/static/style.css"):
            with self.subTest(path=path):
                response = await call_static(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Security-Policy"],
                                 "default-src 'self'; img-src 'self' https://static-cdn.jtvnw.net; connect-src 'self'")
        for path in ("/static/../x", "/static/.secret"):
            with self.subTest(path=path):
                self.assertEqual((await call_static(path)).status, 404)

    async def call(self, method="GET", path="/api/state", headers=None):
        request = make_mocked_request(method, path, headers={"Host": "127.0.0.1:8787", **(headers or {})},
                                      app=self.dashboard.app)

        async def handler(req):
            if req.method == "GET":
                return await self.dashboard._get(req)
            return web.json_response({"ok": True})

        for middleware in reversed(self.dashboard.app.middlewares):
            previous = handler

            async def wrapped(req, middleware=middleware, previous=previous):
                return await middleware(req, previous)

            handler = wrapped
        return await handler(request)

    async def test_headers_host_origin_auth_rate_and_readonly(self) -> None:
        response = await self.call()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for route in ("state", "channels", "inventory", "games", "settings", "logs",
                      "priority", "exclude"):
            response = await self.call(path="/api/" + route,
                                       headers={"Authorization": "Bearer wrong"})
            self.assertIn(response.status, (401, 429))
        self.assertEqual((await self.call()).status, 429)
        token = {"Authorization": "Bearer private-token"}
        self.assertEqual((await self.call(headers=token)).status, 200)
        self.dashboard._failures.clear()
        self.assertEqual((await self.call(headers=token)).status, 200)
        self.assertEqual((await self.call(headers={**token,
            "Origin": "https://127.0.0.1:8787"})).status, 200)
        self.assertTrue(json.loads((await self.call(headers=token)).text)["readonly"])
        self.twitch.settings.priority = ["Game A"]
        self.twitch.settings.exclude = {"Game B"}
        self.assertEqual(json.loads((await self.call(path="/api/priority", headers=token)).text),
                         {"priority": ["Game A"]})
        self.assertEqual(json.loads((await self.call(path="/api/exclude", headers=token)).text),
                         {"exclude": ["Game B"]})
        self.assertEqual((await self.call(headers={**token, "Host": "evil.example"})).status, 403)
        self.assertEqual((await self.call(headers={**token, "Origin": "http://evil.example"})).status, 403)
        self.dashboard.config = DashboardConfig(port=8787, token="private-token", readonly=True,
                                                origins=("https://drops.example.com",))
        self.assertEqual((await self.call(headers={**token,
            "Origin": "https://drops.example.com"})).status, 200)
        self.assertEqual((await self.call(headers={**token,
            "Origin": "https://other.example.com"})).status, 403)
        self.assertEqual((await self.call(path="/api/ws", headers={"Origin": "http://evil.example"})).status, 403)
        self.assertEqual((await self.call(method="POST", path="/api/reload",
                                          headers={**token, "Content-Type": "text/plain"})).status, 403)
        self.assertEqual((await self.call(method="POST", path="/api/reload",
                                          headers={**token, "Content-Type": "application/json"})).status, 403)
        self.assertEqual((await self.call(method="POST", path="/api/reload",
                                          headers={"Content-Type": "application/json"})).status, 401)
        self.dashboard.config = DashboardConfig(port=8787, token="private-token", readonly=False,
                                                enabled=True)
        self.assertEqual((await self.call(method="POST", path="/api/reload",
                                          headers={**token, "Content-Type": "text/plain"})).status, 415)

    async def test_action_error_statuses_and_token_redaction(self) -> None:
        request = make_mocked_request("GET", "/api/state", app=self.dashboard.app)

        async def rejected(_request):
            raise ActionRejected("private-token declined")

        async def invalid(_request):
            raise CommandError("bad value")

        conflict = await self.dashboard._errors(request, rejected)
        self.assertEqual(conflict.status, 409)
        self.assertEqual(json.loads(conflict.text), {"error": "<redacted> declined"})
        bad = await self.dashboard._errors(request, invalid)
        self.assertEqual(bad.status, 400)

        class BadJsonRequest:
            path = "/api/reload"

            async def json(self):
                raise json.JSONDecodeError("bad", "{", 1)

        malformed = await self.dashboard._errors(BadJsonRequest(), self.dashboard._post)
        self.assertEqual(malformed.status, 400)

    async def test_exposed_start_prints_path_and_warning_without_token(self) -> None:
        class FakeSocket:
            def getsockname(self):
                return ("0.0.0.0", 49152)

        class FakeRunner:
            def __init__(self, *args, **kwargs):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                pass

        class FakeSite:
            def __init__(self, *args, **kwargs):
                self._server = SimpleNamespace(sockets=[FakeSocket()])

            async def start(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.token"
            dashboard = Dashboard(self.manager, self.twitch,
                                  DashboardConfig(host="0.0.0.0", port=0,
                                                  token="private-token", token_file=path,
                                                  enabled=True))
            output = io.StringIO()
            with patch("dashboard.web.AppRunner", FakeRunner), patch("dashboard.web.TCPSite", FakeSite), \
                 patch("dashboard._local_ips", return_value=["192.0.2.1"]), redirect_stdout(output):
                await dashboard.start()
                await dashboard.stop()
            self.assertIn("Dashboard: http://0.0.0.0:49152/", output.getvalue())
            self.assertIn(str(path), output.getvalue())
            self.assertIn("192.0.2.1", output.getvalue())
            self.assertIn("exposed without TLS", output.getvalue())
            self.assertNotIn("private-token", output.getvalue())
            self.assertEqual(len(self.manager.lines), 4)
            self.assertTrue(self.manager.lines[0].startswith("Dashboard: "))
            self.assertTrue(all("private-token" not in line for line in self.manager.lines))

    async def test_rest_mutations_share_action_state_changes(self) -> None:
        class Request:
            def __init__(self, path, body):
                self.path = path
                self.body = body

            async def json(self):
                return self.body

        response = await self.dashboard._post(Request("/api/reload", {}))
        self.assertEqual(json.loads(response.text), {"requested": True})
        channel = SimpleNamespace(id=5, name="streamer")
        self.twitch.channels[5] = channel
        selected = []
        self.manager.channels.select = selected.append
        response = await self.dashboard._post(Request("/api/switch", {"channel": "streamer"}))
        self.assertEqual(json.loads(response.text), {"channel": "streamer"})
        self.assertEqual(selected, [channel])
        self.assertEqual(self.twitch.transitions, [State.INVENTORY_FETCH, State.CHANNEL_SWITCH])

    async def test_log_response_redacts_dashboard_token(self) -> None:
        self.manager.lines = ["private-token in a log line"]
        request = make_mocked_request("GET", "/api/logs?tail=1", app=self.dashboard.app)
        response = await self.dashboard._get(request)
        self.assertEqual(json.loads(response.text), ["<redacted> in a log line"])

    async def test_stuck_websocket_close_does_not_block_cleanup(self) -> None:
        closed = []

        class StuckSocket:
            async def close(self, **kwargs):
                await asyncio.Event().wait()

        class QuickSocket:
            async def close(self, **kwargs):
                closed.append(kwargs["code"])

        class Runner:
            async def cleanup(self):
                closed.append("cleanup")

        self.dashboard._sockets = {StuckSocket(), QuickSocket()}
        self.dashboard._runner = Runner()
        start = asyncio.get_running_loop().time()
        await asyncio.wait_for(self.dashboard.stop(), timeout=2.5)
        self.assertLess(asyncio.get_running_loop().time() - start, 2.5)
        self.assertIn(WSCloseCode.GOING_AWAY, closed)
        self.assertIn("cleanup", closed)


class DashboardSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_files_headers_and_denied_paths(self) -> None:
        for path in ("/", "/static/app.js", "/static/style.css"):
            with self.subTest(path=path):
                async with self.session.get(self.base + path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Security-Policy"),
                                     "default-src 'self'; img-src 'self' https://static-cdn.jtvnw.net; connect-src 'self'")
        for path in ("/static/%2e%2e/x", "/static/.secret"):
            with self.subTest(path=path):
                async with self.session.get(self.base + path) as response:
                    self.assertEqual(response.status, 404)

    async def asyncSetUp(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are not allowed here: PermissionError")
        self.manager = FakeManager()
        self.twitch = FakeTwitch()
        self.manager.actions = Actions(self.twitch, self.manager)
        self.dashboard = Dashboard(self.manager, self.twitch,
                                   DashboardConfig(port=0, enabled=True, auth_timeout=0.05))
        try:
            await self.dashboard.start()
        except DashboardError as exc:
            if isinstance(exc.__cause__, PermissionError):
                self.skipTest("loopback sockets are not allowed here: PermissionError")
            raise
        self.base = f"http://127.0.0.1:{self.dashboard.port}"
        self.session = ClientSession()

    async def asyncTearDown(self) -> None:
        if hasattr(self, "session"):
            await self.session.close()
        if hasattr(self, "dashboard"):
            await self.dashboard.stop()

    async def test_security_actions_and_errors(self) -> None:
        async with self.session.get(self.base + "/api/state") as response:
            self.assertEqual(response.status, 200)
            self.assertFalse((await response.json())["readonly"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        async with self.session.get(self.base + "/api/state", headers={"Host": "evil.example"}) as response:
            self.assertEqual(response.status, 403)
        async with self.session.get(self.base + "/api/state", headers={"Origin": "http://evil.example"}) as response:
            self.assertEqual(response.status, 403)
        async with self.session.post(self.base + "/api/reload", data="{}", headers={"Content-Type": "text/plain"}) as response:
            self.assertEqual(response.status, 415)
        async with self.session.post(self.base + "/api/reload", data="{", headers={"Content-Type": "application/json"}) as response:
            self.assertEqual(response.status, 400)
        async with self.session.post(self.base + "/api/reload", json={}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.twitch.transitions, [State.INVENTORY_FETCH])
        channel = SimpleNamespace(id=1, name="streamer", online=True, game=None,
                                  viewers=10, drops_enabled=True, acl_based=False)
        self.twitch.channels[1] = channel
        selected = []
        self.manager.channels.select = selected.append
        async with self.session.post(self.base + "/api/switch", json={"channel": "streamer"}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(selected, [channel])
        self.assertEqual(self.twitch.transitions[-1], State.CHANNEL_SWITCH)
        async with self.session.post(self.base + "/api/switch", json={"channel": "missing"}) as response:
            self.assertEqual(response.status, 400)
        self.manager.actions.reload = lambda: (_ for _ in ()).throw(ActionRejected("busy"))
        async with self.session.post(self.base + "/api/reload", json={}) as response:
            self.assertEqual(response.status, 409)

    async def test_auth_rate_limit_and_readonly(self) -> None:
        await self.dashboard.stop()
        self.dashboard = Dashboard(self.manager, self.twitch,
                                   DashboardConfig(port=0, token="private-token", readonly=True,
                                                   enabled=True, auth_timeout=0.05))
        await self.dashboard.start()
        self.base = f"http://127.0.0.1:{self.dashboard.port}"
        routes = ("state", "channels", "inventory", "games", "settings", "logs",
                  "priority", "exclude")
        for route in routes:
            self.dashboard._failures.clear()
            async with self.session.get(self.base + "/api/" + route) as response:
                self.assertEqual(response.status, 401, route)
        self.dashboard._failures.clear()
        for attempt in range(5):
            async with self.session.get(
                self.base + "/api/state", headers={"Authorization": "Bearer wrong-token"}
            ) as response:
                self.assertEqual(response.status, 401, attempt)
        # the 6th wrong attempt within a minute is rate limited
        async with self.session.get(
            self.base + "/api/state", headers={"Authorization": "Bearer wrong-token"}
        ) as response:
            self.assertEqual(response.status, 429)
        async with self.session.get(self.base + "/api/state", headers={"Authorization": "Bearer private-token"}) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["readonly"])
        async with self.session.post(self.base + "/api/reload", json={},
                                     headers={"Authorization": "Bearer private-token"}) as response:
            self.assertEqual(response.status, 403)
        async with self.session.get(self.base + "/api/state") as response:
            self.assertNotIn("private-token", await response.text())

    async def test_websocket_state_log_throttle_and_origin(self) -> None:
        with self.assertRaises(Exception):
            await self.session.ws_connect(self.base + "/api/ws", headers={"Origin": "http://evil.example"})
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            self.assertEqual((await ws.receive_json())["type"], "state")
            self.manager.emit("log", "hello")
            self.assertEqual(await ws.receive_json(), {"type": "log", "line": "hello"})
            self.manager._logged_in = True
            self.manager.emit("change")
            self.manager._logged_in = False
            self.manager.emit("change")
            message = await asyncio.wait_for(ws.receive_json(), 2)
            self.assertEqual(message["type"], "state")
            self.assertFalse(message["logged_in"])
        self.assertFalse(self.manager.listeners)

    async def test_websocket_auth_and_port_in_use(self) -> None:
        await self.dashboard.stop()
        self.dashboard = Dashboard(self.manager, self.twitch,
                                   DashboardConfig(port=0, token="private-token", enabled=True,
                                                   auth_timeout=0.05))
        await self.dashboard.start()
        self.base = f"http://127.0.0.1:{self.dashboard.port}"
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            await ws.receive()
            self.assertEqual(ws.close_code, WSCloseCode.POLICY_VIOLATION)
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            await ws.send_json({"auth": "private-token"})
            self.assertEqual((await ws.receive_json())["type"], "state")
        other = Dashboard(self.manager, self.twitch,
                          DashboardConfig(port=self.dashboard.port, enabled=True))
        with self.assertRaises(DashboardError):
            await other.start()
        await other.stop()

    async def test_websocket_valid_token_passes_shared_ip_limit(self) -> None:
        await self.dashboard.stop()
        self.dashboard = Dashboard(self.manager, self.twitch,
                                   DashboardConfig(port=0, token="private-token", enabled=True,
                                                   auth_timeout=0.05))
        await self.dashboard.start()
        self.base = f"http://127.0.0.1:{self.dashboard.port}"
        for _ in range(5):
            async with self.session.get(self.base + "/api/state") as response:
                self.assertEqual(response.status, 401)
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            await ws.send_json({"auth": "private-token"})
            self.assertEqual((await ws.receive_json())["type"], "state")
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            await ws.send_json({"auth": "wrong-token"})
            await ws.receive()
            self.assertEqual(ws.close_code, WSCloseCode.POLICY_VIOLATION)
        self.assertEqual(len(next(iter(self.dashboard._failures.values()))), 5)

    async def test_state_is_delivered_during_continuous_logs(self) -> None:
        async with self.session.ws_connect(self.base + "/api/ws") as ws:
            await ws.receive_json()
            self.manager._logged_in = True
            self.manager.emit("change")

            async def produce_logs():
                while True:
                    self.manager.emit("log", "busy")
                    await asyncio.sleep(0.001)

            producer = asyncio.create_task(produce_logs())
            try:
                async def receive_state():
                    while True:
                        message = await ws.receive_json()
                        if message["type"] == "state":
                            return message

                state = await asyncio.wait_for(receive_state(), timeout=2)
                self.assertTrue(state["logged_in"])
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
