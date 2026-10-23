"""Sync per Tonie: state, locks, storage, auto sync."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import bridge
from .const import (
    AUTO_SYNC_MINUTES, DOMAIN, NO_PLAYLIST, OPT_AUTO_SYNC, OPT_TONIES, SIGNAL_UPDATE, STATE_ERROR,
    STATE_PENDING, STATE_RUNNING, STORAGE_VERSION,
)
from .adopt import AdoptError, plan_adopt
from .engine import NO_CHANGE, SyncAbort, read_chapters, run_sync
from .matcher import default_playlist_name
from .migrations import migrate_store
from .planner import MODE_APPEND
from .subsonic import SubsonicClient, SubsonicError, describe_error

_LOGGER = logging.getLogger(__name__)
TONIE_TIMEOUT = 300  # seconds per cloud call


class _TonieAdapter:
    """TonieApi for engine.run_sync via the toniebox client, with a timeout."""

    def __init__(self, ref: bridge.TonieRef) -> None:
        self._ref = ref

    async def _t(self, coro):
        async with asyncio.timeout(TONIE_TIMEOUT):
            return await coro

    async def get(self):
        return await self._t(self._ref.client.get_creative_tonie(self._ref.household_id, self._ref.tonie_id))

    async def upload(self, data: bytes, filename: str) -> str:
        return await self._t(self._ref.client.upload_file(data, filename))

    async def patch(self, chapters):
        return await self._t(
            self._ref.client.patch_creative_tonie(self._ref.household_id, self._ref.tonie_id, {"chapters": chapters})
        )


class _TonieSyncStore(Store):
    """Store with migration of older versions (see migrations.py)."""

    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: Any) -> Any:
        return migrate_store(old_major_version, old_data)


class TonieSyncManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SubsonicClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._store: Store = _TonieSyncStore(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._data: dict[str, Any] = {"tonies": {}}
        self._locks: dict[str, asyncio.Lock] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self._unsub_timer = None

    # ── Configuration ─────────────────────────────────────────────────────────

    @property
    def tonies(self) -> dict[str, dict[str, Any]]:
        """Only Tonies with an assigned playlist."""
        all_t = self.entry.options.get(OPT_TONIES) or {}
        return {tid: c for tid, c in all_t.items() if c.get("playlist") and c["playlist"] != NO_PLAYLIST}

    def stored_data(self) -> dict[str, Any]:
        """Copy of the stored data (for diagnostics)."""
        import copy

        return copy.deepcopy(self._data)

    def _tstore(self, tonie_id: str) -> dict[str, Any]:
        return self._data["tonies"].setdefault(tonie_id, {"mapping": {}, "excluded": []})

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict) and isinstance(data.get("tonies"), dict):
            self._data = data
        for tid, cfg in self.tonies.items():
            last = dict(self._tstore(tid).get("last") or {})
            # Playlist changed → the stored state no longer applies
            if last.get("playlist_id") != cfg["playlist"]:
                last = {}
            state = last.pop("state", STATE_PENDING)
            if state == STATE_RUNNING:
                state = STATE_ERROR
                last["last_message"] = "Sync was interrupted by a restart"
            self.status[tid] = {"state": state, "attrs": {"playlist_name": cfg.get("playlist_name"), **last}}
        if self.entry.options.get(OPT_AUTO_SYNC):
            self._unsub_timer = async_track_time_interval(
                self.hass, self._auto_tick, timedelta(minutes=AUTO_SYNC_MINUTES), name=f"{DOMAIN} auto"
            )

    @callback
    def async_shutdown(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # ── Status ────────────────────────────────────────────────────────────────

    def _set(self, tonie_id: str, state: str | None = None, **attrs: Any) -> None:
        st = self.status.setdefault(tonie_id, {"state": STATE_PENDING, "attrs": {}})
        if state:
            st["state"] = state
        st["attrs"].update(attrs)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(entry_id=self.entry.entry_id, tonie_id=tonie_id))

    def is_running(self, tonie_id: str) -> bool:
        lock = self._locks.get(tonie_id)
        return bool(lock and lock.locked())

    # ── Auto sync ─────────────────────────────────────────────────────────────

    @callback
    def _auto_tick(self, _now) -> None:
        self.hass.async_create_background_task(self._auto_run(), f"{DOMAIN} auto sync")

    async def _auto_run(self) -> None:
        for tid in list(self.tonies):
            if not self.is_running(tid):
                await self.async_sync(tid, only_if_changed=True)

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def async_sync(
        self,
        tonie_id: str,
        restore_deleted: bool = False,
        only_if_changed: bool = False,
        confirm_removals: bool = False,
    ) -> dict[str, Any]:
        cfg = self.tonies.get(tonie_id)
        name = (cfg or {}).get("name", tonie_id)
        if cfg is None:
            return {"tonie_id": tonie_id, "name": name, "state": STATE_ERROR, "message": "No playlist assigned"}
        lock = self._locks.setdefault(tonie_id, asyncio.Lock())
        if lock.locked():
            return {"tonie_id": tonie_id, "name": name, "state": STATE_RUNNING, "message": "Sync is already running"}
        try:
            async with lock:
                prev_state = self.status.get(tonie_id, {}).get("state")
                result: dict[str, Any] = {"tonie_id": tonie_id, "name": name, "uploaded": 0, "removed": 0}
                try:
                    out = await self._sync(tonie_id, cfg, restore_deleted, only_if_changed, confirm_removals, prev_state)
                    if out is None:  # auto sync: playlist unchanged
                        return {**result, "state": prev_state, "message": "Playlist unchanged"}
                    state, msg = out.state, out.message
                    result.update(uploaded=out.uploaded, removed=out.removed)
                except SyncAbort as err:
                    state, msg = err.state, str(err)
                except (SubsonicError, bridge.BridgeError) as err:
                    state, msg = STATE_ERROR, describe_error(err)
                except Exception as err:  # noqa: BLE001, report a state instead of crashing
                    state, msg = STATE_ERROR, describe_error(err)
                    _LOGGER.error("Sync of %s failed: %s", name, msg)
                    # Traceback only at debug level: exception texts may contain URLs with credentials
                    _LOGGER.debug("Sync of %s failed", name, exc_info=True)
                if state == STATE_ERROR:
                    _LOGGER.warning("Sync of %s: %s", name, msg)
                self._set(tonie_id, state, last_message=msg, progress=None)
                await self._persist_last(tonie_id, cfg)
                return {**result, "state": state, "message": msg}
        finally:
            # Only notify after the lock is released, otherwise the button stays "unavailable"
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(entry_id=self.entry.entry_id, tonie_id=tonie_id))

    async def _persist_last(self, tonie_id: str, cfg: dict[str, Any]) -> None:
        st = self.status.get(tonie_id) or {}
        last = {k: v for k, v in (st.get("attrs") or {}).items() if k != "progress"}
        last["state"] = st.get("state")
        last["playlist_id"] = cfg["playlist"]
        self._tstore(tonie_id)["last"] = last
        await self._store.async_save(self._data)

    async def _sync(self, tid, cfg, restore_deleted, only_if_changed, confirm_removals, prev_state):
        ref = bridge.find_tonie(self.hass, tid)

        def report(state: str | None = None, **attrs: Any) -> None:
            self._set(tid, state, **attrs)

        out = await run_sync(
            source=self.client,
            tonie=_TonieAdapter(ref),
            ts=self._tstore(tid),
            persist=lambda: self._store.async_save(self._data),
            report=report,
            now=lambda: dt_util.now().isoformat(),
            playlist_id=cfg["playlist"],
            mode=cfg.get("mode") or MODE_APPEND,
            restore_deleted=restore_deleted,
            confirm_removals=confirm_removals,
            only_if_changed=only_if_changed,
            prev_state=prev_state,
        )
        if out is not None and not out.message.startswith(NO_CHANGE):
            try:
                await bridge.refresh(ref)
            except Exception as err:  # noqa: BLE001, the refresh is only a convenience
                _LOGGER.debug("toniebox refresh failed: %s", err)
        return out

    # ── Adopting existing chapters ────────────────────────────────────────────

    async def async_adopt(
        self, tonie_id: str, playlist_name: str | None, apply: bool, label_hint: str | None = None
    ) -> dict[str, Any]:
        """Match chapters to Navidrome songs; with apply, create the playlist and adopt the mapping.

        Never writes to the Tonie. Errors → AdoptError/SyncAbort/SubsonicError/BridgeError.
        """
        ref = bridge.find_tonie(self.hass, tonie_id)
        lock = self._locks.setdefault(tonie_id, asyncio.Lock())
        if lock.locked():
            raise AdoptError("A sync is currently running for this Tonie")
        async with lock:
            chapters = read_chapters(await _TonieAdapter(ref).get())
            plan = await plan_adopt(chapters, self.client.search_songs)
            label = bridge.tonie_label(self.hass, tonie_id, ref.name, label_hint)
            name = (playlist_name or "").strip() or default_playlist_name(label)
            report: dict[str, Any] = {
                "tonie_id": tonie_id, "tonie": label, "playlist_name": name, "applied": False, **plan.report()
            }
            if not apply:
                return report

            if not plan.song_ids:
                raise AdoptError("No sure match, nothing to adopt")
            if tonie_id in self.tonies:
                raise AdoptError("A playlist is already assigned to this Tonie; set it to \"(none)\" in the options first")
            if self._tstore(tonie_id).get("mapping"):
                raise AdoptError("There is already a stored mapping for this Tonie")
            if report["sync_after_adopt_unchanged"] is not True:
                raise AdoptError("A sync after adopting would change the Tonie; aborted")
            existing = await self.client.get_playlists()
            if any(str(p.get("name") or "").strip().lower() == name.lower() for p in existing):
                raise AdoptError(f"Playlist \"{name}\" already exists in Navidrome; nothing changed")

            pid = await self.client.create_playlist(name, plan.song_ids)
            if not pid:
                pid = next(
                    (str(p["id"]) for p in await self.client.get_playlists() if p.get("name") == name), None
                )
            if not pid:
                raise AdoptError(f"Playlist \"{name}\" may have been created, but its ID is unknown; please check Navidrome")
            pl = await self.client.get_playlist(pid)
            entries = pl.get("entry") or []
            try:
                mapping = plan.mapping_for(entries)
                if not plan.check_unchanged(entries):
                    raise AdoptError("A sync after adopting would not be a no-op")
            except AdoptError as err:
                raise AdoptError(f"{err}; playlist \"{name}\" ({pid}) was created but not assigned") from err

            ts = self._tstore(tonie_id)
            ts.update(mapping=mapping, excluded=[], fingerprint=None, pending=None, last={})
            await self._store.async_save(self._data)

            tonies = {k: dict(v) for k, v in (self.entry.options.get(OPT_TONIES) or {}).items()}
            tonies[tonie_id] = {
                "name": label,
                "household_id": ref.household_id,
                "playlist": pid,
                "playlist_name": name,
                "mode": MODE_APPEND,
            }
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, OPT_TONIES: tonies, OPT_AUTO_SYNC: False}
            )
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            report.update(applied=True, playlist_id=pid, adopted=len(mapping))
            return report
