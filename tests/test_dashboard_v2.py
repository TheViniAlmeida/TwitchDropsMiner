from __future__ import annotations

import argparse
import asyncio
import errno
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import make_mocked_request

from dashboard import (Dashboard, DashboardConfig, DashboardError, _filter_ips,
                       _local_ips, resolve_dashboard_config)
from tests.test_dashboard import FakeManager, FakeTwitch
from version import __version__


class ConfigV2Tests(unittest.TestCase):
    def test_ports(self):
        args = argparse.Namespace(dashboard=True, dashboard_port=None)
        config = resolve_dashboard_config(args, {})
        self.assertEqual((config.port, config.port_range), (23450, (23450, 23500)))
        for raw, expected in (("12345", (12345, None)), ("10-12", (10, (10, 12))),
                              ("0", (0, None))):
            with self.subTest(raw=raw):
                args.dashboard_port = raw
                resolved = resolve_dashboard_config(args, {})
                self.assertEqual((resolved.port, resolved.port_range), expected)
        for raw in ("12-11", "0-2", "70000", "abc", "1-70000", "1-2-3", "-1"):
            args.dashboard_port = raw
            with self.subTest(raw=raw), self.assertRaises(argparse.ArgumentError):
                resolve_dashboard_config(args, {})
        args.dashboard_port = None
        self.assertEqual(resolve_dashboard_config(args, {"TDM_DASHBOARD_PORT": "10-11"}).port_range,
                         (10, 11))

    def test_optional_file_and_env_precedence(self):
        args = argparse.Namespace(dashboard=True, dashboard_host="0.0.0.0",
                                  dashboard_token_file=None)
        self.assertIsNone(resolve_dashboard_config(args, {}).token)
        with tempfile.TemporaryDirectory() as directory, patch("dashboard.DATA_DIR", Path(directory)):
            args.dashboard_token_file = ""
            config = resolve_dashboard_config(args, {})
            self.assertEqual(config.token_file, Path(directory) / "dashboard.token")
            self.assertTrue(config.token)
            self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)
            self.assertEqual(resolve_dashboard_config(args, {}).token, config.token)
            config.token_file.chmod(0o644)
            with self.assertLogs("TwitchDrops.dashboard", "WARNING"):
                self.assertEqual(resolve_dashboard_config(args, {}).token, config.token)
            self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)
            with self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
                chosen = resolve_dashboard_config(args, {"TDM_DASHBOARD_TOKEN": "env-token"})
            self.assertEqual(chosen.token, "env-token")
            self.assertIsNone(chosen.token_file)
            self.assertIn("ignored", warnings.output[0])
            args.dashboard_token_file = None
            self.assertEqual(resolve_dashboard_config(args, {
                "TDM_DASHBOARD_TOKEN_FILE": str(config.token_file)}).token, config.token)
            config.token_file.write_text("")
            with self.assertRaises(DashboardError):
                resolve_dashboard_config(args, {
                    "TDM_DASHBOARD_TOKEN_FILE": str(config.token_file)})

    def test_empty_env_token_is_an_error(self):
        args = argparse.Namespace(dashboard=True, dashboard_host="0.0.0.0", dashboard_port=None)
        with self.assertRaises(argparse.ArgumentError):
            resolve_dashboard_config(args, {"TDM_DASHBOARD_TOKEN": ""})

    def test_filter_ips(self):
        entries = [("eth0", "192.168.1.5"), ("eth0", "2001:4860:4860::8888"),
                   ("eth0", "fe80::1"), ("eth0", "::1"), ("eth0", "127.0.0.1"),
                   ("eth0", "invalid"), ("eth0", "8.8.8.8"),
                   ("docker0", "172.17.0.1"), ("br-abc", "10.0.0.1"),
                   ("veth0", "10.0.0.2"), ("virbr0", "10.0.0.3"),
                   ("cni0", "10.0.0.4"), ("flannel.1", "10.0.0.5"),
                   ("docker0", "2001:4860:4860::8888")]
        self.assertEqual(_filter_ips(entries), ["192.168.1.5", "2001:4860:4860::8888"])
        fake_proc = ("20014860486000000000000000008888 02 40 00 80 eth0\n"
                     "fe800000000000000000000000000001 02 40 20 80 eth0\n"
                     "20014860486000000000000000008889 02 40 00 80 docker0\n")
        if os.name != "posix":
            self.skipTest("Linux interface discovery test")
        def fake_ioctl(descriptor, command, data):
            address = "172.17.0.1" if data.startswith(b"docker") else "192.168.1.5"
            return bytes(20) + socket.inet_aton(address)

        class FakeProbe:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def fileno(self):
                return 1

        with patch("dashboard.Path.read_text", return_value=fake_proc), patch(
                "dashboard.socket.if_nameindex", return_value=[(1, "eth0"), (2, "docker0")]), \
                patch("dashboard.socket.socket", return_value=FakeProbe()), \
                patch("fcntl.ioctl", side_effect=fake_ioctl):
            self.assertEqual(_local_ips(), ["192.168.1.5", "2001:4860:4860::8888"])


