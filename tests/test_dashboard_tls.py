from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientConnectorError, ClientSession, ServerDisconnectedError

from cli_actions import Actions
from dashboard import Dashboard, DashboardConfig, DashboardError, resolve_dashboard_config
from dashboard_tls import TLSError, fingerprint, self_signed_pair, server_context, subject_alt_names
from tests.test_dashboard import FakeManager, FakeTwitch

OPENSSL = shutil.which("openssl")


def _args(**values) -> argparse.Namespace:
    return argparse.Namespace(**{"dashboard": True, **values})


class TLSConfigTests(unittest.TestCase):
    def test_flags_and_env(self) -> None:
        self.assertFalse(resolve_dashboard_config(_args(), {}).tls)
        self.assertTrue(resolve_dashboard_config(_args(dashboard_tls=True), {}).tls)
        self.assertTrue(resolve_dashboard_config(_args(), {"TDM_DASHBOARD_TLS": "1"}).tls)
        config = resolve_dashboard_config(_args(), {"TDM_DASHBOARD_CERT": "c.pem", "TDM_DASHBOARD_KEY": "k.pem"})
        # a given pair implies TLS
        self.assertTrue(config.tls)
        self.assertEqual((config.tls_cert, config.tls_key), (Path("c.pem"), Path("k.pem")))

    def test_cert_and_key_go_together(self) -> None:
        for values in ({"dashboard_cert": "c.pem"}, {"dashboard_key": "k.pem"}):
            with self.subTest(values=values), self.assertRaises(argparse.ArgumentError):
                resolve_dashboard_config(_args(**values), {})


class SubjectAltNameTests(unittest.TestCase):
    def test_names_and_addresses_are_validated(self) -> None:
        san = subject_alt_names(["192.168.1.5", "bad,value", "0.0.0.0", "fe80::1"], "0.0.0.0")
        entries = san.split(",")
        for entry in ("DNS:localhost", "IP:127.0.0.1", "IP:::1", "IP:192.168.1.5", "IP:fe80::1"):
            self.assertIn(entry, entries)
        self.assertNotIn("IP:0.0.0.0", entries)
        self.assertFalse(any("bad" in entry for entry in entries))
        self.assertIn("DNS:panel.lan", subject_alt_names([], "panel.lan").split(","))


@unittest.skipIf(OPENSSL is None, "openssl is not installed")
class SelfSignedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "dashboard-tls"

    def test_pair_is_private_reused_and_loadable(self) -> None:
        cert, key = self_signed_pair(self.path, ["192.168.1.5"], "0.0.0.0")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode) & 0o077, 0)
        self.assertEqual(list(self.path.iterdir()), [cert, key] if cert < key else [key, cert])
        content = cert.read_bytes()
        self.assertEqual(self_signed_pair(self.path, [], "127.0.0.1"), (cert, key))
        self.assertEqual(cert.read_bytes(), content)
        self.assertIsInstance(server_context(cert, key), ssl.SSLContext)
        self.assertRegex(fingerprint(cert), r"\A(?:[0-9A-F]{2}:){31}[0-9A-F]{2}\Z")

    @unittest.skipUnless(os.name == "posix", "POSIX key permissions")
    def test_key_readable_by_others_is_refused(self) -> None:
        cert, key = self_signed_pair(self.path, [], "127.0.0.1")
        key.chmod(0o644)
        with self.assertRaisesRegex(TLSError, "chmod 600"):
            server_context(cert, key)

    def test_openssl_failure_leaves_nothing_behind(self) -> None:
        with patch("dashboard_tls.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(TLSError, "exit 1"):
                self_signed_pair(self.path, [], "127.0.0.1")
        self.assertEqual(list(self.path.iterdir()), [])


class MissingOpenSSLTests(unittest.TestCase):
    def test_clear_error_without_openssl(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("dashboard_tls.shutil.which", return_value=None):
            with self.assertRaisesRegex(TLSError, "--dashboard-cert"):
                self_signed_pair(Path(directory) / "tls", [], "127.0.0.1")


@unittest.skipIf(OPENSSL is None, "openssl is not installed")
class HTTPSDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are not allowed here")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cert, self.key = self_signed_pair(Path(self.directory.name) / "tls", [], "127.0.0.1")
        self.manager = FakeManager()
        self.twitch = FakeTwitch()
        self.manager.actions = Actions(self.twitch, self.manager)
        self.dashboard = Dashboard(self.manager, self.twitch, DashboardConfig(
            port=0, enabled=True, tls=True, tls_cert=self.cert, tls_key=self.key))
        await self.dashboard.start()
        self.session = ClientSession()
        self.trusted = ssl.create_default_context(cafile=str(self.cert))

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.dashboard.stop()

    async def test_https_and_wss_with_the_printed_fingerprint(self) -> None:
        base = f"https://127.0.0.1:{self.dashboard.port}"
        self.assertIn(f"Dashboard: {base}/", self.manager.lines)
        self.assertIn(f"Dashboard certificate SHA-256: {fingerprint(self.cert)}", self.manager.lines)
        async with self.session.get(base + "/api/state", ssl=self.trusted) as response:
            self.assertEqual(response.status, 200)
        async with self.session.ws_connect(base.replace("https", "wss") + "/api/ws", ssl=self.trusted) as ws:
            self.assertEqual((await ws.receive_json())["type"], "state")

    async def test_plain_http_is_not_served_and_not_logged_as_error(self) -> None:
        with self.assertNoLogs("asyncio", level="ERROR"), self.assertRaises((ClientConnectorError, ServerDisconnectedError, asyncio.TimeoutError, OSError)):
            async with self.session.get(f"http://127.0.0.1:{self.dashboard.port}/api/state",
                                        timeout=__import__("aiohttp").ClientTimeout(total=3)) as response:
                await response.read()
        await asyncio.sleep(0.1)

    async def test_stop_restores_the_loop_handler(self) -> None:
        await self.dashboard.stop()
        self.assertIsNone(asyncio.get_running_loop().get_exception_handler())


class TLSStartFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_certificate_fails_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory, "c.pem"), Path(directory, "k.pem")
            cert.write_text("not a certificate")
            key.write_text("not a key")
            key.chmod(0o600)
            manager = FakeManager()
            manager.actions = Actions(FakeTwitch(), manager)
            dashboard = Dashboard(manager, FakeTwitch(), DashboardConfig(
                port=0, enabled=True, tls=True, tls_cert=cert, tls_key=key))
            with self.assertRaisesRegex(DashboardError, "cannot load"):
                await dashboard.start()
            self.assertIsNone(dashboard._runner)


if __name__ == "__main__":
    unittest.main()
