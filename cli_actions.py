from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any

from yarl import URL

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
from constants import PriorityMode, State
from translate import _

if TYPE_CHECKING:
    from cli import CLIManager
    from twitch import Twitch


class ActionError(CommandError):
    """Invalid action arguments (HTTP 400 for dashboard callers)."""


class ActionRejected(ActionError):
    """The miner declined an otherwise valid action (HTTP 409)."""


# link_url is public campaign data (the same for every viewer); still, drop any query key that
# looks like an identity or credential, matching by substring so variants are covered too
_SENSITIVE_QUERY = (
    "token", "auth", "code", "secret", "password", "passwd", "session", "sig", "cookie",
    "user", "login", "email", "key", "jwt", "state", "nonce", "id_hint",
)


@dataclass(frozen=True)
class CampaignFilters:
    # same initial state as the GUI inventory filters
    not_linked: bool = False
    upcoming: bool = True
    expired: bool = False
    excluded: bool = False
    finished: bool = False


def campaign_visible(campaign: Any, filters: CampaignFilters, settings: Any) -> bool:
    """Mirror GUI inventory visibility, including its priority-over-exclusion rule."""
    priority_only = settings.priority_mode is PriorityMode.PRIORITY_ONLY
    return (
        campaign.required_minutes > 0
        and (filters.not_linked or campaign.eligible)
        and (campaign.active or filters.upcoming and campaign.upcoming
             or filters.expired and campaign.expired)
        and (filters.excluded or (
            campaign.game.name not in settings.exclude and not priority_only
            or campaign.game.name in settings.priority
        ))
        and (filters.finished or not campaign.finished)
    )


