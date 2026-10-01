from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hmac
import json
import os
import secrets
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Iterator


MAX_REQUEST = 16384
MAX_LINE = 16384


class ControlUnavailable(ConnectionError):
    pass


# sun_path is 108 bytes on Linux and 104 on macOS/BSD
_SUN_PATH_MAX = 100


@contextmanager
def _unix_path(path: Path) -> Iterator[str]:
    """Yield a path short enough for AF_UNIX bind/connect.

    Long DATA_DIR paths are reached through /proc/self/fd/<dir fd> on Linux.
    """
    if len(os.fsencode(path)) <= _SUN_PATH_MAX or not Path("/proc/self/fd").is_dir():
        yield str(path)
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        yield f"/proc/self/fd/{descriptor}/{path.name}"
    finally:
        os.close(descriptor)


def _packet(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


async def _read_packet(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        raw = await reader.readline()
        if not raw:
            return None
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            raise ValueError("request too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (UnicodeError, json.JSONDecodeError, asyncio.LimitOverrunError) as exc:
        raise ValueError("invalid JSON request") from exc


class ControlServer:
    def __init__(self, manager: Any, data_dir: Path, *, tcp: bool | None = None) -> None:
        self.manager = manager
        self.data_dir = Path(data_dir)
        self.tcp = sys.platform == "win32" if tcp is None else tcp
        self.path = self.data_dir / ("control.json" if self.tcp else "control.sock")
        self.server: asyncio.AbstractServer | None = None
        self.token: str | None = None
        self._clients: set[asyncio.Task[Any]] = set()
        self._owned_inode: int | None = None

    async def start(self) -> None:
        if self.tcp:
            if self.path.exists():
                if not stat.S_ISREG(self.path.lstat().st_mode):
                    raise RuntimeError("control.json exists but is not a regular file")
                try:
                    reader, writer = await open_control(self.data_dir, tcp=True)
                except ControlUnavailable:
                    self.path.unlink()
                else:
                    writer.close()
                    await writer.wait_closed()
                    raise RuntimeError("control server already running")
            self.token = secrets.token_urlsafe(32)
            self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=MAX_REQUEST + 1)
            port = self.server.sockets[0].getsockname()[1]
            created = False
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"port": port, "token": self.token, "pid": os.getpid()}, stream)
                os.chmod(self.path, 0o600)
                self._owned_inode = self.path.stat().st_ino
            except BaseException:
                self.server.close()
                await self.server.wait_closed()
                if created:
                    self.path.unlink(missing_ok=True)
                raise
        else:
            try:
                mode = self.path.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError("control.sock exists but is not a socket")
                try:
                    with _unix_path(self.path) as unix_path:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_unix_connection(unix_path), timeout=1
                        )
                except PermissionError as exc:
                    raise RuntimeError("control socket is not accessible") from exc
                except (OSError, asyncio.TimeoutError):
                    self.path.unlink()
                else:
                    writer.close()
                    await writer.wait_closed()
                    raise RuntimeError("control server already running")
            previous_umask = os.umask(0o177)
            try:
                with _unix_path(self.path) as unix_path:
                    self.server = await asyncio.start_unix_server(
                        self._serve, path=unix_path, limit=MAX_REQUEST + 1
                    )
                os.chmod(self.path, 0o600)
                self._owned_inode = self.path.stat().st_ino
            finally:
                os.umask(previous_umask)

    async def stop(self) -> None:
        try:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
                self.server = None
            for task in tuple(self._clients):
                task.cancel()
            if self._clients:
                await asyncio.gather(*self._clients, return_exceptions=True)
        finally:
            try:
                if self._owned_inode is not None and self.path.stat().st_ino == self._owned_inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._owned_inode = None

    async def _serve(self, reader: asyncio.StreamReader, stream: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)

        def send(line: str) -> None:
            safe_line = line.encode("utf-8")[:MAX_LINE].decode("utf-8", errors="ignore")
            stream.write(_packet({"line": safe_line}))

        try:
            if self.tcp:
                try:
                    auth = await asyncio.wait_for(_read_packet(reader), timeout=5)
                except ValueError:
                    stream.write(_packet({"done": True, "ok": False}))
                    await stream.drain()
                    return
                if not auth or not isinstance(auth.get("auth"), str) or not hmac.compare_digest(
                    auth["auth"], self.token or ""
                ):
                    stream.write(_packet({"done": True, "ok": False}))
                    await stream.drain()
                    return
                stream.write(_packet({"auth_ok": True}))
                await stream.drain()
            while True:
                try:
                    request = await _read_packet(reader)
                except ValueError as exc:
                    send(f"error: {exc}")
                    stream.write(_packet({"done": True, "ok": False}))
                    await stream.drain()
                    if str(exc) == "request too large":
                        break
                    continue
                if request is None:
                    break
                command = request.get("cmd")
                if not isinstance(command, str) or len(command) > MAX_REQUEST:
                    send("error: expected a command string")
                    stream.write(_packet({"done": True, "ok": False}))
                    await stream.drain()
                    continue
                try:
                    parts = shlex.split(command)
                except ValueError:
                    parts = []
                if parts and parts[0].casefold() == "stop":
                    send("error: no active watch")
                    ok = False
                else:
                    ok = await self.manager.dispatch_command(
                        command, writer=send, confirm=request.get("confirm") is True
                    )
                    if ok and parts and parts[0].casefold() == "watch":
                        await stream.drain()
                        while True:
                            try:
                                next_request = await asyncio.wait_for(_read_packet(reader), timeout=1)
                            except asyncio.TimeoutError:
                                await stream.drain()
                                continue
                            except ValueError:
                                send("error: invalid watch request")
                                ok = False
                                break
                            if next_request is None:
                                return
                            if next_request.get("cmd") == "stop":
                                break
                            send("error: send stop before another command")
                            ok = False
                            break
                        self.manager.stop_remote_watch(send)
                stream.write(_packet({"done": True, "ok": ok}))
                await stream.drain()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            self.manager.stop_remote_watch(send)
            stream.close()
            try:
                await stream.wait_closed()
            except (ConnectionError, OSError):
                pass
            if task is not None:
                self._clients.discard(task)


