from __future__ import annotations

import asyncio
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

from yarl import URL
import aiohttp

from auth_session import (
    archive_count, backup_session, client_name, logout_allowed,
    install_session, logout_disabled_message, read_token, restore_session, validate_token,
)

from constants import COOKIES_PATH, DATA_DIR, LOCK_PATH, ClientType, PriorityMode
from settings import Settings
from translate import _
from utils import lock_file


EDITABLE_KEYS = (
    "proxy",
    "language",
    "connection_quality",
    "priority_mode",
    "enable_badges_emotes",
    "available_drops_check",
    "tray_notifications",
)
GUI_ONLY_KEYS = ("dark_mode", "autostart_tray")
SETTING_KEYS = EDITABLE_KEYS + GUI_ONLY_KEYS
BOOL_VALUES = {
    "true": True,
    "1": True,
    "yes": True,
    "on": True,
    "false": False,
    "0": False,
    "no": False,
    "off": False,
}


class CommandError(ValueError):
    pass


def _remote_command(args: Any) -> list[str]:
    if args.command == "settings":
        if args.settings_command == "show":
            return ["settings"]
        if args.settings_command == "get":
            return ["get", args.key]
        return ["set", args.key, args.value]
    if args.command == "logout":
        return ["logout"]
    action = getattr(args, f"{args.command}_command")
    return [args.command, action] + (
        [args.game] if action in ("add", "remove", "move") else []
    ) + ([args.position] if action == "move" else [])


