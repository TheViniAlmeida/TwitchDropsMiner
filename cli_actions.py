from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cli_commands import (
    CommandError,
    EDITABLE_KEYS,
    SETTING_KEYS,
    exclude_add,
    exclude_remove,
    priority_add,
    priority_move,
    priority_remove,
    set_setting,
    setting_value,
)
from constants import COOKIES_PATH, State
from auth_session import ensure_backup, logout_allowed, logout_disabled_message

if TYPE_CHECKING:
    from cli import CLIManager
    from twitch import Twitch


class ActionError(CommandError):
    """Invalid action arguments (HTTP 400 for dashboard callers)."""


class ActionRejected(ActionError):
    """The miner declined an otherwise valid action (HTTP 409)."""


class Actions:
    def __init__(self, twitch: Twitch, manager: CLIManager, *, readonly: bool = False) -> None:
        self.twitch = twitch
        self.manager = manager
        self.readonly = readonly

    def state(self) -> dict[str, Any]:
        state = getattr(self.twitch, "_state", None)
        watched_id = self.manager.channels._watching
        watched = getattr(self.twitch, "channels", self.manager.channels._channels).get(watched_id)
        drop = self.manager._current_drop
        websocket_pool = getattr(self.twitch, "websocket", None)
        if websocket_pool is not None:
            sockets = websocket_pool.websockets
            connected = sum(socket.connected for socket in sockets)
            total = len(sockets)
        else:
            states = self.manager.websockets._states.values()
            connected = sum(
                status is not None and status.casefold() == "connected" for status, _ in states
            )
            total = len(self.manager.websockets._states)
        current_drop = None if drop is None else {
            "game": drop.campaign.game.name,
            "reward": drop.rewards_text(),
            "progress": drop.progress,
            "remaining_minutes": drop.remaining_minutes,
            "timer_seconds": self.manager.progress.seconds,
        }
        return {
            "state": state.name.lower() if state is not None else "unknown",
            "watched_channel": watched.name if watched is not None else None,
            "current_drop": current_drop,
            "websockets": {"connected": connected, "total": total},
            "logged_in": self.manager._logged_in,
            "readonly": self.readonly,
        }

    def channels(self) -> list[dict[str, Any]]:
        values = getattr(self.twitch, "channels", self.manager.channels._channels).values()
        return [
            {
                "name": channel.name,
                "online": channel.online,
                "game": channel.game.name if channel.game is not None else None,
                "viewers": channel.viewers,
                "drops_enabled": channel.drops_enabled,
                "acl_based": channel.acl_based,
                "watching": channel.id == self.manager.channels._watching,
            }
            for channel in values
        ]

    def inventory(self, *, all: bool = False) -> list[dict[str, Any]]:
        campaigns = []
        for campaign in self.manager.inv._campaigns.values():
            if not all and (campaign.finished or campaign.expired):
                continue
            campaigns.append({
                "game": campaign.game.name,
                "name": campaign.name,
                "progress": campaign.progress,
                "claimed_drops": campaign.claimed_drops,
                "total_drops": campaign.total_drops,
                "starts_at": campaign.starts_at.isoformat(),
                "ends_at": campaign.ends_at.isoformat(),
                "image_url": str(campaign.image_url),
                "finished": campaign.finished,
                "expired": campaign.expired,
                "drops": [
                    {
                        "name": drop.name,
                        "reward": drop.rewards_text(),
                        "progress": drop.progress,
                        "current_minutes": drop.current_minutes,
                        "required_minutes": drop.required_minutes,
                        "remaining_minutes": drop.remaining_minutes,
                        "claimed": drop.is_claimed,
                        "starts_at": drop.starts_at.isoformat(),
                        "ends_at": drop.ends_at.isoformat(),
                        "image_url": str(drop.benefits[0].image_url) if drop.benefits else None,
                    }
                    for drop in campaign.drops
                ],
            })
        return campaigns

    def games(self) -> list[str]:
        return sorted((game.name for game in self.manager._games), key=str.casefold)

    def settings(self, *, include_gui_only: bool = False) -> dict[str, str]:
        keys = SETTING_KEYS if include_gui_only else EDITABLE_KEYS
        return {key: setting_value(self.twitch.settings, key) for key in keys}

    def get_setting(self, key: str) -> str:
        try:
            return setting_value(self.twitch.settings, key)
        except CommandError as exc:
            raise ActionError(str(exc)) from exc

    def switch(self, channel: str) -> dict[str, Any]:
        if not isinstance(channel, str) or not channel:
            raise ActionError("channel name is required")
        wanted = channel.casefold()
        selected = next((item for item in getattr(
            self.twitch, "channels", self.manager.channels._channels
        ).values() if item.name.casefold() == wanted), None)
        if selected is None:
            raise ActionError(f"channel not found: {channel}")
        self.manager.channels.select(selected)
        self.twitch.state_change(State.CHANNEL_SWITCH)()
        return {"channel": selected.name}

    def reload(self) -> dict[str, bool]:
        self.twitch.state_change(State.INVENTORY_FETCH)()
        return {"requested": True}

    def priority(self, op: str, game: str = "", pos: str | int | None = None) -> dict[str, Any]:
        settings = self.twitch.settings
        if op == "list":
            return {"priority": list(settings.priority)}
        if not isinstance(game, str):
            raise ActionError("game name is required")
        if op == "add" and pos is None:
            operation = priority_add
            args = (settings, game)
        elif op == "remove" and pos is None:
            operation = priority_remove
            args = (settings, game)
        elif op == "move" and pos is not None:
            operation = priority_move
            args = (settings, game, str(pos))
        else:
            raise ActionError("usage: priority list|add <game>|remove <game>|move <game> <pos>")
        try:
            changed = operation(*args)
        except CommandError as exc:
            raise ActionError(str(exc)) from exc
        if changed:
            self.reload()
        return {"changed": changed, "priority": list(settings.priority)}

    def exclude(self, op: str, game: str = "") -> dict[str, Any]:
        settings = self.twitch.settings
        if op == "list":
            return {"exclude": sorted(settings.exclude, key=str.casefold)}
        if not isinstance(game, str):
            raise ActionError("game name is required")
        if op == "add":
            operation = exclude_add
        elif op == "remove":
            operation = exclude_remove
        else:
            raise ActionError("usage: exclude list|add <game>|remove <game>")
        try:
            changed = operation(settings, game)
        except CommandError as exc:
            raise ActionError(str(exc)) from exc
        if changed:
            self.reload()
        return {"changed": changed, "exclude": sorted(settings.exclude, key=str.casefold)}

    def set_setting(self, key: str, value: str) -> dict[str, Any]:
        if not isinstance(key, str) or not isinstance(value, str):
            raise ActionError("setting key and value must be strings")
        try:
            warning = set_setting(self.twitch.settings, key, value)
        except CommandError as exc:
            raise ActionError(str(exc)) from exc
        return {"key": key, "value": setting_value(self.twitch.settings, key), "warning": warning}

    async def logout(self) -> dict[str, Any]:
        if not logout_allowed():
            raise ActionRejected(logout_disabled_message(self.twitch._client_type))
        if not ensure_backup(COOKIES_PATH):
            raise ActionRejected("cannot back up the saved session; refusing logout")
        auth_state = await self.twitch.get_auth()
        async with self.twitch.request(
            "POST",
            "https://id.twitch.tv/oauth2/revoke",
            data={
                "client_id": self.twitch._client_type.CLIENT_ID,
                "token": auth_state.access_token,
            },
        ) as response:
            success = response.status == 200
            if success:
                auth_state.invalidate(delete_cookies=True)
        self.twitch.change_state(State.RESTART)
        if not success:
            raise ActionRejected(f"logout failed (HTTP {response.status})")
        return {"logged_out": True}