async def open_control(
    data_dir: Path, *, tcp: bool | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    tcp = sys.platform == "win32" if tcp is None else tcp
    try:
        if tcp:
            path = Path(data_dir) / "control.json"
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("invalid control file")
            info = json.loads(path.read_text(encoding="utf-8"))
            port, token = info["port"], info["token"]
            if not isinstance(port, int) or not 0 < port < 65536 or not isinstance(token, str):
                raise ValueError("invalid control file")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port, limit=MAX_REQUEST + 1), timeout=2
            )
            writer.write(_packet({"auth": token}))
            await writer.drain()
            reply = await asyncio.wait_for(_read_packet(reader), timeout=2)
            if reply is None or reply.get("auth_ok") is not True:
                writer.close()
                await writer.wait_closed()
                raise ValueError("control authentication failed")
        else:
            with _unix_path(Path(data_dir) / "control.sock") as unix_path:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(unix_path, limit=MAX_REQUEST + 1),
                    timeout=2,
                )
        return reader, writer
    except (OSError, ValueError, KeyError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
        raise ControlUnavailable("no running miner control endpoint") from exc


async def send_command(
    data_dir: Path, command: str, *, confirm: bool = False,
    output: Callable[[str], None] = print, tcp: bool | None = None,
) -> int:
    reader, writer = await open_control(data_dir, tcp=tcp)
    try:
        writer.write(_packet({"cmd": command, "confirm": confirm}))
        await writer.drain()
        while True:
            response = await _read_packet(reader)
            if response is None:
                raise ControlUnavailable("control connection closed")
            if "line" in response:
                output(response["line"])
            if response.get("done") is True:
                return 0 if response.get("ok") is True else 1
    except (OSError, ValueError) as exc:
        raise ControlUnavailable("control connection closed") from exc
    finally:
        writer.close()
        await writer.wait_closed()


def quote_command(parts: list[str]) -> str:
    return shlex.join(parts)