def run_control_client(args: Any) -> int:
    from control import ControlUnavailable, open_control, quote_command, send_command

    async def probe() -> None:
        reader, writer = await open_control(DATA_DIR)
        writer.close()
        await writer.wait_closed()

    def confirm_logout(words: list[str]) -> bool:
        if not words or words[0] != "logout":
            return True
        if not logout_allowed():
            # gate here too: an older miner on the other end may not refuse the logout itself
            print(logout_disabled_message(), file=sys.stderr)
            return False
        if args.y:
            return True
        if not sys.stdin.isatty():
            print("logout requires -y when stdin is not a TTY", file=sys.stderr)
            return False
        return input("Back up and remove saved login? [y/N] ").casefold() == "y"

    try:
        asyncio.run(probe())
        if args.words:
            if not confirm_logout(args.words):
                return 2
            return asyncio.run(send_command(DATA_DIR, quote_command(args.words), confirm=args.y or args.words[0] == "logout"))
        while True:
            try:
                line = input("ctl> ")
            except EOFError:
                return 0
            if line.strip() in ("quit", "exit"):
                return 0
            if not line.strip():
                continue
            import shlex

            try:
                words = shlex.split(line)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                continue
            if not confirm_logout(words):
                continue
            asyncio.run(send_command(DATA_DIR, line, confirm=bool(words and words[0] == "logout")))
    except ControlUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def _forward_offline(args: Any) -> int:
    from control import ControlUnavailable, quote_command, send_command

    if args.command == "logout" and not logout_allowed():
        # gate here too: an older miner on the other end may not refuse the logout itself
        print(logout_disabled_message(), file=sys.stderr)
        return 2
    if args.command == "logout" and not args.yes:
        if not sys.stdin.isatty():
            print("logout requires --yes when stdin is not a TTY", file=sys.stderr)
            return 2
        if input("Back up and remove saved login? [y/N] ").casefold() != "y":
            print("logout cancelled")
            return 0
    try:
        return asyncio.run(send_command(
            DATA_DIR, quote_command(_remote_command(args)), confirm=args.command == "logout"
        ))
    except ControlUnavailable:
        print("The miner is running without a control endpoint.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


def setting_value(settings: Settings, key: str) -> str:
    if key not in SETTING_KEYS:
        raise CommandError(f"unknown setting: {key}")
    value = getattr(settings, key)
    if key == "proxy" and value:
        proxy = URL(value)
        if proxy.password is not None:
            return str(proxy.with_password("***"))
    if isinstance(value, PriorityMode):
        return value.name.lower()
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def set_setting(settings: Settings, key: str, raw_value: str) -> str | None:
    if key not in SETTING_KEYS:
        raise CommandError(f"unknown setting: {key}")
    value: Any
    if key == "proxy":
        raw_value = raw_value.strip()
        try:
            value = URL(raw_value)
        except ValueError as exc:
            raise CommandError("invalid proxy URL") from exc
        if raw_value and (value.host is None or value.port is None):
            raise CommandError("invalid proxy URL")
    elif key == "language":
        if raw_value not in set(_.languages):
            raise CommandError(f"invalid language: {raw_value}")
        value = raw_value
    elif key == "connection_quality":
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise CommandError("connection_quality must be an integer from 1 to 6") from exc
        if not 1 <= value <= 6:
            raise CommandError("connection_quality must be an integer from 1 to 6")
    elif key == "priority_mode":
        modes = {mode.name.casefold(): mode for mode in PriorityMode}
        try:
            value = modes[raw_value.casefold()]
        except KeyError as exc:
            choices = ", ".join(name.lower() for name in modes)
            raise CommandError(f"invalid priority_mode (use: {choices})") from exc
    else:
        try:
            value = BOOL_VALUES[raw_value.casefold()]
        except KeyError as exc:
            raise CommandError("boolean value must be true/false, 1/0, yes/no, or on/off") from exc
    setattr(settings, key, value)
    settings.alter()
    if key in GUI_ONLY_KEYS:
        return "warning: GUI-only setting"
    return None


def priority_add(settings: Settings, game: str) -> bool:
    if not game:
        raise CommandError("game name is required")
    if game in settings.priority:
        return False
    settings.priority.append(game)
    settings.alter()
    return True


def priority_remove(settings: Settings, game: str) -> bool:
    if not game:
        raise CommandError("game name is required")
    try:
        settings.priority.remove(game)
    except ValueError:
        raise CommandError(f"priority game not found: {game}") from None
    settings.alter()
    return True


def priority_move(settings: Settings, game: str, position: str) -> bool:
    if not game:
        raise CommandError("game name is required")
    try:
        target = int(position)
    except ValueError as exc:
        raise CommandError("position must be a positive integer") from exc
    if not 1 <= target <= len(settings.priority):
        raise CommandError(f"position must be from 1 to {len(settings.priority)}")
    try:
        current = settings.priority.index(game)
    except ValueError:
        raise CommandError(f"priority game not found: {game}") from None
    target -= 1
    if current == target:
        return False
    settings.priority.pop(current)
    settings.priority.insert(target, game)
    settings.alter()
    return True


def exclude_add(settings: Settings, game: str) -> bool:
    if not game:
        raise CommandError("game name is required")
    if game in settings.exclude:
        return False
    settings.exclude.add(game)
    settings.alter()
    return True


def exclude_remove(settings: Settings, game: str) -> bool:
    if not game:
        raise CommandError("game name is required")
    if game not in settings.exclude:
        raise CommandError(f"excluded game not found: {game}")
    settings.exclude.remove(game)
    settings.alter()
    return True


def _print_settings(settings: Settings, output: Callable[[str], None]) -> None:
    for key in SETTING_KEYS:
        output(f"{key} = {setting_value(settings, key)}")
    output("priority = " + ", ".join(settings.priority))
    output("exclude = " + ", ".join(sorted(settings.exclude, key=str.casefold)))


async def _check_token(token: str, proxy: URL | None) -> dict:
    async with aiohttp.ClientSession() as session:
        return await validate_token(session, token, proxy=proxy)


def _run_auth(args: Any) -> int:
    command = args.auth_command
    backup_path = COOKIES_PATH.with_name(COOKIES_PATH.name + ".bak")
    try:
        if command == "backup":
            backup = backup_session(COOKIES_PATH)
            if backup is None:
                print("no saved session", file=sys.stderr)
                return 2
            print(backup)
            return 0
        if command == "restore":
            current, previous = restore_session(COOKIES_PATH)
            print(f"restored {current} from {backup_path}; previous: {previous}")
            return 0
        if command == "status" and not COOKIES_PATH.is_file():
            print("no saved session", file=sys.stderr)
            return 2
        if command == "import":
            if args.from_jar:
                token, _ = read_token(Path(args.from_jar))
            else:
                token = os.environ.get("TDM_AUTH_TOKEN") or (
                    getpass.getpass("Twitch auth token: ") if sys.stdin.isatty()
                    else sys.stdin.readline().strip()
                )
            if not token:
                print("no auth token provided", file=sys.stderr)
                return 2
        else:
            token, _ = read_token(COOKIES_PATH)
        settings = Settings(args)
        data = asyncio.run(_check_token(token, settings.proxy))
        name = client_name(data["client_id"]) or "unknown"
        if command == "status":
            expires = data["expires_in"]
            # Twitch reports 0 for tokens that never expire
            expiration = "never" if not expires else f"{expires}s"
            backup = (
                f"{backup_path} ({datetime.fromtimestamp(backup_path.stat().st_mtime).astimezone().isoformat()})"
                if backup_path.is_file() else "none"
            )
            print(f"client: {name}")
            print(f"login: {data['login']}")
            print(f"expires_in: {expiration}")
            print(f"backup: {backup}")
            print(f"archives: {archive_count(COOKIES_PATH)}")
            return 0
        if data["client_id"] != ClientType.ANDROID_APP.CLIENT_ID:
            print(
                f"token belongs to client {name}; only ANDROID_APP sessions can mine drops today",
                file=sys.stderr,
            )
            return 2
        install_session(token, data["user_id"], ClientType.ANDROID_APP, COOKIES_PATH)
        print(f"imported session for {data['login']} (ANDROID_APP)")
        return 0
    except ValueError as exc:
        # our own messages: never contain the token
        print(f"auth error: {exc}", file=sys.stderr)
        return 2
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"auth error: {type(exc).__name__}", file=sys.stderr)
        return 2


