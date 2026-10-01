from __future__ import annotations

import asyncio

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from yarl import URL

import auth_session
import cli_commands
from constants import ClientType
from exceptions import LoginException
from twitch import _AuthState


class SessionFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "cookies.jar"

    def test_backup_is_atomic_private_and_empty_source_is_ignored(self) -> None:
        self.assertIsNone(auth_session.backup_session(self.path))
        self.path.touch()
        self.assertIsNone(auth_session.backup_session(self.path))
        self.path.write_bytes(b"original session")
        with patch("auth_session.os.replace", wraps=os.replace) as replace:
            backup = auth_session.backup_session(self.path)
        self.assertEqual(backup.read_bytes(), b"original session")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(replace.call_count, 1)
        self.assertNotEqual(Path(replace.call_args.args[0]), self.path)

    def test_ensure_backup_is_idempotent_and_restore_round_trip(self) -> None:
        auth_session.write_session("first", "1", ClientType.ANDROID_APP, self.path)
        backup = auth_session.ensure_backup(self.path)
        auth_session.write_session("second", "2", ClientType.ANDROID_APP, self.path)
        self.assertEqual(auth_session.ensure_backup(self.path), backup)
        self.assertEqual(auth_session.read_token(backup)[0], "second")
        self.assertEqual(auth_session.archive_count(self.path), 1)
        auth_session.write_session("first", "1", ClientType.ANDROID_APP, self.path)
        self.assertEqual(auth_session.ensure_backup(self.path), backup)
        self.assertEqual(auth_session.read_token(backup)[0], "first")
        restored, previous = auth_session.restore_session(self.path)
        self.assertEqual(auth_session.read_token(restored)[0], "first")
        self.assertEqual(auth_session.read_token(previous)[0], "first")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(previous.stat().st_mode), 0o600)

    def test_save_jar_is_atomic_private_and_preserves_old_on_failure(self) -> None:
        with auth_session._cookie_jar() as jar:
            jar.update_cookies({"auth-token": "new-secret"}, ClientType.ANDROID_APP.CLIENT_URL)
            self.path.write_bytes(b"old")
            with patch("auth_session.os.replace", wraps=os.replace) as replace:
                auth_session.save_jar(jar, self.path)
            self.assertEqual(replace.call_count, 1)
            self.assertNotEqual(Path(replace.call_args.args[0]), self.path)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            saved = self.path.read_bytes()
            with patch("auth_session.os.replace", side_effect=OSError(5, "storage failure")):
                with self.assertRaises(OSError):
                    auth_session.save_jar(jar, self.path)
            self.assertEqual(self.path.read_bytes(), saved)
            self.assertEqual(list(self.path.parent.glob(".cookies.jar.*")), [])

    def test_archives_keep_every_distinct_session(self) -> None:
        for number in range(8):
            auth_session.write_session(f"secret-{number}", "7", ClientType.ANDROID_APP, self.path)
            backup = self.path.with_name("cookies.jar.bak")
            if backup.exists():
                timestamp = 1788220800 + number * 60
                os.utime(backup, (timestamp, timestamp))
            auth_session.backup_session(self.path)
        # backing up the same session again must not create a duplicate archive
        auth_session.backup_session(self.path)
        archives = sorted(self.path.parent.glob("cookies.jar.bak.[0-9]*"))
        self.assertEqual(auth_session.archive_count(self.path), 7)
        self.assertEqual([auth_session.read_token(archive)[0] for archive in archives],
                         [f"secret-{number}" for number in range(7)])
        self.assertEqual(auth_session.read_token(self.path.with_name("cookies.jar.bak"))[0], "secret-7")

    def test_save_jar_backs_up_before_dropping_the_token(self) -> None:
        import aiohttp
        auth_session.write_session("only-copy", "1", ClientType.ANDROID_APP, self.path)

        async def save_empty() -> None:
            auth_session.save_jar(aiohttp.CookieJar(), self.path)

        asyncio.run(save_empty())
        self.assertEqual(auth_session.read_token(self.path.with_name("cookies.jar.bak"))[0], "only-copy")
        auth_session.write_session("only-copy", "1", ClientType.ANDROID_APP, self.path)
        with patch.object(auth_session, "_copy_atomic", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                asyncio.run(save_empty())
        self.assertEqual(auth_session.read_token(self.path)[0], "only-copy")

    def test_save_jar_backs_up_before_replacing_the_token(self) -> None:
        auth_session.write_session("first-secret", "1", ClientType.ANDROID_APP, self.path)
        with auth_session._cookie_jar() as jar:
            jar.update_cookies({"auth-token": "second-secret"}, ClientType.ANDROID_APP.CLIENT_URL)
            auth_session.save_jar(jar, self.path)
            backup = self.path.with_name("cookies.jar.bak")
            self.assertEqual(auth_session.read_token(backup)[0], "first-secret")
            self.assertEqual(auth_session.read_token(self.path)[0], "second-secret")
            with patch("auth_session.backup_session", side_effect=OSError(28, "no space")):
                jar.update_cookies({"auth-token": "third-secret"}, ClientType.ANDROID_APP.CLIENT_URL)
                with self.assertRaises(OSError):
                    auth_session.save_jar(jar, self.path)
            self.assertEqual(auth_session.read_token(self.path)[0], "second-secret")

    def test_corrupt_session_never_replaces_a_valid_backup(self) -> None:
        backup = self.path.with_name("cookies.jar.bak")
        auth_session.write_session("valid-secret", "1", ClientType.ANDROID_APP, backup)
        self.path.write_bytes(b"corrupt")
        copy = auth_session.backup_session(self.path)
        self.assertNotEqual(copy, backup)
        self.assertEqual(copy.read_bytes(), b"corrupt")
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(auth_session.read_token(backup)[0], "valid-secret")
        with self.assertRaisesRegex(ValueError, "cannot read the session file"):
            auth_session.read_token(self.path)

    def test_second_restore_keeps_the_first_previous_session(self) -> None:
        auth_session.write_session("backup", "1", ClientType.ANDROID_APP, self.path)
        auth_session.backup_session(self.path)
        auth_session.write_session("imported", "1", ClientType.ANDROID_APP, self.path)
        auth_session.restore_session(self.path)
        auth_session.write_session("third", "1", ClientType.ANDROID_APP, self.path)
        auth_session.restore_session(self.path)
        kept = {auth_session.read_token(item)[0] for item in self.path.parent.glob("cookies.jar.bak*")}
        self.assertTrue({"backup", "imported", "third"} <= kept)

    def test_unreadable_backup_is_archived_not_discarded(self) -> None:
        auth_session.write_session("valid", "1", ClientType.ANDROID_APP, self.path)
        backup = self.path.with_name("cookies.jar.bak")
        backup.write_bytes(b"unreadable")
        auth_session.backup_session(self.path)
        self.assertEqual(auth_session.archive_count(self.path), 1)
        self.assertEqual(auth_session.read_token(backup)[0], "valid")
        self.assertEqual(next(self.path.parent.glob("cookies.jar.bak.[0-9]*")).read_bytes(), b"unreadable")

    def test_restore_keeps_archives(self) -> None:
        for token in ("old", "middle", "new"):
            auth_session.write_session(token, "1", ClientType.ANDROID_APP, self.path)
            auth_session.backup_session(self.path)
        archives = {archive.name: archive.read_bytes() for archive in self.path.parent.glob("cookies.jar.bak.[0-9]*")}
        auth_session.restore_session(self.path)
        self.assertEqual({archive.name: archive.read_bytes() for archive in self.path.parent.glob("cookies.jar.bak.[0-9]*")}, archives)
        self.assertEqual(auth_session.read_token(self.path.with_name("cookies.jar.bak.prev"))[0], "new")

    def test_cookie_jar_round_trip(self) -> None:
        auth_session.write_session("sensitive-token", "42", ClientType.ANDROID_APP, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(auth_session.read_token(self.path), ("sensitive-token", "www.twitch.tv"))
        with auth_session._cookie_jar() as jar:
            jar.load(self.path)
            cookies = jar.filter_cookies(ClientType.ANDROID_APP.CLIENT_URL)
            self.assertEqual(cookies["persistent"].value, "42")


class AuthCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "cookies.jar"
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.lock = Mock()
        self.patches = (
            patch.object(cli_commands, "COOKIES_PATH", self.path),
            patch.object(cli_commands, "lock_file", return_value=(True, self.lock)),
            patch.object(cli_commands, "Settings", return_value=SimpleNamespace(proxy=URL())),
            patch("sys.stdout", self.output),
            patch("sys.stderr", self.errors),
        )
        for current in self.patches:
            current.start()
            self.addCleanup(current.stop)

    def run_auth(self, action: str, **kwargs) -> int:
        args = SimpleNamespace(command="auth", auth_command=action, from_jar=None, **kwargs)
        return cli_commands.run_offline(args)

    def test_import_from_jar_prioritizes_env_and_stdin(self) -> None:
        source = Path(self.temporary.name) / "source.jar"
        auth_session.write_session("jar-secret", "7", ClientType.ANDROID_APP, source)
        with patch.object(cli_commands, "validate_token", new_callable=AsyncMock) as validate:
            validate.return_value = dict(client_id=ClientType.ANDROID_APP.CLIENT_ID, login="alice", user_id="7", expires_in=123)
            with patch.dict(os.environ, {"TDM_AUTH_TOKEN": "env-secret"}), patch("sys.stdin", io.StringIO("stdin-secret\n")):
                args = SimpleNamespace(command="auth", auth_command="import", from_jar=str(source))
                self.assertEqual(cli_commands.run_offline(args), 0)
            self.assertEqual(auth_session.read_token(self.path)[0], "jar-secret")
            self.assertEqual(validate.call_args.args[1], "jar-secret")
            self.assertIn("imported session for alice (ANDROID_APP)", self.output.getvalue())
        self.assertNotIn("jar-secret", self.output.getvalue() + self.errors.getvalue())

    def test_import_env_preserves_old_jar_and_stdin_fallback(self) -> None:
        auth_session.write_session("old-secret", "8", ClientType.ANDROID_APP, self.path)
        with patch.object(cli_commands, "validate_token", new_callable=AsyncMock) as validate:
            validate.return_value = dict(client_id=ClientType.ANDROID_APP.CLIENT_ID, login="alice", user_id="7", expires_in=123)
            with patch.dict(os.environ, {"TDM_AUTH_TOKEN": "env-secret"}), patch("sys.stdin", io.StringIO("stdin-secret\n")):
                self.assertEqual(self.run_auth("import"), 0)
            self.assertEqual(auth_session.read_token(self.path)[0], "env-secret")
            self.assertEqual(auth_session.read_token(self.path.with_name("cookies.jar.bak"))[0], "env-secret")
            with patch.dict(os.environ, {"TDM_AUTH_TOKEN": ""}), patch("sys.stdin", io.StringIO("stdin-secret\n")):
                self.assertEqual(self.run_auth("import"), 0)
            self.assertEqual(auth_session.read_token(self.path)[0], "stdin-secret")
            self.assertEqual(auth_session.read_token(self.path.with_name("cookies.jar.bak"))[0], "stdin-secret")
            self.assertEqual({auth_session.read_token(archive)[0] for archive in self.path.parent.glob("cookies.jar.bak.[0-9]*")},
                             {"old-secret", "env-secret"})
        for secret in ("old-secret", "env-secret", "stdin-secret"):
            self.assertNotIn(secret, self.output.getvalue() + self.errors.getvalue())

    def test_other_client_refused_without_changing_existing_jar(self) -> None:
        auth_session.write_session("old-secret", "8", ClientType.ANDROID_APP, self.path)
        with patch.object(cli_commands, "validate_token", new_callable=AsyncMock) as validate:
            validate.return_value = dict(client_id=ClientType.WEB.CLIENT_ID, login="bob", user_id="7", expires_in=23)
            with patch.dict(os.environ, {"TDM_AUTH_TOKEN": "bad-secret"}):
                self.assertEqual(self.run_auth("import"), 2)
        self.assertEqual(auth_session.read_token(self.path)[0], "old-secret")
        self.assertEqual(self.errors.getvalue().strip(), "token belongs to client WEB; only ANDROID_APP sessions can mine drops today")
        self.assertNotIn("bad-secret", self.errors.getvalue() + self.output.getvalue())

    def test_import_backup_failure_preserves_saved_session(self) -> None:
        auth_session.write_session("old-secret", "8", ClientType.ANDROID_APP, self.path)
        with patch.object(cli_commands, "validate_token", new_callable=AsyncMock) as validate:
            validate.return_value = dict(client_id=ClientType.ANDROID_APP.CLIENT_ID, login="alice", user_id="7", expires_in=123)
            with patch.dict(os.environ, {"TDM_AUTH_TOKEN": "new-secret"}), patch.object(
                cli_commands, "backup_session", side_effect=OSError(28, "No space left on device")
            ):
                self.assertEqual(self.run_auth("import"), 2)
        self.assertEqual(auth_session.read_token(self.path)[0], "old-secret")

    def test_offline_logout_backup_failure_keeps_saved_session(self) -> None:
        self.path.write_bytes(b"saved login")
        with patch.dict(os.environ, {"TDM_ALLOW_LOGOUT": "1"}), patch.object(
            cli_commands, "backup_session", side_effect=OSError(28, "No space left on device")
        ):
            self.assertEqual(cli_commands.run_offline(SimpleNamespace(command="logout", yes=True)), 2)
        self.assertEqual(self.path.read_bytes(), b"saved login")
        self.assertIn("refusing logout", self.errors.getvalue())

    def test_status_format_and_missing_invalid_exit_codes(self) -> None:
        self.assertEqual(self.run_auth("status"), 2)
        auth_session.write_session("secret", "7", ClientType.ANDROID_APP, self.path)
        with patch.object(cli_commands, "validate_token", new_callable=AsyncMock) as validate:
            validate.return_value = dict(client_id=ClientType.ANDROID_APP.CLIENT_ID, login="alice", user_id="7", expires_in=3600)
            self.assertEqual(self.run_auth("status"), 0)
            self.assertIn("client: ANDROID_APP\nlogin: alice\nexpires_in: 3600s\nbackup: none", self.output.getvalue())
            self.assertIn("archives: 0", self.output.getvalue())
            auth_session.backup_session(self.path)
            validate.return_value["expires_in"] = None
            self.assertEqual(self.run_auth("status"), 0)
            self.assertIn("expires_in: never\nbackup: " + str(self.path) + ".bak (", self.output.getvalue())
            validate.side_effect = ValueError("invalid token")
            self.assertEqual(self.run_auth("status"), 2)
            self.path.write_bytes(b"secret-but-corrupt")
            self.assertEqual(self.run_auth("status"), 2)
            self.assertIn("auth error: cannot read the session file", self.errors.getvalue())
        self.assertNotIn("secret", self.output.getvalue() + self.errors.getvalue())

    def test_backup_restore_commands_and_lock(self) -> None:
        self.assertEqual(self.run_auth("backup"), 2)
        auth_session.write_session("original", "1", ClientType.ANDROID_APP, self.path)
        self.assertEqual(self.run_auth("backup"), 0)
        self.path.write_bytes(b"replacement")
        self.assertEqual(self.run_auth("restore"), 0)
        self.assertEqual(auth_session.read_token(self.path)[0], "original")
        self.assertEqual(self.path.with_name("cookies.jar.bak.prev").read_bytes(), b"replacement")
        with patch.object(cli_commands, "lock_file", return_value=(False, self.lock)):
            self.assertEqual(self.run_auth("backup"), 3)
        self.assertIn("The miner is running; use the interactive console instead.", self.errors.getvalue())


class ValidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_sends_oauth_header_and_sanitizes_failure(self) -> None:
        response = AsyncMock()
        response.status = 200
        response.json.return_value = dict(client_id="id", login="alice", user_id="7", expires_in=42)
        context = AsyncMock()
        context.__aenter__.return_value = response
        session = Mock(get=Mock(return_value=context))
        self.assertEqual((await auth_session.validate_token(session, "secret"))["expires_in"], 42)
        self.assertEqual(session.get.call_args.kwargs["headers"], {"Authorization": "OAuth secret"})
        response.status = 401
        with self.assertRaisesRegex(ValueError, "token validation failed \\(HTTP 401\\)") as raised:
            await auth_session.validate_token(session, "secret")
        self.assertNotIn("secret", str(raised.exception))

    async def test_401_backs_up_before_clearing_jar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            auth_session.write_session("old-secret", "7", ClientType.ANDROID_APP, path)
            jar = aiohttp.CookieJar()
            jar.load(path)
            cleared = []
            original_clear = jar.clear_domain

            def clear_domain(host: str) -> None:
                cleared.append(auth_session.read_token(path.with_name("cookies.jar.bak"))[0])
                original_clear(host)

            jar.clear_domain = clear_domain
            session = SimpleNamespace(cookie_jar=jar)
            login = SimpleNamespace(update=Mock())
            gui = SimpleNamespace(login=login, set_logged_in=Mock())
            response = AsyncMock()
            response.status = 401
            valid_response = AsyncMock()
            valid_response.status = 200
            valid_response.json.return_value = dict(client_id=ClientType.ANDROID_APP.CLIENT_ID, user_id="7")
            responses = iter((response, valid_response))

            class Request:
                async def __aenter__(self):
                    return next(responses)

                async def __aexit__(self, *args):
                    return None

            twitch = SimpleNamespace(
                _client_type=ClientType.ANDROID_APP,
                get_session=AsyncMock(return_value=session),
                request=Mock(side_effect=lambda *args, **kwargs: Request()),
                gui=gui,
            )
            state = _AuthState(twitch)
            state.device_id = "device"
            with patch("twitch.COOKIES_PATH", path), patch.object(state, "_oauth_login", new_callable=AsyncMock) as login_request:
                login_request.return_value = "new-secret"
                await state._validate()
            self.assertEqual(cleared, ["old-secret"])
            self.assertEqual(auth_session.read_token(path)[0], "new-secret")

    async def test_backup_failure_preserves_jar_on_401_and_mismatch(self) -> None:
        for status, client_id in ((401, None), (200, ClientType.WEB.CLIENT_ID)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cookies.jar"
                auth_session.write_session("saved-secret", "7", ClientType.ANDROID_APP, path)
                original = path.read_bytes()
                jar = aiohttp.CookieJar()
                jar.load(path)
                response = AsyncMock()
                response.status = status
                response.json.return_value = {"client_id": client_id, "user_id": "7"}
                request = AsyncMock()
                request.__aenter__.return_value = response
                twitch = SimpleNamespace(
                    _client_type=ClientType.ANDROID_APP,
                    get_session=AsyncMock(return_value=SimpleNamespace(cookie_jar=jar)),
                    request=Mock(return_value=request),
                    gui=SimpleNamespace(login=SimpleNamespace(update=Mock()), set_logged_in=Mock()),
                )
                state = _AuthState(twitch)
                state.device_id = "device"
                with patch("twitch.COOKIES_PATH", path), patch(
                    "twitch._ensure_backup", side_effect=OSError(28, "No space left on device")
                ):
                    with self.assertRaisesRegex(
                        LoginException,
                        "cannot back up the saved session; refusing to discard it: No space left on device",
                    ):
                        await state._validate()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(jar.filter_cookies(ClientType.ANDROID_APP.CLIENT_URL)["auth-token"].value, "saved-secret")

    async def test_invalidate_keeps_file_and_jar_when_backup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            auth_session.write_session("saved-secret", "7", ClientType.ANDROID_APP, path)
            jar = aiohttp.CookieJar()
            jar.load(path)
            twitch = SimpleNamespace(_session=SimpleNamespace(cookie_jar=jar), gui=SimpleNamespace(set_logged_in=Mock()))
            state = _AuthState(twitch)
            with patch("twitch.COOKIES_PATH", path), patch(
                "twitch._ensure_backup", side_effect=OSError(28, "No space left on device")
            ):
                state.invalidate(delete_cookies=True)
            self.assertTrue(path.exists())
            self.assertEqual(jar.filter_cookies(ClientType.ANDROID_APP.CLIENT_URL)["auth-token"].value, "saved-secret")