class FakeActions:
    readonly = False

    def __init__(self):
        self.campaign_args = None
        self.samples = 0

    def state(self):
        return {"state": "idle"}

    def progress(self):
        self.samples += 1
        return {"id": "drop-1", "campaign": "Campaign", "progress": .5,
                "remaining_minutes": 12}

    def campaigns(self, filters=None, *, game=None, include_all=False):
        self.campaign_args = (filters, game, include_all)
        return [{"id": "campaign-1"}]

    def drops(self, target):
        return [{"target": target}]

    def games(self):
        return [{"game": "A", "status": "available"}]

    def game(self, name):
        return {"name": name}

    def settings_schema(self):
        return {"language": {"type": "choice"}}

    def game_choices(self):
        return ["A"]


class RoutesV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = FakeManager()
        self.manager.actions = FakeActions()
        self.dashboard = Dashboard(self.manager, FakeTwitch(),
                                   DashboardConfig(host="0.0.0.0", port=0))

    async def call(self, path, headers=None):
        headers = {"Host": f"127.0.0.1:{self.dashboard.port}", **(headers or {})}
        request = make_mocked_request("GET", path, headers=headers, app=self.dashboard.app)
        request._match_info = await self.dashboard.app.router.resolve(request)
        handler = request.match_info.handler
        for middleware in reversed(self.dashboard.app.middlewares):
            previous = handler

            async def wrapped(req, middleware=middleware, previous=previous):
                return await middleware(req, previous)

            handler = wrapped
        return await handler(request)

    async def test_routes_and_queries_without_token(self):
        for path in ("/api/state", "/api/meta", "/api/campaigns", "/api/drops?target=A",
                     "/api/games", "/api/game?name=A", "/api/settings/schema",
                     "/api/game_choices", "/api/progress", "/api/history"):
            with self.subTest(path=path):
                self.assertEqual((await self.call(path)).status, 200)
        self.assertEqual(json.loads((await self.call("/api/meta")).text),
                         {"auth": False, "readonly": False, "version": __version__})
        self.assertEqual(json.loads((await self.call("/api/state")).text)["auth"], False)
        self.assertEqual(json.loads((await self.call("/api/games")).text)[0]["status"], "available")
        self.assertEqual(json.loads((await self.call("/api/drops?target=A")).text),
                         [{"target": "A"}])
        await self.call("/api/campaigns?all=1&game=A&finished=0&upcoming=1")
        self.assertEqual(self.manager.actions.campaign_args,
                         ({"finished": False, "upcoming": True}, "A", True))
        for key in ("all", "not_linked", "upcoming", "expired", "excluded", "finished"):
            self.assertEqual((await self.call(f"/api/campaigns?{key}=yes")).status, 400)
        for path in ("/api/drops", "/api/game", "/api/history?since=wrong"):
            self.assertEqual((await self.call(path)).status, 400)

    async def test_host_header_blocks_dns_rebinding_on_lan_binds(self):
        port = self.dashboard.port
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1:1", "a@127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual((await self.call("/api/meta", {"Host": host})).status, 403)
        with patch("dashboard._interface_entries", return_value=[("eth0", "192.168.1.50")]):
            self.dashboard._hosts = None
            self.assertEqual((await self.call("/api/meta", {"Host": f"192.168.1.50:{port}"})).status, 200)
        self.dashboard.config = DashboardConfig(host="0.0.0.0", port=0,
                                                origins=("https://drops.example.com",))
        self.dashboard._hosts = None
        self.assertEqual((await self.call("/api/meta", {"Host": "drops.example.com"})).status, 200)
        self.assertEqual((await self.call("/api/meta", {"Host": "other.example.com"})).status, 403)

    async def test_auth_meta_and_rate(self):
        self.dashboard.config = DashboardConfig(host="0.0.0.0", port=0,
                                                token="private-token", readonly=True)
        meta = await self.call("/api/meta")
        self.assertEqual(json.loads(meta.text),
                         {"auth": True, "readonly": True, "version": __version__})
        for _ in range(5):
            self.assertEqual((await self.call("/api/state")).status, 401)
        self.assertEqual((await self.call("/api/state")).status, 429)
        state = await self.call("/api/state", {"Authorization": "Bearer private-token"})
        self.assertEqual((json.loads(state.text)["auth"], json.loads(state.text)["readonly"]),
                         (True, True))

    async def test_history_and_stop(self):
        self.dashboard.config = DashboardConfig(host="0.0.0.0", port=0, history_interval=.01)
        self.dashboard._sample_history()
        self.assertEqual(len(self.dashboard.history), 1)
        self.assertEqual(set(self.dashboard.history[0]),
                         {"t", "drop_id", "campaign", "progress", "remaining_minutes", "claimed", "total"})
        for _ in range(1441):
            self.dashboard._sample_history()
        self.assertEqual(len(self.dashboard.history), 1440)
        now = int(time.time())
        response = await self.call(f"/api/history?since={now + 60}")
        self.assertEqual(json.loads(response.text), [])
        task = asyncio.create_task(self.dashboard._sample_history_loop())
        self.dashboard._history_task = task
        await asyncio.sleep(.035)
        self.assertGreater(self.manager.actions.samples, 1442)
        await self.dashboard.stop()
        self.assertTrue(task.cancelled())

    async def test_sampling_error_logs_once_and_recovers(self):
        with patch.object(self.manager.actions, "progress", side_effect=ValueError("sampling failed")):
            with self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
                self.dashboard._sample_history()
                self.dashboard._sample_history()
        self.assertEqual(len(warnings.output), 1)
        self.dashboard._sample_history()
        self.assertEqual(len(self.dashboard.history), 1)

    async def test_exposed_warning_once(self):
        class FakeSocket:
            def getsockname(self):
                return ("0.0.0.0", 12345)

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

        with patch("dashboard.web.AppRunner", FakeRunner), patch("dashboard.web.TCPSite", FakeSite), \
                patch("dashboard._local_ips", return_value=[]), redirect_stdout(io.StringIO()), \
                self.assertLogs("TwitchDrops.dashboard", "WARNING") as warnings:
            await self.dashboard.start()
            await self.dashboard.stop()
        warning = "dashboard exposed WITHOUT authentication: anyone on this network can control the miner"
        self.assertEqual(self.manager.lines.count(warning), 1)
        self.assertEqual(warnings.output.count("WARNING:TwitchDrops.dashboard:" + warning), 1)


