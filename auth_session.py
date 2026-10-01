from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import aiohttp
from yarl import URL

from constants import COOKIES_PATH, ClientType

if TYPE_CHECKING:
    from constants import ClientInfo


def _private(descriptor: int) -> None:
    # os.fchmod is missing on Windows before Python 3.13; mkstemp files are already private there
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)


def _copy_atomic(source: Path, destination: Path) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            _private(target.fileno())
            while chunk := original.read(1024 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def backup_session(path: Path = COOKIES_PATH) -> Path | None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return _copy_atomic(path, path.with_name(path.name + ".bak"))


def ensure_backup(path: Path = COOKIES_PATH) -> Path | None:
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        return backup
    return backup_session(path)


def backup_previous(path: Path = COOKIES_PATH) -> Path | None:
    """Keep the session about to be replaced in cookies.jar.bak.prev."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return _copy_atomic(path, path.with_name(path.name + ".bak.prev"))


def restore_session(path: Path = COOKIES_PATH) -> tuple[Path, Path]:
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not backup.is_file() or backup.stat().st_size == 0:
        raise ValueError("no saved session backup")
    previous = path.with_name(path.name + ".bak.prev")
    backup_previous(path)
    _copy_atomic(backup, path)
    return path, previous


@contextmanager
def _cookie_jar() -> Iterator[aiohttp.CookieJar]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            yield aiohttp.CookieJar(loop=loop)
        finally:
            loop.close()
    else:
        yield aiohttp.CookieJar(loop=loop)


def read_token(jar_path: Path) -> tuple[str, str]:
    with _cookie_jar() as jar:
        jar.load(jar_path)
        hosts = (ClientType.ANDROID_APP.CLIENT_URL.host, ClientType.MOBILE_WEB.CLIENT_URL.host)
        for host in hosts:
            if host is None:
                continue
            token = jar.filter_cookies(URL.build(scheme="https", host=host)).get("auth-token")
            if token is not None:
                return token.value, host
    raise ValueError("no auth-token in saved session")


def write_session(token: str, user_id: str, client: ClientInfo, path: Path = COOKIES_PATH) -> None:
    path = Path(path)
    with _cookie_jar() as jar:
        jar.update_cookies({"auth-token": token, "persistent": str(user_id)}, client.CLIENT_URL)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            _private(descriptor)
            os.close(descriptor)
            jar.save(temporary)
            with open(temporary, "rb") as saved:
                os.fsync(saved.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


async def validate_token(session: aiohttp.ClientSession, token: str, *, proxy: URL | None = None) -> dict:
    kwargs = {"proxy": str(proxy)} if proxy else {}
    async with session.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
        **kwargs,
    ) as response:
        if response.status != 200:
            raise ValueError(f"token validation failed (HTTP {response.status})")
        data = await response.json()
        if not isinstance(data, dict) or not all(
            key in data for key in ("client_id", "login", "user_id", "expires_in")
        ):
            raise ValueError("invalid token validation response")
        return {key: data[key] for key in ("client_id", "login", "user_id", "expires_in")}


def client_name(client_id: str) -> str | None:
    return next(
        (name for name in ("WEB", "MOBILE_WEB", "ANDROID_APP", "SMARTBOX")
         if getattr(ClientType, name).CLIENT_ID == client_id),
        None,
    )


def logout_allowed() -> bool:
    return os.environ.get("TDM_ALLOW_LOGOUT", "").casefold() in ("1", "true", "yes", "on")


def logout_disabled_message(client: ClientInfo = ClientType.ANDROID_APP) -> str:
    name = client_name(client.CLIENT_ID) or "unknown"
    return (
        f"logout disabled: Twitch blocks new device logins for {name}; "
        "this session cannot be recreated. Set TDM_ALLOW_LOGOUT=1 to allow it."
    )
