"""Flow of one sync without HA imports (testable with fakes).

The manager provides the playlist source, Tonie access, storage and status reporting.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import re
from typing import Any, Protocol

try:
    from .const import STATE_CONFIRM, STATE_CURRENT, STATE_ERROR, STATE_RUNNING, STATE_TOO_LONG
    from .planner import (
        MAX_CHAPTERS, MAX_SECONDS, MODE_APPEND, Plan, assemble, build_plan, chapter_ids, fingerprint,
        mapping_after_patch, new_refs, resolve_pending, safe_suffix, songs_from_entries,
    )
    from .subsonic import describe_error
except ImportError:  # tests: modules are on sys.path directly
    from const import STATE_CONFIRM, STATE_CURRENT, STATE_ERROR, STATE_RUNNING, STATE_TOO_LONG
    from planner import (
        MAX_CHAPTERS, MAX_SECONDS, MODE_APPEND, Plan, assemble, build_plan, chapter_ids, fingerprint,
        mapping_after_patch, new_refs, resolve_pending, safe_suffix, songs_from_entries,
    )
    from subsonic import describe_error


NO_CHANGE = "No change"


class SyncAbort(Exception):
    """Abort without changing the Tonie; state = sensor state."""

    def __init__(self, message: str, state: str = STATE_ERROR) -> None:
        super().__init__(message)
        self.state = state


class Source(Protocol):
    async def get_playlist(self, playlist_id: str) -> dict[str, Any]: ...
    async def download_song(self, song_id: str, suffix: str = "mp3") -> tuple[bytes, str]: ...


class TonieApi(Protocol):
    async def get(self) -> dict[str, Any]: ...
    async def upload(self, data: bytes, filename: str) -> str: ...
    async def patch(self, chapters: list[dict[str, Any]]) -> Any: ...


@dataclass
class Outcome:
    state: str
    message: str
    uploaded: int = 0
    removed: int = 0


def _num(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def read_chapters(tonie: Any) -> list[dict[str, Any]]:
    """Chapter list from the cloud response, strictly validated (otherwise the PATCH could lose data)."""
    if not isinstance(tonie, dict):
        raise SyncAbort("Tonie cloud returned an invalid response")
    chapters = tonie.get("chapters")
    if not isinstance(chapters, list) or not all(isinstance(c, dict) for c in chapters):
        raise SyncAbort("Tonie cloud returned no chapter list; nothing changed")
    present = _num(tonie.get("chaptersPresent"))
    if present is not None and present != len(chapters):
        raise SyncAbort(
            f"Inconsistent chapter count (list {len(chapters)}, cloud reports {present}); nothing changed"
        )
    return chapters


def limits(tonie: dict[str, Any]) -> tuple[float, int]:
    """Capacity; smaller cloud values (present + remaining) take precedence."""
    max_s: float = MAX_SECONDS
    max_c = MAX_CHAPTERS
    sp, sr = _num(tonie.get("secondsPresent")), _num(tonie.get("secondsRemaining"))
    if sp is not None and sr is not None:
        max_s = min(max_s, sp + sr)
    cp, cr = _num(tonie.get("chaptersPresent")), _num(tonie.get("chaptersRemaining"))
    if cp is not None and cr is not None:
        max_c = min(max_c, cp + cr)
    return max_s, max_c


def check_plan(plan: Plan, restore_deleted: bool, confirm_removals: bool) -> None:
    """Safety checks before any change."""
    if plan.mapping_lost and not restore_deleted:
        raise SyncAbort(
            f"None of the {len(plan.mapping) or 'stored'} mapped chapters found on the Tonie "
            "(IDs changed?); nothing changed. With restore_deleted: true the songs are uploaded again "
            "(the existing chapters are then treated as foreign)."
        )
    if plan.ambiguous:
        raise SyncAbort(
            f"{len(plan.ambiguous)} managed chapter(s) cannot be found unambiguously (cloud changed IDs, "
            "several candidates with the same title); nothing changed"
        )
    if plan.unknown_duration:
        names = ", ".join(plan.unknown_duration[:3]) + (" …" if len(plan.unknown_duration) > 3 else "")
        raise SyncAbort(f"Unknown duration ({len(plan.unknown_duration)}): {names}; wait for transcoding")
    if not plan.fits:
        raise SyncAbort("Does not fit on the Tonie: " + ", ".join(plan.problems), STATE_TOO_LONG)
    if plan.mass_removal and not confirm_removals:
        raise SyncAbort(
            f"{len(plan.removals)} chapters would be removed (playlist empty or shrunk a lot); "
            "to proceed, call the action tonie_sync.sync with confirm_removals: true",
            STATE_CONFIRM,
        )


async def run_sync(
    *,
    source: Source,
    tonie: TonieApi,
    ts: dict[str, Any],
    persist: Callable[[], Awaitable[None]],
    report: Callable[..., None],
    now: Callable[[], str],
    playlist_id: str,
    mode: str = MODE_APPEND,
    restore_deleted: bool = False,
    confirm_removals: bool = False,
    only_if_changed: bool = False,
    prev_state: str | None = None,
) -> Outcome | None:
    """One sync. None = skipped (playlist unchanged)."""
    pl = await source.get_playlist(playlist_id)
    songs = songs_from_entries(pl.get("entry") or [])
    fp = fingerprint(songs, mode)
    if only_if_changed and not ts.get("pending") and fp == ts.get("fingerprint") and prev_state == STATE_CURRENT:
        return None

    report(STATE_RUNNING, progress="Reading Tonie", playlist_name=pl.get("name"), mode=mode)
    data = await tonie.get()
    chapters = read_chapters(data)
    notes: list[str] = []

    # Resolve an interrupted PATCH from the previous run
    if pending := ts.get("pending"):
        resolved = resolve_pending(pending, chapters)
        if resolved is not None:
            ts["mapping"] = resolved
            ts["excluded"] = pending.get("excluded", ts.get("excluded", []))
            notes.append("interrupted sync recovered")
        ts["pending"] = None
        await persist()

    def plan_for(chs: list[dict[str, Any]], tonie_data: dict[str, Any]) -> Plan:
        max_s, max_c = limits(tonie_data)
        return build_plan(
            chs, songs, ts.get("mapping") or {}, ts.get("excluded") or [], mode, restore_deleted, max_s, max_c,
            known_foreign=set(ts.get("foreign_ids") or []),
        )

    plan = plan_for(chapters, data)
    report(
        playlist_name=pl.get("name"),
        songs=len(songs),
        playlist_minutes=round(plan.playlist_seconds / 60, 1),
        total_minutes=round(plan.total_seconds / 60, 1),
        free_minutes=round(plan.free_seconds / 60, 1),
        chapters=plan.chapter_count,
        foreign_chapters=plan.foreign_count,
        excluded_songs=len(plan.excluded),
        mode=mode,
    )
    check_plan(plan, restore_deleted, confirm_removals)

    def commit(mapping: dict[str, dict[str, str]], p: Plan) -> None:
        ts["mapping"] = mapping
        ts["excluded"] = p.excluded
        ts["fingerprint"] = fp
        ts["pending"] = None
        ts["foreign_ids"] = sorted(p.foreign_ids)
        report(last_sync=now())

    if plan.unchanged:
        commit(plan.mapping, plan)
        await persist()
        if plan.newly_excluded:
            notes.append(f"{len(plan.newly_excluded)} songs deleted in the app skipped")
        return Outcome(STATE_CURRENT, NO_CHANGE + "".join(f", {n}" for n in notes))

    # Download and upload new songs one by one (only one song in memory at a time)
    uploaded: dict[str, str] = {}
    n = len(plan.uploads)
    for i, song in enumerate(plan.uploads, 1):
        report(progress=f"{i}/{n}: {song.chapter_title}")
        try:
            audio, ext = await source.download_song(song.song_id, song.suffix)
            name = re.sub(r"[^A-Za-z0-9_-]", "_", song.song_id)[:64] or "song"
            fid = await tonie.upload(audio, f"{name}.{safe_suffix(ext)}")
            del audio
        except Exception as err:
            raise SyncAbort(
                f"Upload of \"{song.chapter_title}\" failed ({describe_error(err)}); nothing changed, "
                f"{len(uploaded)} file(s) already uploaded and orphaned"
            ) from err
        if not fid:
            raise SyncAbort(f"Upload of \"{song.chapter_title}\" returned no file ID")
        uploaded[song.key] = str(fid)

    # Read again and re-plan: changes made in the meantime (titles, durations, new
    # foreign chapters) are kept instead of being overwritten with the old state.
    report(progress="Writing chapter list")
    fresh_data = await tonie.get()
    fresh = read_chapters(fresh_data)
    plan = plan_for(fresh, fresh_data)
    if {s.key for s in plan.uploads} != set(uploaded):
        raise SyncAbort(
            f"Tonie was changed during the sync; nothing written, {len(uploaded)} file(s) orphaned. "
            "Please sync again."
        )
    check_plan(plan, restore_deleted, confirm_removals)
    final = assemble(plan, uploaded)

    # Remember as "pending" before the PATCH so a timeout or restart can be resolved cleanly
    ts["pending"] = {"mapping": new_refs(plan, uploaded), "uploaded": uploaded, "excluded": plan.excluded}
    await persist()
    try:
        resp = await tonie.patch(final)
    except Exception as err:
        mapping = await _after_failed_patch(tonie, ts, final, uploaded)
        if mapping is None:
            await persist()
            raise SyncAbort(f"Write failed ({describe_error(err)}); {len(uploaded)} file(s) orphaned") from err
        commit(mapping, plan)
        await persist()
        notes.append(f"no response ({type(err).__name__}), result confirmed by reading again")
        resp = None
    else:
        resp_chapters = resp.get("chapters") if isinstance(resp, dict) else None
        commit(mapping_after_patch(plan, uploaded, resp_chapters), plan)
        await persist()

    parts = [f"{len(uploaded)} uploaded", f"{len(plan.removals)} removed"]
    if plan.excluded:
        parts.append(f"{len(plan.excluded)} deleted in the app skipped")
    parts += notes
    msg = ", ".join(parts) + f" ({plan.total_seconds / 60:.1f} of {plan.max_seconds / 60:.0f} min)"
    return Outcome(STATE_CURRENT, msg, len(uploaded), len(plan.removals))


async def _after_failed_patch(
    tonie: TonieApi, ts: dict[str, Any], final: list[dict[str, Any]], uploaded: dict[str, str]
) -> dict[str, dict[str, str]] | None:
    """PATCH failed: read again. Returns the mapping if it did arrive after all, otherwise None.

    If reading again fails too, "pending" stays stored and the next run resolves it.
    """
    pending = ts.get("pending") or {}
    try:
        chapters = read_chapters(await tonie.get())
    except Exception as err:  # noqa: BLE001
        raise SyncAbort(
            f"Write without response and reading again failed ({describe_error(err)}); "
            "will be resolved by the next sync"
        ) from err
    if uploaded:
        mapping = resolve_pending(pending, chapters)
    else:
        landed = [sorted(chapter_ids(c)) for c in chapters] == [sorted(chapter_ids(c)) for c in final]
        mapping = {k: dict(v) for k, v in (pending.get("mapping") or {}).items()} if landed else None
    if mapping is None:
        ts["pending"] = None
    return mapping