class BindV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_port_scan_and_cleanup_without_sockets(self):
        cleanup_calls = []
        stopped_ports = []

        class FakeRunner:
            def __init__(self, *args, **kwargs):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                cleanup_calls.append(True)

        class FakeSocket:
            def __init__(self, port):
                self.port = port

            def getsockname(self):
                return ("127.0.0.1", self.port)

        class FakeSite:
            def __init__(self, runner, host, port):
                self.port = port
                self._server = None

            async def start(self):
                if self.port == 23450:
                    raise OSError(errno.EADDRINUSE, "busy")
                self._server = SimpleNamespace(sockets=[FakeSocket(self.port)])

            async def stop(self):
                stopped_ports.append(self.port)

        manager = FakeManager()
        manager.actions = FakeActions()
        with patch("dashboard.web.AppRunner", FakeRunner), patch("dashboard.web.TCPSite", FakeSite), \
                redirect_stdout(io.StringIO()):
            dashboard = Dashboard(manager, FakeTwitch(), DashboardConfig(
                port=23450, port_range=(23450, 23451)))
            await dashboard.start()
            self.assertEqual(dashboard.port, 23451)
            await dashboard.stop()
            self.assertEqual(stopped_ports, [23450])
            self.assertEqual(len(cleanup_calls), 1)

            exhausted = Dashboard(manager, FakeTwitch(), DashboardConfig(
                port=23450, port_range=(23450, 23450)))
            with self.assertRaisesRegex(DashboardError, "no free port in 23450-23450"):
                await exhausted.start()
            self.assertEqual(len(cleanup_calls), 2)

            exact = Dashboard(manager, FakeTwitch(), DashboardConfig(port=23450))
            with self.assertRaises(DashboardError):
                await exact.start()
            self.assertEqual(len(cleanup_calls), 3)

            class InvalidHostSite(FakeSite):
                async def start(self):
                    raise OSError(errno.EINVAL, "bad host")

            with patch("dashboard.web.TCPSite", InvalidHostSite):
                invalid = Dashboard(manager, FakeTwitch(), DashboardConfig(
                    port=23450, port_range=(23450, 23451)))
                with self.assertRaises(DashboardError):
                    await invalid.start()
            self.assertEqual(len(cleanup_calls), 4)

    async def _start(self, config):
        manager = FakeManager()
        manager.actions = FakeActions()
        dashboard = Dashboard(manager, FakeTwitch(), config)
        try:
            await dashboard.start()
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("loopback sockets unavailable in sandbox")
            raise
        except DashboardError as exc:
            if isinstance(exc.__cause__, OSError) and exc.__cause__.errno == errno.EPERM:
                self.skipTest("loopback sockets unavailable in sandbox")
            raise
        return dashboard

    async def test_default_range_skips_busy_port(self):
        try:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("loopback sockets unavailable in sandbox")
            raise
        with listener:
            occupied = listener.getsockname()[1]
            if occupied == 65535:
                self.skipTest("ephemeral port has no next port")
            dashboard = await self._start(DashboardConfig(port=occupied,
                port_range=(occupied, occupied + 1)))
            try:
                self.assertEqual(dashboard.port, occupied + 1)
                async with ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{dashboard.port}/api/meta") as response:
                        self.assertEqual(response.status, 200)
            finally:
                await dashboard.stop()
            exact_manager = FakeManager()
            exact_manager.actions = FakeActions()
            exact = Dashboard(exact_manager, FakeTwitch(), DashboardConfig(port=occupied))
            with self.assertRaises(DashboardError):
                await exact.start()
            exhausted_manager = FakeManager()
            exhausted_manager.actions = FakeActions()
            exhausted = Dashboard(exhausted_manager, FakeTwitch(),
                                  DashboardConfig(port=occupied, port_range=(occupied, occupied)))
            with self.assertRaisesRegex(DashboardError, "no free port"):
                await exhausted.start()

    async def test_default_range_with_first_port_occupied(self):
        try:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 23450))
            listener.listen()
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("loopback sockets unavailable in sandbox")
            if exc.errno == errno.EADDRINUSE:
                self.skipTest("first default dashboard port already occupied")
            raise
        with listener:
            manager = FakeManager()
            manager.actions = FakeActions()
            config = resolve_dashboard_config(argparse.Namespace(dashboard=True), {})
            dashboard = Dashboard(manager, FakeTwitch(), config)
            try:
                await dashboard.start()
            except DashboardError as exc:
                if isinstance(exc.__cause__, OSError) and exc.__cause__.errno == errno.EPERM:
                    self.skipTest("loopback sockets unavailable in sandbox")
                raise
            try:
                self.assertGreaterEqual(dashboard.port, 23451)
                self.assertLessEqual(dashboard.port, 23500)
            finally:
                await dashboard.stop()
