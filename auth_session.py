from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
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


_TOKEN_HOSTS = (ClientType.ANDROID_APP.CLIENT_URL.host, ClientType.MOBILE_WEB.CLIENT_URL.host)


def _jar_token(jar: aiohttp.CookieJar) -> str | None:
    for host in _TOKEN_HOSTS:
        if host is not None:
            token = jar.filter_cookies(URL.build(scheme="https", host=host)).get("auth-token")
            if token is not None and token.value:
                return token.value
    return None


def save_jar(jar: aiohttp.CookieJar, path: Path = COOKIES_PATH) -> None:
    """Atomically save the jar; never drop a saved session without a backup of it."""
    path = Path(path)
    existing = path.is_file() and path.stat().st_size > 0
    saved = _file_token(path) if existing else None
    # an unreadable file may still hold a valid session: keep a copy before replacing it
    if existing and (saved is None or _jar_token(jar) != saved):
        # raises OSError when the backup cannot be written: the saved file stays untouched
        backup_session(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _private(descriptor)
        finally:
            os.close(descriptor)
        jar.save(temporary)
        with open(temporary, "rb") as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _file_token(path: Path) -> str | None:
    try:
        return read_token(path)[0]
    except Exception:
        return None


def _same_token(source: Path, backup: Path) -> bool:
    token = _file_token(source)
    return token is not None and token == _file_token(backup)


def _archives(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak.[0-9]*"),
                  key=lambda archive: archive.stat().st_mtime, reverse=True)


def _archive(source: Path, path: Path) -> None:
    """Move source into a timestamped archive, unless that session is already archived.

    Archives are deduplicated by token and never pruned: each one is a distinct session.
    """
    token = _file_token(source)
    if token is not None and any(_file_token(archive) == token for archive in _archives(path)):
        source.unlink()
        return
    archive = _archive_path(source, path)
    os.replace(source, archive)
    os.chmod(archive, 0o600)


def _archive_path(source: Path, path: Path) -> Path:
    base = path.with_name(path.name + ".bak")
    timestamp = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = base.with_name(f"{base.name}.{timestamp}")
    suffix = 1
    while archive.exists():
        archive = base.with_name(f"{base.name}.{timestamp}.{suffix}")
        suffix += 1
    return archive


def archive_count(path: Path = COOKIES_PATH) -> int:
    return len(_archives(Path(path)))


def backup_session(path: Path = COOKIES_PATH) -> Path | None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    if _file_token(path) is None:
        # unreadable or tokenless: keep a copy, but never let it replace a usable .bak
        return _copy_atomic(path, _archive_path(path, path))
    backup = path.with_name(path.name + ".bak")
    if backup.exists() and not _same_token(path, backup):
        _archive(backup, path)
    return _copy_atomic(path, backup)


def ensure_backup(path: Path = COOKIES_PATH) -> Path | None:
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if backup.exists() and _same_token(path, backup):
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
    if _file_token(backup) is None:
        raise ValueError("the saved session backup is unreadable; nothing was changed")
    previous = path.with_name(path.name + ".bak.prev")
    if previous.is_file() and not _same_token(path, previous) and not _same_token(backup, previous):
        # a second restore must not lose the session kept by the first one
        _archive(previous, path)
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
        try:
            jar.load(jar_path)
        except OSError:
            raise
        except Exception:
            # malformed jar: never echo its content
            raise ValueError("cannot read the session file") from None
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
        save_jar(jar, path)


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
