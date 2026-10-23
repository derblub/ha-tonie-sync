"""tonie_sync: sync a Navidrome playlist to a Creative Tonie."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import probatio as vol  # HA 2026.10+ validates with probatio (voluptuous is only a compat alias)

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, ATTR_ENTITY_ID, ENTITY_MATCH_ALL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_extract_entity_ids, async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from . import bridge
from .adopt import AdoptError
from .const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, DOMAIN, OPT_TONIES, TONIEBOX_DOMAIN
from .engine import SyncAbort
from .manager import TonieSyncManager
from .subsonic import SubsonicClient, SubsonicError, describe_error
from .targets import TargetError, choose_entry

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_RESTORE = "restore_deleted"
ATTR_CONFIRM = "confirm_removals"
SYNC_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_RESTORE, default=False): cv.boolean,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
    }
)

ADOPT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("playlist_name"): cv.string,
        vol.Optional("apply", default=False): cv.boolean,
    }
)

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("query"): cv.string,
        vol.Optional("count", default=20): vol.All(vol.Coerce(int), vol.Range(1, 100)),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)

PLAYLIST_EDIT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("add_song_ids", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("remove_song_ids", default=[]): vol.All(cv.ensure_list, [cv.string]),
    }
)

type TonieSyncConfigEntry = ConfigEntry[TonieSyncManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_sync(call: ServiceCall) -> ServiceResponse:
        target_keys = (ATTR_ENTITY_ID, "device_id", "area_id", "floor_id", "label_id")
        has_target = call.data.get(ATTR_ENTITY_ID) != ENTITY_MATCH_ALL and any(k in call.data for k in target_keys)
        entity_ids = await async_extract_entity_ids(call) if has_target else None
        targets = _resolve_targets(hass, entity_ids, strict=has_target and ATTR_ENTITY_ID in call.data)
        results: list[dict[str, Any]] = []
        for manager, tonie_id in targets:
            results.append(
                await manager.async_sync(
                    tonie_id, restore_deleted=call.data[ATTR_RESTORE], confirm_removals=call.data[ATTR_CONFIRM]
                )
            )
        return {"results": results} if call.return_response else None

    hass.services.async_register(
        DOMAIN, "sync", handle_sync, schema=SYNC_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )

    async def handle_adopt(call: ServiceCall) -> ServiceResponse:
        tonie_id, owner = _tonie_from_entity(hass, call.data[ATTR_ENTITY_ID])
        manager = _pick_manager(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID), owner, tonie_id,
            lambda m: set(m.entry.options.get(OPT_TONIES) or {}),
        )
        try:
            state = hass.states.get(call.data[ATTR_ENTITY_ID])
            hint = state.attributes.get("friendly_name") if state else None
            report = await manager.async_adopt(
                tonie_id, call.data.get("playlist_name"), call.data["apply"], label_hint=hint
            )
        except (AdoptError, SyncAbort, SubsonicError, bridge.BridgeError) as err:
            raise HomeAssistantError(describe_error(err)) from err
        return report if call.return_response else None

    # adopt, search_songs and playlist_edit change Navidrome playlists or expose the library: admins only
    async_register_admin_service(
        hass, DOMAIN, "adopt", handle_adopt, schema=ADOPT_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )

    async def handle_search(call: ServiceCall) -> ServiceResponse:
        manager = _pick_manager(hass, call.data.get(ATTR_CONFIG_ENTRY_ID), None, None, lambda m: set())
        try:
            songs = await manager.client.search_songs(call.data["query"], call.data["count"])
        except SubsonicError as err:
            raise HomeAssistantError(describe_error(err)) from err
        keys = ("id", "artist", "title", "album", "duration")
        return {"songs": [{k: s.get(k) for k in keys} for s in songs]}

    async_register_admin_service(
        hass, DOMAIN, "search_songs", handle_search, schema=SEARCH_SCHEMA, supports_response=SupportsResponse.ONLY
    )

    async def handle_playlist_edit(call: ServiceCall) -> ServiceResponse:
        """Edit the Navidrome playlist of a Tonie; the Tonie itself only changes with the next sync."""
        tonie_id, owner = _tonie_from_entity(hass, call.data[ATTR_ENTITY_ID])
        manager = _pick_manager(hass, None, owner, tonie_id, lambda m: set(m.tonies))
        cfg = manager.tonies.get(tonie_id)
        if not cfg:
            raise HomeAssistantError("No playlist is assigned to this Tonie")
        pid = cfg["playlist"]
        try:
            entries = (await manager.client.get_playlist(pid)).get("entry") or []
            remove = set(call.data["remove_song_ids"])
            idx = [i for i, e in enumerate(entries) if str(e.get("id")) in remove]
            missing = remove - {str(entries[i].get("id")) for i in idx}
            if missing:
                raise HomeAssistantError(f"Not in the playlist: {', '.join(sorted(missing))}")
            await manager.client.update_playlist(pid, call.data["add_song_ids"], idx)
            count = len((await manager.client.get_playlist(pid)).get("entry") or [])
        except SubsonicError as err:
            raise HomeAssistantError(describe_error(err)) from err
        return {"playlist_id": pid, "added": len(call.data["add_song_ids"]), "removed": len(idx), "songs": count}

    async_register_admin_service(
        hass, DOMAIN, "playlist_edit", handle_playlist_edit, schema=PLAYLIST_EDIT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _tonie_from_entity(hass: HomeAssistant, entity_id: str) -> tuple[str, str | None]:
    """Creative Tonie ID from an entity of the toniebox or tonie_sync integration.

    Returns (tonie_id, config entry of the tonie_sync entity or None for a toniebox entity).
    """
    ent = er.async_get(hass).async_get(entity_id)
    if ent is not None:
        if ent.platform == DOMAIN:
            return ent.unique_id.rsplit("_", 1)[0], ent.config_entry_id
        if ent.platform == TONIEBOX_DOMAIN and ent.unique_id.startswith("ct_"):
            uid = ent.unique_id[3:]
            for tid in bridge.list_creative_tonies(hass):
                if uid == tid or uid.startswith(tid + "_"):
                    return tid, None
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="invalid_tonie", translation_placeholders={"entity": entity_id}
    )


def _pick_manager(
    hass: HomeAssistant,
    explicit: str | None,
    owner: str | None,
    tonie_id: str | None,
    known: Callable[[TonieSyncManager], set[str]],
) -> TonieSyncManager:
    """Manager of the config entry an action acts on (see targets.choose_entry)."""
    managers = _managers(hass)
    try:
        entry_id = choose_entry({eid: known(m) for eid, m in managers.items()}, explicit, owner, tonie_id)
    except TargetError as err:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key=str(err)) from None
    return managers[entry_id]


def _managers(hass: HomeAssistant) -> dict[str, TonieSyncManager]:
    return {
        e.entry_id: e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    }


def _resolve_targets(
    hass: HomeAssistant, entity_ids: set[str] | None, strict: bool
) -> list[tuple[TonieSyncManager, str]]:
    """Target entities → (manager, Tonie). Without a target: all Tonies with a playlist."""
    managers = _managers(hass)
    if entity_ids is None:
        return [(m, tid) for m in managers.values() for tid in m.tonies]
    reg = er.async_get(hass)
    out: list[tuple[TonieSyncManager, str]] = []
    for eid in sorted(entity_ids):
        ent = reg.async_get(eid)
        if ent is None or ent.platform != DOMAIN or ent.config_entry_id not in managers:
            if not strict:
                continue  # foreign entities picked up via device/area
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_target", translation_placeholders={"entity": eid}
            )
        tonie_id = ent.unique_id.rsplit("_", 1)[0]
        pair = (managers[ent.config_entry_id], tonie_id)
        if pair not in out:
            out.append(pair)
    return out


async def async_setup_entry(hass: HomeAssistant, entry: TonieSyncConfigEntry) -> bool:
    client = SubsonicClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    manager = TonieSyncManager(hass, entry, client)
    await manager.async_load()
    entry.runtime_data = manager
    entry.async_on_unload(manager.async_shutdown)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TonieSyncConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
