"""Config and options flow."""
from __future__ import annotations

from typing import Any

import probatio as vol  # HA 2026.10+ validates with probatio (voluptuous is only a compat alias)

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector, SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode, TextSelector,
    TextSelectorConfig, TextSelectorType,
)

from . import bridge
from .const import (
    CONF_PASSWORD, CONF_URL, CONF_USERNAME, DEFAULT_URL, DOMAIN, NO_PLAYLIST, OPT_AUTO_SYNC, OPT_TONIES,
)
from .planner import MODE_APPEND, MODE_REPLACE
from .subsonic import SubsonicAuthError, SubsonicClient, SubsonicError


async def _test(hass, data: dict[str, Any]) -> str | None:
    client = SubsonicClient(async_get_clientsession(hass), data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD])
    try:
        await client.ping()
    except SubsonicAuthError:
        return "invalid_auth"
    except SubsonicError:
        return "cannot_connect"
    return None


class TonieSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].rstrip("/")
            await self.async_set_unique_id(f"{user_input[CONF_URL]}|{user_input[CONF_USERNAME]}")
            self._abort_if_unique_id_configured()
            if (err := await _test(self.hass, user_input)) is None:
                return self.async_create_entry(
                    title=f"Navidrome ({user_input[CONF_USERNAME]})",
                    data=user_input,
                    options={OPT_TONIES: {}, OPT_AUTO_SYNC: False},
                )
            errors["base"] = err
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_URL)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TonieSyncOptionsFlow:
        return TonieSyncOptionsFlow()


class TonieSyncOptionsFlow(OptionsFlowWithReload):
    """Overview → pick a Tonie → playlist and mode → back to the overview."""

    def __init__(self) -> None:
        self._tonies: dict[str, dict[str, Any]] | None = None
        self._auto = False
        self._available: dict[str, dict[str, str]] = {}
        self._playlists: dict[str, str] = {}
        self._current: str | None = None

    async def _prepare(self) -> str | None:
        if self._tonies is not None:
            return None
        self._available = bridge.list_creative_tonies(self.hass)
        if not self._available:
            return "no_toniebox"
        entry = self.config_entry
        client = SubsonicClient(
            async_get_clientsession(self.hass), entry.data[CONF_URL], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
        )
        try:
            pls = await client.get_playlists()
        except SubsonicError:
            return "cannot_connect"
        self._playlists = {str(p["id"]): str(p.get("name") or p["id"]) for p in pls if p.get("id")}
        self._tonies = {k: dict(v) for k, v in (entry.options.get(OPT_TONIES) or {}).items()}
        self._auto = bool(entry.options.get(OPT_AUTO_SYNC, False))
        return None

    def _overview(self) -> str:
        lines = []
        for tid, t in sorted(self._available.items(), key=lambda kv: kv[1]["name"].lower()):
            cfg = (self._tonies or {}).get(tid) or {}
            pl = cfg.get("playlist") or NO_PLAYLIST
            if pl == NO_PLAYLIST:
                lines.append(f"- {t['name']}: (none)")
            else:
                mode = "↻" if cfg.get("mode") == MODE_REPLACE else "+"
                lines.append(f"- {t['name']}: {self._playlists.get(pl, cfg.get('playlist_name', pl))} ({mode})")
        return "\n".join(lines)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (err := await self._prepare()) is not None:
            return self.async_abort(reason=err)
        if user_input is not None:
            self._auto = bool(user_input.get(OPT_AUTO_SYNC, False))
            if tid := user_input.get("tonie"):
                self._current = tid
                return await self.async_step_tonie()
            return self.async_create_entry(data={OPT_TONIES: self._tonies, OPT_AUTO_SYNC: self._auto})
        tonie_opts = [
            SelectOptionDict(value=tid, label=t["name"])
            for tid, t in sorted(self._available.items(), key=lambda kv: kv[1]["name"].lower())
        ]
        schema = vol.Schema(
            {
                vol.Optional("tonie"): SelectSelector(
                    SelectSelectorConfig(options=tonie_opts, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(OPT_AUTO_SYNC, default=self._auto): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, description_placeholders={"overview": self._overview()}
        )

    async def async_step_tonie(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._tonies is not None and self._current is not None
        tid = self._current
        info = self._available[tid]
        cfg = self._tonies.get(tid) or {}
        if user_input is not None:
            pl = user_input["playlist"]
            self._tonies[tid] = {
                "name": info["name"],
                "household_id": info["household_id"],
                "playlist": pl,
                "playlist_name": self._playlists.get(pl, ""),
                "mode": user_input["mode"],
            }
            self._current = None
            return await self.async_step_init()
        pl_opts = [SelectOptionDict(value=NO_PLAYLIST, label="(none)")] + [
            SelectOptionDict(value=pid, label=name)
            for pid, name in sorted(self._playlists.items(), key=lambda kv: kv[1].lower())
        ]
        current_pl = cfg.get("playlist", NO_PLAYLIST)
        if current_pl not in self._playlists:
            current_pl = NO_PLAYLIST
        schema = vol.Schema(
            {
                vol.Required("playlist", default=current_pl): SelectSelector(
                    SelectSelectorConfig(options=pl_opts, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("mode", default=cfg.get("mode", MODE_APPEND)): SelectSelector(
                    SelectSelectorConfig(
                        options=[MODE_APPEND, MODE_REPLACE], mode=SelectSelectorMode.LIST, translation_key="mode"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="tonie", data_schema=schema, description_placeholders={"name": info["name"]})
