from __future__ import annotations

import asyncio
from aiohttp import BasicAuth
from multidict import CIMultiDict
from types import SimpleNamespace
import unittest

from yarl import URL

from cli_commands import setting_value
from twitch import _redacted_request_kwargs
from websocket import Websocket, _redacted_sent_message


class RedactionTests(unittest.TestCase):
    def test_request_kwargs_are_redacted_without_mutation(self) -> None:
        kwargs = {
            "headers": CIMultiDict({"aUtHoRiZaTiOn": "Bearer sample", "Client-Integrity": "proof",
                                    "Cookie": "session=sample", "X-Device-Id": "device"}),
            "data": {"token": "sample", "device_code": "sample", "other": "public"},
            "json": {"access_token": "sample", "refresh_token": "sample"},
            "params": {"password": "sample", "client_secret": "sample",
                       "auth-token": "sample"},
            "proxy": URL("http://alice:sample@localhost:8080"),
            "auth": BasicAuth("auth_user", "auth_password"),
            "proxy_auth": BasicAuth("proxy_user", "proxy_password"),
        }
        redacted = _redacted_request_kwargs(kwargs)

        for value in redacted["headers"].values():
            self.assertEqual(value, "<redacted>")
        self.assertEqual(redacted["data"], {"token": "<redacted>",
                                           "device_code": "<redacted>", "other": "public"})
        self.assertTrue(all(value == "<redacted>" for value in redacted["json"].values()))
        self.assertTrue(all(value == "<redacted>" for value in redacted["params"].values()))
        self.assertEqual(redacted["proxy"], "http://alice:***@localhost:8080")
        self.assertEqual(_redacted_request_kwargs({"proxy": URL("http://proxy:8080/?token=sample")})["proxy"],
                         "http://proxy:8080/?***")
        self.assertEqual(_redacted_request_kwargs({"proxy": URL("http://proxy:8080")})["proxy"],
                         "http://proxy:8080")
        self.assertEqual(redacted["auth"], "<redacted>")
        self.assertEqual(redacted["proxy_auth"], "<redacted>")
        redacted_repr = repr(redacted)
        for secret in ("auth_user", "auth_password", "proxy_user", "proxy_password"):
            self.assertNotIn(secret, redacted_repr)
        self.assertEqual(kwargs["headers"]["aUtHoRiZaTiOn"], "Bearer sample")
        self.assertEqual(kwargs["data"]["token"], "sample")
        self.assertEqual(kwargs["proxy"].password, "sample")
        self.assertIsNot(redacted, kwargs)
        self.assertIsNot(redacted["headers"], kwargs["headers"])

    def test_setting_value_masks_proxy_password(self) -> None:
        proxy = URL("http://alice:sample@localhost:8080")
        settings = SimpleNamespace(proxy=proxy)
        self.assertEqual(setting_value(settings, "proxy"), "http://alice:***@localhost:8080")
        self.assertEqual(settings.proxy, proxy)
        self.assertEqual(settings.proxy.password, "sample")
        settings.proxy = URL("http://proxy:8080/?token=sample#frag")
        self.assertEqual(setting_value(settings, "proxy"), "http://proxy:8080/?***")

    def test_websocket_log_copy_masks_auth_token(self) -> None:
        message = {"type": "LISTEN", "data": {"auth_token": "sample", "topics": ["a"]}}
        redacted = _redacted_sent_message(message)
        self.assertEqual(redacted["data"]["auth_token"], "<redacted>")
        self.assertEqual(redacted["data"]["topics"], ["a"])
        self.assertEqual(message["data"]["auth_token"], "sample")
        self.assertIsNot(redacted, message)
        self.assertIsNot(redacted["data"], message["data"])

    def test_websocket_send_logs_copy_and_sends_original(self) -> None:
        async def check() -> None:
            sent = []

            class Socket:
                async def send_json(self, message, *, dumps):
                    sent.append(message.copy())

            websocket = Websocket.__new__(Websocket)
            websocket._idx = 1
            websocket._ws = SimpleNamespace(get_with_default=lambda _: Socket())
            message = {"type": "LISTEN", "data": {"auth_token": "sample", "topics": ["a"]}}
            with self.assertLogs("TwitchDrops.websocket", level="DEBUG") as captured:
                await websocket.send(message)
            self.assertEqual(sent[0]["data"]["auth_token"], "sample")
            self.assertEqual(message["data"]["auth_token"], "sample")
            self.assertIn("<redacted>", captured.output[0])
            self.assertNotIn("sample", captured.output[0])

        asyncio.run(check())