def run_offline(args: Any) -> int:
    locked, lock = lock_file(LOCK_PATH)
    if not locked:
        lock.close()
        if args.command == "auth":
            # the session file belongs to this machine, not to the miner console
            if args.auth_command in ("import", "restore"):
                # the running miner would overwrite the new session when it exits
                print("The miner is running; stop it before replacing the saved session.", file=sys.stderr)
                return 3
            return _run_auth(args)
        return _forward_offline(args)
    try:
        try:
            command = args.command
            if command == "logout":
                if not logout_allowed():
                    print(logout_disabled_message(), file=sys.stderr)
                    return 2
                if not args.yes:
                    if not sys.stdin.isatty():
                        print("logout requires --yes when stdin is not a TTY", file=sys.stderr)
                        return 2
                    answer = input("Back up and remove saved login? [y/N] ")
                    if answer.casefold() != "y":
                        print("logout cancelled")
                        return 0
                if COOKIES_PATH.exists():
                    try:
                        backup = backup_session(COOKIES_PATH)
                    except OSError as exc:
                        print(f"cannot back up the saved session; refusing logout: {exc.strerror or type(exc).__name__}", file=sys.stderr)
                        return 2
                    if backup is None:
                        print("cannot back up the saved session; refusing logout", file=sys.stderr)
                        return 2
                    COOKIES_PATH.unlink()
                    print(f"backed up {COOKIES_PATH} to {backup} and removed login")
                else:
                    print(f"no saved login at {COOKIES_PATH}")
                return 0
            if command == "auth":
                return _run_auth(args)
            try:
                settings = Settings(args)
            except Exception as exc:
                print(f"settings error: {exc}", file=sys.stderr)
                return 4
            if command == "settings":
                if args.settings_command == "show":
                    _print_settings(settings, print)
                elif args.settings_command == "get":
                    print(f"{args.key} = {setting_value(settings, args.key)}")
                else:
                    warning = set_setting(settings, args.key, args.value)
                    settings.save(force=True)
                    if warning:
                        print(warning)
            elif command == "priority":
                if args.priority_command == "list":
                    for index, game in enumerate(settings.priority, 1):
                        print(f"{index}. {game}")
                elif args.priority_command == "add":
                    priority_add(settings, args.game)
                    settings.save(force=True)
                elif args.priority_command == "remove":
                    priority_remove(settings, args.game)
                    settings.save(force=True)
                else:
                    priority_move(settings, args.game, args.position)
                    settings.save(force=True)
            elif command == "exclude":
                if args.exclude_command == "list":
                    for game in sorted(settings.exclude, key=str.casefold):
                        print(game)
                elif args.exclude_command == "add":
                    exclude_add(settings, args.game)
                    settings.save(force=True)
                else:
                    exclude_remove(settings, args.game)
                    settings.save(force=True)
            else:
                raise CommandError(f"unknown offline command: {command}")
        except CommandError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    finally:
        lock.close()
