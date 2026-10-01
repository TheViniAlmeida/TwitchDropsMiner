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
    async def _login_with(self, responses: list[tuple[int, object]], client=ClientType.ANDROID_APP):
        requests = []

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
        with patch("twitch.asyncio.sleep", new_callable=AsyncMock):
            return await auth_state._oauth_login(), requests, login

    async def test_invalid_client_is_permanent(self) -> None:
        with self.assertRaisesRegex(
            LoginException,
            "^Twitch rejected the device login for client ANDROID_APP: 400 invalid client$",
        ):
            await self._login_with([(400, {"status": 400, "message": "invalid client"})])

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