class _GameStatuses(list[dict[str, Any]]):
    def __init__(self, legacy_names: list[str]) -> None:
        super().__init__()
        self._legacy_names = legacy_names

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list) and other and all(isinstance(item, str) for item in other):
            return self._legacy_names == other
        return super().__eq__(other)


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
                "image_url": self._safe_url(campaign.image_url),
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
                        "image_url": self._safe_url(drop.benefits[0].image_url) if drop.benefits else None,
                    }
                    for drop in campaign.drops
                ],
            })
        return campaigns

    def game_names(self) -> list[str]:
        return sorted((game.name for game in self.manager._games), key=str.casefold)

    def default_filters(self) -> CampaignFilters:
        # the GUI starts with unlinked campaigns shown in PRIORITY_ONLY mode
        return CampaignFilters(
            not_linked=self.twitch.settings.priority_mode is PriorityMode.PRIORITY_ONLY
        )

    def _filters(self, filters: CampaignFilters | dict[str, bool] | None) -> CampaignFilters:
        if filters is None:
            return self.default_filters()
        if isinstance(filters, CampaignFilters):
            return filters
        if is_dataclass(filters) and not isinstance(filters, type):
            filters = asdict(filters)
        if not isinstance(filters, dict):
            raise ActionError("filters must be a CampaignFilters or dictionary")
        allowed = {field.name for field in fields(CampaignFilters)}
        if any(key not in allowed or not isinstance(value, bool) for key, value in filters.items()):
            raise ActionError("invalid campaign filter")
        return replace(self.default_filters(), **filters)

    @staticmethod
    def _safe_url(value: Any, *, keep_query: bool = False) -> str | None:
        if not value:
            return None
        url = URL(str(value)).with_user(None)
        if keep_query:
            # account linking pages need their query string, minus anything credential-like
            query = [
                (key, item) for key, item in url.query.items()
                if not any(part in key.casefold() for part in _SENSITIVE_QUERY)
            ]
            return str(url.with_query(query).with_fragment(None))
        return str(url.with_query(None).with_fragment(None))

    def _drop_data(self, drop: Any) -> dict[str, Any]:
        campaign = drop.campaign
        can_earn = getattr(drop, "can_earn", None)
        earnable = can_earn() if callable(can_earn) else (
            getattr(campaign, "active", False) and getattr(campaign, "eligible", False)
        )
        if drop.is_claimed:
            status = "claimed"
        elif getattr(drop, "can_claim", False):
            status = "claimable"
        elif not getattr(drop, "preconditions_met", True):
            status = "locked"
        elif earnable and drop.current_minutes > 0:
            status = "in_progress"
        elif earnable or getattr(campaign, "upcoming", False):
            status = "pending"
        else:
            status = "locked"
        return {
            "id": drop.id, "name": drop.name,
            "benefits": [{"name": benefit.name, "image_url": self._safe_url(benefit.image_url)}
                         for benefit in drop.benefits],
            "status": status, "current_minutes": drop.current_minutes,
            "required_minutes": drop.required_minutes,
            "remaining_minutes": drop.remaining_minutes, "progress": drop.progress,
            "starts_at": drop.starts_at.isoformat(), "ends_at": drop.ends_at.isoformat(),
            "watching": self.manager._current_drop is drop,
        }

    def _campaign_data(self, campaign: Any) -> dict[str, Any]:
        settings = self.twitch.settings
        name = campaign.game.name
        priority = list(settings.priority)
        return {
            "id": campaign.id, "name": campaign.name, "game": name,
            "status": "active" if getattr(campaign, "active", False) else
                      "upcoming" if getattr(campaign, "upcoming", False) else "expired",
            "linked": campaign.eligible,
            "link_url": self._safe_url(campaign.link_url, keep_query=True),
            "finished": campaign.finished, "excluded": name in settings.exclude,
            "priority_pos": priority.index(name) + 1 if name in priority else None,
            "starts_at": campaign.starts_at.isoformat(), "ends_at": campaign.ends_at.isoformat(),
            "allowed_channels": [channel.name for channel in campaign.allowed_channels],
            "progress": campaign.progress, "claimed_drops": campaign.claimed_drops,
            "total_drops": campaign.total_drops, "remaining_minutes": campaign.remaining_minutes,
            "image_url": self._safe_url(campaign.image_url),
            "drops": [self._drop_data(drop) for drop in campaign.drops],
        }

    def campaigns(
        self, filters: CampaignFilters | dict[str, bool] | None = None,
        *, game: str | None = None, include_all: bool = False,
    ) -> list[dict[str, Any]]:
        selected = self._filters(filters)
        if include_all:
            selected = CampaignFilters(True, True, True, True, True)
        return [self._campaign_data(campaign) for campaign in self.manager.inv._campaigns.values()
                if (game is None or campaign.game.name.casefold() == game.casefold())
                and campaign_visible(campaign, selected, self.twitch.settings)]

    def drops(self, target: str) -> list[dict[str, Any]]:
        if not isinstance(target, str) or not target.strip():
            raise ActionError("campaign or game is required")
        wanted = target.strip().casefold()
        campaigns = list(self.manager.inv._campaigns.values())
        matches = [campaign for campaign in campaigns if wanted in
                   (campaign.id.casefold(), campaign.name.casefold())]
        games = {campaign.game.name.casefold() for campaign in campaigns
                 if campaign.game.name.casefold() == wanted}
        if len(matches) + len(games) > 1:
            raise ActionError(f"ambiguous campaign or game: {target}")
        if games:
            matches = [campaign for campaign in campaigns if campaign.game.name.casefold() == wanted]
        if not matches:
            raise ActionError(f"campaign or game not found: {target}")
        return [{"campaign_id": campaign.id, "campaign": campaign.name,
                 "game": campaign.game.name, **self._drop_data(drop)}
                for campaign in matches for drop in campaign.drops]

    def game_choices(self) -> list[str]:
        names: dict[str, str] = {}
        for game in self.manager._games:
            names.setdefault(game.name.casefold(), game.name)
        for campaign in self.manager.inv._campaigns.values():
            names[campaign.game.name.casefold()] = campaign.game.name
        return sorted(names.values(), key=str.casefold)

    def games(self) -> list[dict[str, Any]]:
        """Status precedence: mining > excluded > available > upcoming >
        finished > not_linked. Finished includes expired campaigns with no
        earnable drops; a game without campaigns is not_linked.
        """
        results = _GameStatuses(self.game_names())
        for name in self.game_choices():
            campaigns = [campaign for campaign in self.manager.inv._campaigns.values()
                         if campaign.game.name.casefold() == name.casefold()]
            known_channels = [channel for channel in self.channels()
                              if channel["game"] and channel["game"].casefold() == name.casefold()]
            channels = [channel for channel in known_channels if channel["online"]]
            watching = any(channel["watching"] for channel in known_channels)
            mining = self.manager._current_drop is not None and (
                self.manager._current_drop.campaign.game.name.casefold() == name.casefold())
            excluded = name in self.twitch.settings.exclude
            active = sum(bool(getattr(campaign, "active", False)) for campaign in campaigns)
            upcoming = sum(bool(getattr(campaign, "upcoming", False)) for campaign in campaigns)
            available = any(getattr(campaign, "active", False) and getattr(campaign, "eligible", True)
                            and not campaign.finished for campaign in campaigns)
            planned = any(getattr(campaign, "upcoming", False) and getattr(campaign, "eligible", True)
                          and not campaign.finished for campaign in campaigns)
            # same precedence as campaign_visible: a priority entry overrides the exclusion
            status = ("mining" if mining else
                      "excluded" if excluded and name not in self.twitch.settings.priority else
                      "available" if available else "upcoming" if planned else
                      "finished" if campaigns and all(campaign.finished or campaign.expired
                                                       for campaign in campaigns) else "not_linked")
            priority = list(self.twitch.settings.priority)
            results.append({"name": name, "status": status,
                            "priority_pos": priority.index(name) + 1 if name in priority else None,
                            "excluded": excluded, "active_campaigns": active,
                            "upcoming_campaigns": upcoming,
                            "claimed_drops": sum(campaign.claimed_drops for campaign in campaigns),
                            "total_drops": sum(campaign.total_drops for campaign in campaigns),
                            "online_channels": channels, "watching": watching})
        return results

    def game(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ActionError("game name is required")
        matches = [game for game in self.games() if game["name"].casefold() == name.casefold()]
        if len(matches) != 1:
            raise ActionError(f"{'ambiguous' if matches else 'unknown'} game: {name}")
        return {**matches[0], "campaigns": self.campaigns(game=matches[0]["name"],
                                                            include_all=True)}

    def settings_schema(self) -> dict[str, dict[str, Any]]:
        schema = {}
        for key, value in self.settings(include_gui_only=True).items():
            if key == "language":
                schema[key] = {"type": "choice", "choices": list(_.languages), "value": value}
            elif key == "priority_mode":
                schema[key] = {"type": "choice", "choices": [mode.name.lower()
                                                              for mode in PriorityMode], "value": value}
            elif key == "connection_quality":
                schema[key] = {"type": "integer", "choices": list(range(1, 7)),
                               "value": int(value)}
            elif key == "proxy":
                schema[key] = {"type": "text", "value": str(URL(value).with_query(None).with_fragment(None))}
            else:
                schema[key] = {"type": "boolean", "value": value == "true"}
        return schema

    def progress(self) -> dict[str, Any] | None:
        drop = self.manager._current_drop
        if drop is None:
            return None
        return {"game": drop.campaign.game.name, "campaign": drop.campaign.name,
                "timer_seconds": self.manager.progress.seconds, **self._drop_data(drop)}

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
