from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from constants import ClientType
from exceptions import LoginException
from twitch import _AuthState


class OAuthLoginTests(unittest.IsolatedAsyncioTestCase):
    async def _login_with(
        self, responses: list[tuple[int, object]], client=ClientType.ANDROID_APP,
        request_urls: list[str] | None = None, sleep_intervals: list[int] | None = None,
    ):
        requests = request_urls if request_urls is not None else []

        @asynccontextmanager
        async def request(method, url, **kwargs):
            requests.append(str(url))
            status, body = responses.pop(0)

            async def parse_json():
                if isinstance(body, Exception):
                    raise body
                return body

            yield SimpleNamespace(status=status, json=parse_json)

        login = SimpleNamespace(ask_enter_code=AsyncMock())
        twitch = SimpleNamespace(
            _client_type=client,
            gui=SimpleNamespace(login=login),
            request=request,
        )
        auth_state = _AuthState(twitch)
        auth_state.device_id = "local-device-id"
        with patch("twitch.asyncio.sleep", new_callable=AsyncMock) as sleep:
            try:
                return await auth_state._oauth_login(), requests, login
            finally:
                if sleep_intervals is not None:
                    sleep_intervals.extend(call.args[0] for call in sleep.await_args_list)

    async def test_invalid_client_is_permanent(self) -> None:
        with self.assertRaisesRegex(
            LoginException,
            '^Twitch rejected the device login for client ANDROID_APP: 400 invalid client\\. New device logins for this client are blocked by Twitch; restore a saved session with "cli auth restore" or "cli auth import --from-jar PATH"$',
        ):
            await self._login_with([(400, {"status": 400, "message": "invalid client"})])

    async def test_device_error_uses_twitch_message(self) -> None:
        with self.assertRaisesRegex(
            LoginException,
            "^Twitch rejected the device login for client ANDROID_APP: 400 unsupported grant$",
        ):
            await self._login_with([(400, {"message": "unsupported grant"})])

    async def test_device_error_message_is_sanitized(self) -> None:
        message = " \tInvalid\n\r request\x00 " + "x" * 140
        reason = "Invalid request " + "x" * (120 - len("Invalid request "))
        with self.assertRaises(LoginException) as raised:
            await self._login_with([(400, {"message": message})])
        self.assertEqual(
            str(raised.exception),
            f"Twitch rejected the device login for client ANDROID_APP: 400 {reason}",
        )

    async def test_device_error_without_string_message_uses_fallback(self) -> None:
        with self.assertRaisesRegex(LoginException, "400 request rejected$"):
            await self._login_with([(400, {"message": 123})])

    async def test_missing_device_code(self) -> None:
        with self.assertRaisesRegex(
            LoginException,
            "^Twitch rejected the device login for client ANDROID_APP: 200 missing device_code$",
        ):
            await self._login_with([(200, {"interval": 5})])

    async def test_non_json_device_response(self) -> None:
        with self.assertRaisesRegex(
            LoginException,
            "^Twitch rejected the device login for client ANDROID_APP: 200 invalid JSON response$",
        ):
            await self._login_with([(200, json.JSONDecodeError("invalid", "", 0))])

    async def test_non_dict_device_response(self) -> None:
        with self.assertRaises(LoginException):
            await self._login_with([(200, [])])

    async def test_unknown_client_does_not_expose_client_id(self) -> None:
        with self.assertRaisesRegex(LoginException, "client unknown: 400 invalid client"):
            await self._login_with(
                [(400, {"message": "invalid client"})],
                client=SimpleNamespace(
                    CLIENT_ID="private-client-id", CLIENT_URL="https://example.invalid", USER_AGENT="test"
                ),
            )

    async def test_invalid_token_payload_after_pending(self) -> None:
        device_response = {
            "device_code": "local-device-code", "user_code": "LOCAL123",
            "verification_uri": "https://www.twitch.tv/activate",
            "interval": 1, "expires_in": 1800,
        }
        with self.assertRaisesRegex(
            LoginException, "^Twitch returned an invalid token response for client ANDROID_APP$"
        ):
            await self._login_with([
                (200, device_response), (400, {"message": "authorization pending"}),
                (200, json.JSONDecodeError("invalid", "", 0)),
            ])

    async def test_authorization_pending_keeps_polling(self) -> None:
        requests, intervals = [], []
        token, _, login = await self._login_with([
            (200, self._device_response()),
            (400, {"status": 400, "message": "authorization_pending"}),
            (200, {"access_token": "test-token"}),
        ], request_urls=requests, sleep_intervals=intervals)
        self.assertEqual(token, "test-token")
        self.assertEqual(len(requests), 3)
        self.assertEqual(intervals, [1, 1])
        login.ask_enter_code.assert_awaited_once()

    async def test_slow_down_increases_polling_interval(self) -> None:
        intervals = []
        token, requests, _ = await self._login_with([
            (200, self._device_response()),
            (400, {"message": "SLOW_DOWN"}),
            (200, {"access_token": "test-token"}),
        ], sleep_intervals=intervals)
        self.assertEqual(token, "test-token")
        self.assertEqual(len(requests), 3)
        self.assertEqual(intervals, [1, 6])

    async def test_permanent_token_errors_do_not_retry(self) -> None:
        for message in ("Invalid Device Code", "access_denied", "expired_token", "INVALID CLIENT"):
            with self.subTest(message=message):
                requests, intervals = [], []
                with self.assertRaises(LoginException) as raised:
                    await self._login_with([
                        (200, self._device_response()),
                        (400, {"message": message}),
                    ], request_urls=requests, sleep_intervals=intervals)
                self.assertIn("client ANDROID_APP", str(raised.exception))
                self.assertIn(message.casefold(), str(raised.exception).casefold())
                self.assertNotIn("local-device-code", str(raised.exception))
                self.assertEqual(len(requests), 2)
                self.assertEqual(intervals, [1])

    async def test_transient_token_errors_keep_polling(self) -> None:
        intervals = []
        token, requests, _ = await self._login_with([
            (200, self._device_response()),
            (429, {"message": "too many requests"}),
            (503, json.JSONDecodeError("invalid", "", 0)),
            (400, {"message": "authorization pending"}),
            (200, {"access_token": "test-token"}),
        ], sleep_intervals=intervals)
        self.assertEqual(token, "test-token")
        self.assertEqual(len(requests), 5)
        self.assertEqual(intervals, [1, 1, 1, 1])

    async def test_unknown_or_unparseable_token_error_stops(self) -> None:
        for status, payload in (
            (400, {"message": "invalid_grant"}),
            (400, json.JSONDecodeError("invalid", "", 0)),
            (403, {"message": "forbidden"}),
        ):
            with self.subTest(status=status, payload=payload):
                requests = []
                with self.assertRaises(LoginException) as raised:
                    await self._login_with([
                        (200, self._device_response()), (status, payload),
                    ], request_urls=requests)
                self.assertIn(f"{status} ", str(raised.exception))
                self.assertEqual(len(requests), 2)

    async def test_token_error_reason_masks_codes(self) -> None:
        with self.assertRaises(LoginException) as raised:
            await self._login_with([
                (200, self._device_response()),
                (400, {"message": "bad local-device-code for LOCAL123"}),
            ])
        message = str(raised.exception)
        self.assertNotIn("local-device-code", message)
        self.assertNotIn("LOCAL123", message)
        self.assertIn("<redacted>", message)

    @staticmethod
    def _device_response() -> dict[str, object]:
        return {
            "device_code": "local-device-code", "user_code": "LOCAL123",
            "verification_uri": "https://www.twitch.tv/activate",
            "interval": 1, "expires_in": 1800,
        }
