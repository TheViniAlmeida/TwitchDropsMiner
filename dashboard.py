"""Optional HTTP dashboard for the CLI miner."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
import errno
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
from private_file import restrict_to_owner
from version import __version__


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


# EADDRINUSE/EACCES plus their Windows socket codes (WSAEADDRINUSE, WSAEACCES)
_PORT_BUSY = (errno.EADDRINUSE, errno.EACCES, 10048, 10013)


def _filter_ips(entries: list[tuple[str, str]]) -> list[str]:
    addresses = set()
    for ifname, address in entries:
        if ifname.startswith(("docker", "br-", "veth", "virbr", "cni", "flannel")):
            continue
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or isinstance(ip, ipaddress.IPv6Address) and (
            ip.is_link_local or not ip.is_global
        ):
            continue
        if isinstance(ip, ipaddress.IPv4Address) and not ip.is_private:
            continue
        addresses.add(str(ip))
    return sorted(addresses)


def _interface_entries() -> list[tuple[str, str]]:
    """Inspect local interfaces without DNS or outbound packets."""
    entries: list[tuple[str, str]] = []
    if os.name == "posix":
        try:
            import fcntl

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                for _, name in socket.if_nameindex():
                    try:
                        data = fcntl.ioctl(probe.fileno(), 0x8915,
                                           struct.pack("256s", name[:15].encode()))
                        entries.append((name, socket.inet_ntoa(data[20:24])))
                    except OSError:
                        continue
        except OSError:
            pass
        try:
            for line in Path("/proc/net/if_inet6").read_text().splitlines():
                fields = line.split()
                entries.append((fields[-1], str(ipaddress.IPv6Address(int(fields[0], 16)))))
        except (OSError, ValueError, IndexError):
            pass
    return entries


def _local_ips() -> list[str]:
    return _filter_ips(_interface_entries())


def _host_names(bound: str, origins: tuple[str, ...]) -> set[str]:
    """Host header names that reach this dashboard; anything else may be DNS rebinding."""
    names = {"localhost", "127.0.0.1", "::1", bound.casefold()}
    if not is_loopback(bound):
        names.update(address for _, address in _interface_entries())
        hostname = socket.gethostname().casefold()
        names.update((hostname, f"{hostname}.local"))
        try:
            # Windows has no interface listing above; resolving our own name stays local
            names.update(socket.gethostbyname_ex(hostname)[2])
        except OSError:
            pass
        names.update(urlsplit(origin).hostname or "" for origin in origins)
    names.discard("0.0.0.0")
    names.discard("::")
    return {name.casefold() for name in names if name}


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 23450
    readonly: bool = False
    token: str | None = None
    token_file: Path | None = None
    enabled: bool = False
    auth_timeout: float = 5.0
    origins: tuple[str, ...] = ()
    port_range: tuple[int, int] | None = None
    history_interval: float = 60.0


def _env_true(value: str | None) -> bool:
    return value is not None and value.casefold() in ("1", "true", "yes", "on")


def resolve_dashboard_config(args: argparse.Namespace, environ: Mapping[str, str] | None = None) -> DashboardConfig:
    env = os.environ if environ is None else environ
    enabled = bool(getattr(args, "dashboard", None) or _env_true(env.get("TDM_DASHBOARD")))
    host = getattr(args, "dashboard_host", None) or env.get("TDM_DASHBOARD_HOST") or "127.0.0.1"
    raw_port = getattr(args, "dashboard_port", None)
    if raw_port is None:
        raw_port = env.get("TDM_DASHBOARD_PORT", "23450-23500")
    try:
        parts = str(raw_port).split("-")
        if len(parts) == 1 and re.fullmatch(r"\d+", parts[0]):
            port = int(parts[0])
            port_range = None
            if port > 65535:
                raise ValueError
        elif len(parts) == 2 and all(re.fullmatch(r"\d+", part) for part in parts):
            start, end = map(int, parts)
            if not 1 <= start <= end <= 65535:
                raise ValueError
            port, port_range = start, (start, end)
        else:
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
        # an empty value must not silently disable the authentication the operator asked for
        raise argparse.ArgumentError(None, "TDM_DASHBOARD_TOKEN must not be empty")
    token_file = None
    requested_file = getattr(args, "dashboard_token_file", None)
    if requested_file is None:
        requested_file = env.get("TDM_DASHBOARD_TOKEN_FILE")
    if requested_file is not None and token is not None:
        logger.warning("Dashboard token file ignored: TDM_DASHBOARD_TOKEN is set")
    elif requested_file is not None:
        token_file = DATA_DIR / "dashboard.token" if requested_file == "" else Path(requested_file)
        try:
            try:
                descriptor = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    # before the token exists: Windows ignores the 0o600 above
                    restrict_to_owner(token_file)
                except OSError:
                    os.close(descriptor)
                    token_file.unlink(missing_ok=True)
                    raise
                token = secrets.token_urlsafe(32)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(token + "\n")
            if token is None:
                if token_file.is_symlink():
                    raise DashboardError(f"Dashboard token path is not a regular file: {token_file}")
                # an existing file may carry a looser ACL: tighten it before reading the token
                restrict_to_owner(token_file)
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
    return DashboardConfig(host, port, readonly, token, token_file, enabled,
                           origins=origins, port_range=port_range)


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
        self._hosts: set[str] | None = None
        self._pending_auth: dict[str, int] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=1440)
        self._history_task: asyncio.Task[None] | None = None
        self._history_errors: set[str] = set()
        self.app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[self._headers, self._host, self._origin, self._auth,
                         self._readonly, self._post_type, self._errors],
        )
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/static/{name}", self._static)
        for name in ("state", "channels", "inventory", "games", "settings", "logs",
                     "priority", "exclude", "meta", "campaigns", "drops", "game",
                     "game_choices", "progress", "history"):
            self.app.router.add_get("/api/" + name, self._get)
        self.app.router.add_get("/api/settings/schema", self._get)
        for name in ("switch", "reload", "priority", "exclude", "settings", "logout"):
            self.app.router.add_post("/api/" + name, self._post)
        self.app.router.add_get("/api/ws", self._ws)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=0.5)
        try:
            await self._runner.setup()
            ports = (range(self.config.port_range[0], self.config.port_range[1] + 1)
                     if self.config.port_range else (self.config.port,))
            for port in ports:
                self._site = web.TCPSite(self._runner, self.config.host, port)
                try:
                    await self._site.start()
                except OSError as exc:
                    server = self._site._server
                    await self._site.stop()
                    if server is not None:
                        await server.wait_closed()
                    self._site = None
                    if self.config.port_range and exc.errno in _PORT_BUSY:
                        continue
                    raise
                sockets = self._site._server.sockets if self._site._server else []
                self.port = sockets[0].getsockname()[1]
                break
            else:
                start, end = self.config.port_range
                raise DashboardError(f"Dashboard cannot bind {self.config.host}: no free port in {start}-{end}")
        except (OSError, ValueError) as exc:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            raise DashboardError(f"Dashboard cannot bind {self.config.host}:{self.config.port}: {exc}") from exc
        except DashboardError:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            raise
        self._sample_history()
        self._history_task = asyncio.create_task(self._sample_history_loop())
        host = f"[{self.config.host}]" if ":" in self.config.host else self.config.host
        self.manager.print(f"Dashboard: http://{host}:{self.port}/")
        if self.config.token_file is not None:
            self.manager.print(f"Dashboard token file: {self.config.token_file}")
        if not is_loopback(self.config.host):
            if self.config.token is None:
                warning = "dashboard exposed WITHOUT authentication: anyone on this network can control the miner"
                self.manager.print(warning)
                logger.warning(warning)
            if self.config.host in ("0.0.0.0", "::"):
                addresses = _local_ips()
                if addresses:
                    self.manager.print("Dashboard local IPs: " + ", ".join(addresses))
            self.manager.print("Dashboard exposed without TLS; use a reverse proxy for remote access")

    async def stop(self) -> None:
        if self._history_task is not None:
            self._history_task.cancel()
            await asyncio.gather(self._history_task, return_exceptions=True)
            self._history_task = None
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
        self._site = None

    def _sample_history(self) -> None:
        try:
            current = self.manager.actions.progress() or {}
            active = self.manager.actions.state().get("current_drop") or {}
            self.history.append({
                "t": int(time.time()), "drop_id": current.get("id"),
                "campaign": current.get("campaign"),
                "progress": max(0.0, min(1.0, float(current.get(
                    "progress", active.get("progress")) or 0))),
                "remaining_minutes": current.get(
                    "remaining_minutes", active.get("remaining_minutes")),
                "claimed": current.get("claimed", 0), "total": current.get("total", 0),
            })
        except Exception as exc:
            message = self._clean(str(exc))
            if message not in self._history_errors:
                self._history_errors.add(message)
                logger.warning("Dashboard history sampling failed: %s", message)

    async def _sample_history_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.history_interval)
            self._sample_history()

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

    def _host_allowed(self, header: str | None) -> bool:
        if not header:
            return False
        try:
            url = urlsplit(f"http://{header}")
            name, port = url.hostname or "", url.port
        except ValueError:
            return False
        if url.username is not None or url.path or url.query or url.fragment:
            return False
        if port is not None and port != self.port:
            # a port-less Host is only valid behind a configured reverse proxy origin
            return False
        if port is None and not any(
            urlsplit(origin).hostname == name for origin in self.config.origins
        ):
            return False
        if self._hosts is None or name not in self._hosts:
            # interfaces change (DHCP, VPN): refresh before refusing
            self._hosts = _host_names(self.config.host, self.config.origins)
        return name in self._hosts

    @web.middleware
    async def _host(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if not self._host_allowed(request.headers.get("Host")):
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
        if (self.config.token is not None and request.path.startswith("/api/")
                and request.path not in ("/api/ws", "/api/meta")):
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
        if request.path == "/api/settings/schema":
            result = actions.settings_schema()
        elif action == "meta":
            result = {"auth": self.config.token is not None,
                      "readonly": self.config.readonly, "version": __version__}
        elif action == "state":
            result = self._state()
        elif action == "history":
            since = request.query.get("since")
            try:
                timestamp = int(since) if since is not None else None
            except ValueError as exc:
                raise ActionError("since must be a unix timestamp") from exc
            result = [item for item in self.history if timestamp is None or item["t"] >= timestamp]
        elif action == "campaigns":
            options = {}
            for key in ("all", "not_linked", "upcoming", "expired", "excluded", "finished"):
                value = request.query.get(key)
                if value is not None:
                    if value not in ("0", "1"):
                        raise ActionError(f"{key} must be 0 or 1")
                    options[key] = value == "1"
            result = actions.campaigns(
                {key: value for key, value in options.items() if key != "all"},
                game=request.query.get("game"), include_all=options.get("all", False))
        elif action == "drops":
            target = request.query.get("target")
            if not target:
                raise ActionError("target is required")
            result = actions.drops(target)
        elif action == "game":
            name = request.query.get("name")
            if not name:
                raise ActionError("name is required")
            result = actions.game(name)
        elif action == "inventory":
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

    def _state(self) -> dict[str, Any]:
        return {**self.manager.actions.state(), "auth": self.config.token is not None,
                "readonly": self.config.readonly}

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
        # the token arrives after the upgrade, so a limited address (maybe a shared NAT with a
        # valid user) keeps one pending handshake instead of being locked out
        pending_cap = 1 if self._limited(remote) else 4
        if self.config.token is not None and self._pending_auth.get(remote, 0) >= pending_cap:
            return self._error(429, "too many attempts")
        ws = web.WebSocketResponse(heartbeat=20)
        self._secure_headers(ws, request.path)
        await ws.prepare(request)
        self._sockets.add(ws)
        listener = None
        sender = None
        try:
            if self.config.token is not None:
                self._pending_auth[remote] = self._pending_auth.get(remote, 0) + 1
                try:
                    message = await ws.receive(timeout=self.config.auth_timeout)
                    data = json.loads(message.data) if message.type == WSMsgType.TEXT else None
                except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
                    data = None
                finally:
                    if self._pending_auth[remote] <= 1:
                        del self._pending_auth[remote]
                    else:
                        self._pending_auth[remote] -= 1
                if not isinstance(data, dict) or not isinstance(data.get("auth"), str) or not self._matches(data["auth"]):
                    if not self._limited(remote):
                        self._failed(remote)
                    await ws.close(code=WSCloseCode.POLICY_VIOLATION)
                    return ws
            await ws.send_json({"type": "state", **self._sanitize(self._state())})
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
                        await ws.send_json({"type": "state", **self._sanitize(self._state())})
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
