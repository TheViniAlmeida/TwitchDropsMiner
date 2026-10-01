"""Optional HTTP dashboard for the CLI miner."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import WSMsgType, WSCloseCode, web

from cli_actions import ActionError, ActionRejected
from cli_commands import CommandError
from constants import DATA_DIR
from utils import resource_path


logger = logging.getLogger("TwitchDrops.dashboard")
_CSP = "default-src 'self'; img-src 'self' https://static-cdn.jtvnw.net; connect-src 'self'"
_ORIGIN = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?\Z")


class DashboardError(RuntimeError):
    pass


def is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_ips() -> list[str]:
    """Inspect local interfaces without DNS or outbound packets."""
    addresses: set[str] = set()
    if os.name == "posix":
        try:
            import fcntl

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                for _, name in socket.if_nameindex():
                    try:
                        data = fcntl.ioctl(probe.fileno(), 0x8915,
                                           struct.pack("256s", name[:15].encode()))
                        addresses.add(socket.inet_ntoa(data[20:24]))
                    except OSError:
                        continue
        except OSError:
            pass
        try:
            for line in Path("/proc/net/if_inet6").read_text().splitlines():
                addresses.add(str(ipaddress.IPv6Address(int(line.split()[0], 16))))
        except (OSError, ValueError, IndexError):
            pass
    return sorted(address for address in addresses if not is_loopback(address))


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    readonly: bool = False
    token: str | None = None
    token_file: Path | None = None
    enabled: bool = False
    auth_timeout: float = 5.0
    origins: tuple[str, ...] = ()


def _env_true(value: str | None) -> bool:
    return value is not None and value.casefold() in ("1", "true", "yes", "on")


def resolve_dashboard_config(args: argparse.Namespace, environ: Mapping[str, str] | None = None) -> DashboardConfig:
    env = os.environ if environ is None else environ
    enabled = bool(getattr(args, "dashboard", None) or _env_true(env.get("TDM_DASHBOARD")))
    host = getattr(args, "dashboard_host", None) or env.get("TDM_DASHBOARD_HOST") or "127.0.0.1"
    raw_port = getattr(args, "dashboard_port", None)
    if raw_port is None:
        raw_port = env.get("TDM_DASHBOARD_PORT", "8787")
    try:
        port = int(raw_port)
        if not 0 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentError(None, f"invalid dashboard port: {raw_port}") from exc
    readonly = bool(getattr(args, "dashboard_readonly", None) or _env_true(env.get("TDM_DASHBOARD_READONLY")))
    raw_origins = env.get("TDM_DASHBOARD_ORIGINS", "")
    origins = tuple(item.strip() for item in raw_origins.split(",") if item.strip())
    for origin in origins:
        if not _ORIGIN.fullmatch(origin):
            raise argparse.ArgumentError(None, f"invalid TDM_DASHBOARD_ORIGINS origin: {origin}")
        try:
            parsed = urlsplit(origin)
            valid_port = parsed.port is None or parsed.port > 0
        except ValueError:
            valid_port = False
        hostname = parsed.hostname if valid_port else None
        valid_host = bool(hostname) and (
            ":" in hostname or all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in hostname.split(".")
            )
        )
        if not valid_host:
            raise argparse.ArgumentError(None, f"invalid TDM_DASHBOARD_ORIGINS origin: {origin}")
    if raw_origins.strip() and any(not item.strip() for item in raw_origins.split(",")):
        raise argparse.ArgumentError(None, "invalid TDM_DASHBOARD_ORIGINS: empty origin")
    token = env.get("TDM_DASHBOARD_TOKEN")
    if token == "":
        raise argparse.ArgumentError(None, "TDM_DASHBOARD_TOKEN must not be empty")
    token_file = None
    if enabled and not is_loopback(host) and token is None:
        token_file = DATA_DIR / "dashboard.token"
        try:
            try:
                descriptor = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                token = secrets.token_urlsafe(32)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(token + "\n")
            if token is None:
                if token_file.is_symlink():
                    raise DashboardError(f"Dashboard token path is not a regular file: {token_file}")
                descriptor = os.open(token_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise DashboardError(f"Dashboard token path is not a regular file: {token_file}")
                    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
                        os.fchmod(source.fileno(), 0o600)
                        logger.warning("Dashboard token file permissions tightened: %s", token_file)
                    token = source.read().strip()
                if not token:
                    raise DashboardError(f"Dashboard token file is empty: {token_file}")
        except OSError as exc:
            raise DashboardError(f"Dashboard token file is inaccessible: {token_file}: {exc.strerror}") from exc
        except UnicodeError as exc:
            raise DashboardError(f"Dashboard token file is not UTF-8: {token_file}") from exc
    return DashboardConfig(host, port, readonly, token, token_file, enabled, origins=origins)


class Dashboard:
    def __init__(self, manager: Any, twitch: Any, config: DashboardConfig) -> None:
        self.manager = manager
        self.twitch = twitch
        self.config = config
        self.manager.actions.readonly = config.readonly
        self.port = config.port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._failures: dict[str, deque[float]] = {}
        self.app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[self._headers, self._host, self._origin, self._auth,
                         self._readonly, self._post_type, self._errors],
        )
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/static/{name}", self._static)
        for name in ("state", "channels", "inventory", "games", "settings", "logs",
                     "priority", "exclude"):
            self.app.router.add_get("/api/" + name, self._get)
        for name in ("switch", "reload", "priority", "exclude", "settings", "logout"):
            self.app.router.add_post("/api/" + name, self._post)
        self.app.router.add_get("/api/ws", self._ws)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=0.5)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
            await self._site.start()
            sockets = self._site._server.sockets if self._site._server else []
            self.port = sockets[0].getsockname()[1]
        except (OSError, ValueError) as exc:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            raise DashboardError(f"Dashboard cannot bind {self.config.host}:{self.config.port}: {exc}") from exc
        host = f"[{self.config.host}]" if ":" in self.config.host else self.config.host
        self.manager.print(f"Dashboard: http://{host}:{self.port}/")
        if self.config.token_file is not None:
            self.manager.print(f"Dashboard token file: {self.config.token_file}")
        if not is_loopback(self.config.host):
            if self.config.host in ("0.0.0.0", "::"):
                addresses = _local_ips()
                if addresses:
                    self.manager.print("Dashboard local IPs: " + ", ".join(addresses))
            self.manager.print("Dashboard exposed without TLS; use a reverse proxy for remote access")

    async def stop(self) -> None:
        closes = {asyncio.create_task(ws.close(code=WSCloseCode.GOING_AWAY))
                  for ws in tuple(self._sockets)}
        if closes:
            done, pending = await asyncio.wait(closes, timeout=2)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    logger.warning("Dashboard WebSocket close failed: %s", task.exception())
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _error(self, status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    def _secure_headers(self, response: web.StreamResponse, path: str) -> None:
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

    @web.middleware
    async def _headers(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = self._error(exc.status, exc.reason.lower()) if request.path.startswith("/api/") else exc
        self._secure_headers(response, request.path)
        return response

    @web.middleware
    async def _host(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if is_loopback(self.config.host):
            allowed = {f"{host}:{self.port}" for host in ("localhost", "127.0.0.1", "[::1]")}
            if request.headers.get("Host") not in allowed:
                return self._error(403, "forbidden host")
        return await handler(request)

    @web.middleware
    async def _origin(self, request: web.Request, handler: Any) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        host = request.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}", *self.config.origins}
        if origin is not None and origin not in allowed:
            return self._error(403, "forbidden origin")
        return await handler(request)

    @web.middleware
    async def _post_type(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.method == "POST" and request.content_type != "application/json":
            return self._error(415, "application/json required")
        return await handler(request)

    def _limited(self, remote: str) -> bool:
        now = time.monotonic()
        for key in tuple(self._failures):
            recent = self._failures[key]
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if not recent:
                del self._failures[key]
        return len(self._failures.get(remote, ())) >= 5

    def _failed(self, remote: str) -> None:
        if remote not in self._failures and len(self._failures) >= 1024:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(remote, deque(maxlen=6)).append(time.monotonic())

    def _matches(self, candidate: str) -> bool:
        token = self.config.token
        return token is not None and hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.config.token is not None and request.path.startswith("/api/") and request.path != "/api/ws":
            remote = request.remote or "unknown"
            header = request.headers.get("Authorization", "")
            if not (header.startswith("Bearer ") and self._matches(header[7:])):
                if self._limited(remote):
                    return self._error(429, "too many authentication attempts")
                self._failed(remote)
                return self._error(401, "unauthorized")
        return await handler(request)

    @web.middleware
    async def _readonly(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.config.readonly and request.method == "POST":
            return self._error(403, "readonly")
        return await handler(request)

    @web.middleware
    async def _errors(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except ActionRejected as exc:
            return self._error(409, self._clean(str(exc)))
        except (ActionError, CommandError, ValueError) as exc:
            return self._error(400, self._clean(str(exc)))
        except web.HTTPException:
            raise
        except Exception as exc:
            safe_error = RuntimeError("internal error")
            logger.exception("Dashboard request failed", exc_info=(RuntimeError, safe_error, exc.__traceback__))
            return self._error(500, "internal error")

    def _clean(self, value: str) -> str:
        token = self.config.token
        return value.replace(token, "<redacted>") if token else value

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return await self._file("index.html")

    async def _static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if not name or name.startswith(".") or Path(name).name != name or "\\" in name:
            raise web.HTTPNotFound()
        return await self._file(name)

    async def _file(self, name: str) -> web.StreamResponse:
        path = resource_path(Path("web") / name)
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _get(self, request: web.Request) -> web.Response:
        action = request.path.rsplit("/", 1)[-1]
        actions = self.manager.actions
        if action == "inventory":
            all_value = request.query.get("all", "0")
            if all_value not in ("0", "1"):
                raise ActionError("all must be 0 or 1")
            result = actions.inventory(all=all_value == "1")
        elif action == "priority":
            result = actions.priority("list")
        elif action == "exclude":
            result = actions.exclude("list")
        elif action == "logs":
            try:
                tail = int(request.query.get("tail", "100"))
            except ValueError as exc:
                raise ActionError("tail must be an integer") from exc
            if not 0 <= tail <= 1000:
                raise ActionError("tail must be between 0 and 1000")
            result = self.manager.log_tail(tail)
        else:
            result = getattr(actions, action)()
        return web.json_response(self._sanitize(result))

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._clean(value)
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: self._sanitize(item) for key, item in value.items()}
        return value

    async def _post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ActionError("invalid JSON") from exc
        if not isinstance(data, dict):
            raise ActionError("JSON object required")
        action = request.path.rsplit("/", 1)[-1]
        actions = self.manager.actions
        if action == "switch":
            result = actions.switch(data.get("channel"))
        elif action == "reload":
            result = actions.reload()
        elif action == "priority":
            result = actions.priority(data.get("op"), data.get("game", ""), data.get("pos"))
        elif action == "exclude":
            result = actions.exclude(data.get("op"), data.get("game", ""))
        elif action == "settings":
            result = actions.set_setting(data.get("key"), data.get("value"))
        else:
            if data.get("confirm") is not True:
                raise ActionError("logout requires confirm: true")
            result = await actions.logout()
        return web.json_response(self._sanitize(result))

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        remote = request.remote or "unknown"
        ws = web.WebSocketResponse(heartbeat=20)
        self._secure_headers(ws, request.path)
        await ws.prepare(request)
        self._sockets.add(ws)
        listener = None
        sender = None
        try:
            if self.config.token is not None:
                try:
                    message = await ws.receive(timeout=self.config.auth_timeout)
                    data = json.loads(message.data) if message.type == WSMsgType.TEXT else None
                except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
                    data = None
                if not isinstance(data, dict) or not isinstance(data.get("auth"), str) or not self._matches(data["auth"]):
                    if not self._limited(remote):
                        self._failed(remote)
                    await ws.close(code=WSCloseCode.POLICY_VIOLATION)
                    return ws
            await ws.send_json({"type": "state", **self._sanitize(self.manager.actions.state())})
            pending_logs: deque[str] = deque(maxlen=100)
            changed = False
            wake = asyncio.Event()

            def on_event(event: str, value: Any) -> None:
                nonlocal changed
                if event == "change":
                    changed = True
                elif event == "log":
                    pending_logs.append(self._clean(str(value)))
                wake.set()

            async def send_events() -> None:
                nonlocal changed
                last_state = time.monotonic()
                while not ws.closed:
                    delay = max(0.0, 1.0 - (time.monotonic() - last_state)) if changed else None
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    wake.clear()
                    if changed and time.monotonic() - last_state >= 1.0:
                        changed = False
                        await ws.send_json({"type": "state", **self._sanitize(self.manager.actions.state())})
                        last_state = time.monotonic()
                    for _ in range(min(len(pending_logs), 50)):
                        await ws.send_json({"type": "log", "line": pending_logs.popleft()})
                    if pending_logs:
                        wake.set()

            listener = on_event
            self.manager.subscribe(listener)
            sender = asyncio.create_task(send_events())
            async for _message in ws:
                pass
        finally:
            if listener is not None:
                self.manager.unsubscribe(listener)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self._sockets.discard(ws)
        return ws
