"""Pure sync logic: playlist + Tonie chapters + mapping → plan.

No Home Assistant imports, so the logic can be tested with plain pytest.

Terms:
- key: stable key of a playlist entry. First occurrence of a song = song ID,
  further occurrences of the same song = "<id>#2", "<id>#3" …
- mapping: key → {"chapter_id": …, "file_id": …} for chapters created by the sync.
- excluded: keys whose managed chapter the user deleted in the Tonie app.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re
from typing import Any

MAX_SECONDS = 5400  # 90 min
MAX_CHAPTERS = 99

# Removal threshold: more than this many chapters AND more than this share → only with confirmation
MASS_REMOVAL_MIN = 3
MASS_REMOVAL_SHARE = 0.5

MODE_APPEND = "append"  # playlist is added to foreign chapters
MODE_REPLACE = "replace"  # playlist replaces everything

_SUFFIX = re.compile(r"[a-z0-9]{1,5}")


def safe_suffix(value: object) -> str:
    """File extension from the server, limited to [a-z0-9]{1,5}; anything else becomes mp3."""
    text = str(value or "").strip().lower()
    return text if _SUFFIX.fullmatch(text) else "mp3"


@dataclass(frozen=True)
class Song:
    key: str
    song_id: str
    title: str
    artist: str
    duration: float
    suffix: str = "mp3"

    @property
    def chapter_title(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title}"
        return self.title


def songs_from_entries(entries: list[dict[str, Any]]) -> list[Song]:
    """Convert Subsonic playlist entries into songs with unique keys."""
    seen: dict[str, int] = {}
    songs: list[Song] = []
    for e in entries:
        sid = str(e.get("id") or "")
        if not sid:
            continue
        n = seen.get(sid, 0) + 1
        seen[sid] = n
        key = sid if n == 1 else f"{sid}#{n}"
        try:
            duration = float(e.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        songs.append(
            Song(
                key=key,
                song_id=sid,
                title=str(e.get("title") or sid),
                artist=str(e.get("artist") or ""),
                duration=duration,
                suffix=safe_suffix(e.get("suffix")),
            )
        )
    return songs


def fingerprint(songs: list[Song], mode: str) -> str:
    """Short hash over order and mode, for change detection."""
    h = hashlib.sha256(mode.encode())
    for s in songs:
        h.update(b"\0" + s.key.encode())
    return h.hexdigest()[:16]


def chapter_ids(ch: dict[str, Any]) -> set[str]:
    return {str(v) for v in (ch.get("id"), ch.get("file")) if v}


def _known_seconds(ch: dict[str, Any]) -> float | None:
    """Seconds of a chapter, or None if (still) unknown."""
    if ch.get("transcoding"):
        return None
    try:
        sec = float(ch.get("seconds") or 0)
    except (TypeError, ValueError):
        return None
    return sec if sec > 0 else None


@dataclass
class Slot:
    """One slot in the target list: existing chapter or new upload."""

    chapter: dict[str, Any] | None = None  # existing chapter (kept unchanged)
    song: Song | None = None  # to be uploaded
    managed_key: str | None = None  # set for managed chapters


@dataclass
class Plan:
    slots: list[Slot]
    uploads: list[Song]
    removals: list[dict[str, Any]]  # managed (or, in replace mode, foreign) chapters that are dropped
    newly_excluded: list[str]  # keys whose chapter was deleted in the app
    excluded_active: list[str]  # keys in the playlist that are missing because they are excluded
    mapping: dict[str, dict[str, str]]  # cleaned-up mapping (without new uploads)
    excluded: list[str]  # cleaned-up exclusion list
    total_seconds: float
    foreign_seconds: float
    playlist_seconds: float
    chapter_count: int
    unchanged: bool
    foreign_count: int = 0
    max_seconds: float = MAX_SECONDS
    problems: list[str] = field(default_factory=list)  # capacity exceeded
    unknown_duration: list[str] = field(default_factory=list)  # titles without a known duration
    mass_removal: bool = False  # empty playlist or large removal
    mapping_lost: bool = False  # not a single stored mapping can be found any more
    ambiguous: list[str] = field(default_factory=list)  # keys with several possible chapters
    foreign_ids: set[str] = field(default_factory=set)  # IDs of chapters that were foreign before

    @property
    def fits(self) -> bool:
        return not self.problems

    @property
    def over_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.max_seconds)

    @property
    def free_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.total_seconds)


def _ref_ids(ref: dict[str, Any]) -> list[str]:
    return [str(v) for v in (ref.get("chapter_id"), ref.get("file_id"), ref.get("upload_id")) if v]


def _contains_id(ch: dict[str, Any], ids: list[str]) -> bool:
    """Is a known ID contained in the chapter's id/file (e.g. file = "<prefix>_<uuid>")?"""
    own = [str(v) for v in (ch.get("id"), ch.get("file")) if v]
    return any(i in own or (len(i) >= 8 and any(i in o for o in own)) for i in ids)


def resolve_mapping(
    mapping: dict[str, dict[str, Any]], chapters: list[dict[str, Any]], known_foreign: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str], list[str]]:
    """Map the stored mapping onto the current chapters.

    Stages: 1) id/file exact, 2) known ID as part of id/file, 3) only for freshly
    uploaded ones ("pending"): exact title + position between the managed neighbours,
    unambiguous and never onto a chapter that was foreign before.
    Returns: key → chapter, updated mapping, missing keys, ambiguous keys.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for ch in chapters:
        for cid in chapter_ids(ch):
            by_id.setdefault(cid, ch)
    index = {id(ch): i for i, ch in enumerate(chapters)}
    found: dict[str, dict[str, Any]] = {}
    claimed: set[int] = set()

    def claim(key: str, ch: dict[str, Any]) -> None:
        found[key] = ch
        claimed.add(id(ch))

    for key, ref in mapping.items():  # stage 1
        for cid in _ref_ids(ref):
            ch = by_id.get(cid)
            if ch is not None and id(ch) not in claimed:
                claim(key, ch)
                break
    for key, ref in mapping.items():  # stage 2
        if key in found:
            continue
        cands = [c for c in chapters if id(c) not in claimed and _contains_id(c, _ref_ids(ref))]
        if len(cands) == 1:
            claim(key, cands[0])
    ambiguous: list[str] = []
    for key, ref in mapping.items():  # stage 3
        if key in found or not ref.get("pending") or not ref.get("title"):
            continue
        lo, hi = -1, len(chapters)
        if (a := ref.get("after")) in found:
            lo = index[id(found[a])]
        if (b := ref.get("before")) in found:
            hi = index[id(found[b])]
        cands = [
            c for c in chapters[lo + 1:hi]
            if id(c) not in claimed and c.get("title") == ref["title"] and not (chapter_ids(c) & known_foreign)
        ]
        if len(cands) == 1:
            claim(key, cands[0])
        elif len(cands) > 1:
            ambiguous.append(key)

    new_mapping: dict[str, dict[str, Any]] = {}
    for key, ch in found.items():
        ref = dict(mapping[key])
        cid = str(ch.get("id") or ch.get("file") or ref.get("chapter_id"))
        ref["chapter_id"] = cid
        ref["file_id"] = str(ch.get("file") or cid)
        if ref.get("pending") and not ch.get("transcoding") and cid != ref.get("upload_id"):
            ref["pending"] = False  # cloud rewrote it and is done: identity is settled
        new_mapping[key] = ref
    missing = [k for k in mapping if k not in found and k not in ambiguous]
    return found, new_mapping, missing, ambiguous


def build_plan(
    chapters: list[dict[str, Any]],
    songs: list[Song],
    mapping: dict[str, dict[str, str]],
    excluded: list[str],
    mode: str = MODE_APPEND,
    restore_deleted: bool = False,
    max_seconds: float = MAX_SECONDS,
    max_chapters: int = MAX_CHAPTERS,
    known_foreign: set[str] | None = None,
) -> Plan:
    """Compute the plan. Does not modify its inputs.

    known_foreign: chapter IDs that were foreign at the last sync (never map them as managed).
    """
    playlist_keys = {s.key for s in songs}
    managed_chapter, new_mapping, missing, ambiguous = resolve_mapping(mapping, chapters, known_foreign or set())
    managed_obj_ids: set[int] = {id(ch) for ch in managed_chapter.values()}

    # Several mappings and not a single one found: more likely a cloud ID change than deletion in the app
    mapping_lost = len(mapping) >= 2 and not managed_chapter and bool(chapters)

    # Chapter missing on the Tonie: deleted by the user in the app → exclude
    newly_excluded = [] if restore_deleted else [k for k in missing if k in playlist_keys]
    excl = set() if restore_deleted else {k for k in excluded if k in playlist_keys}
    excl.update(newly_excluded)

    foreign = [ch for ch in chapters if id(ch) not in managed_obj_ids]
    foreign_ids: set[str] = set()
    for ch in foreign:
        foreign_ids |= chapter_ids(ch)
    removals: list[dict[str, Any]] = []
    slots: list[Slot] = []
    unknown: list[str] = []

    # Append mode: foreign chapters are anchored to the preceding remaining managed
    # chapter (or to the start) and so keep their position relative to it.
    anchored: dict[str | None, list[dict[str, Any]]] = {}
    if mode == MODE_REPLACE:
        removals.extend(foreign)
    else:
        key_of = {id(ch): k for k, ch in managed_chapter.items() if k in playlist_keys}
        anchor: str | None = None
        for ch in chapters:
            if id(ch) in key_of:
                anchor = key_of[id(ch)]
            elif id(ch) not in managed_obj_ids:
                anchored.setdefault(anchor, []).append(ch)
        slots.extend(Slot(chapter=ch) for ch in anchored.get(None, []))

    uploads: list[Song] = []
    excluded_active: list[str] = []
    playlist_seconds = 0.0
    for s in songs:
        if s.key in managed_chapter:
            ch = managed_chapter[s.key]
            slots.append(Slot(chapter=ch, managed_key=s.key))
            sec = _known_seconds(ch) or (s.duration if s.duration > 0 else None)
        elif s.key in excl:
            excluded_active.append(s.key)
            continue
        else:
            uploads.append(s)
            slots.append(Slot(song=s, managed_key=s.key))
            sec = s.duration if s.duration > 0 else None
        if sec is None:
            unknown.append(s.chapter_title)
        else:
            playlist_seconds += sec
        slots.extend(Slot(chapter=ch) for ch in anchored.get(s.key, []))

    for key, ch in managed_chapter.items():
        if key not in playlist_keys:
            removals.append(ch)
            new_mapping.pop(key, None)

    foreign_seconds = 0.0
    if mode != MODE_REPLACE:
        for ch in foreign:
            sec = _known_seconds(ch)
            if sec is None:
                unknown.append(str(ch.get("title") or ch.get("id") or "?"))
            else:
                foreign_seconds += sec
    total = foreign_seconds + playlist_seconds
    count = len(slots)

    problems: list[str] = []
    if total > max_seconds:
        problems.append(f"{math.ceil((total - max_seconds) / 60)} min too long")
    if count > max_chapters:
        problems.append(f"{count - max_chapters} chapter(s) too many")

    n_removed = len(removals)
    mass_removal = (not songs and n_removed > 0) or (
        n_removed > MASS_REMOVAL_MIN and n_removed > MASS_REMOVAL_SHARE * len(chapters)
    )

    target_existing = [s.chapter for s in slots]
    unchanged = not uploads and len(target_existing) == len(chapters) and all(
        a is b for a, b in zip(target_existing, chapters)
    )

    return Plan(
        slots=slots,
        uploads=uploads,
        removals=removals,
        newly_excluded=newly_excluded,
        excluded_active=excluded_active,
        mapping=new_mapping,
        excluded=sorted(excl),
        total_seconds=total,
        foreign_seconds=foreign_seconds,
        playlist_seconds=playlist_seconds,
        chapter_count=count,
        unchanged=unchanged,
        foreign_count=0 if mode == MODE_REPLACE else len(foreign),
        max_seconds=max_seconds,
        problems=problems,
        unknown_duration=unknown,
        mass_removal=mass_removal,
        mapping_lost=mapping_lost,
        foreign_ids=foreign_ids,
        ambiguous=ambiguous,
    )


def new_chapter(file_id: str, title: str) -> dict[str, Any]:
    return {"id": file_id, "file": file_id, "title": title, "seconds": 0, "transcoding": False}


def assemble(plan: Plan, uploaded: dict[str, str]) -> list[dict[str, Any]]:
    """Final chapter list for the single PATCH. uploaded: key → file_id."""
    out: list[dict[str, Any]] = []
    for slot in plan.slots:
        if slot.chapter is not None:
            out.append(slot.chapter)
        else:
            assert slot.song is not None
            out.append(new_chapter(uploaded[slot.song.key], slot.song.chapter_title))
    return out


def new_refs(plan: Plan, uploaded: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Mapping including freshly uploaded chapters (as "pending" with title and neighbours)."""
    mapping = {k: dict(v) for k, v in plan.mapping.items()}
    managed = [s for s in plan.slots if s.managed_key]
    for i, slot in enumerate(managed):
        if slot.song is None or slot.managed_key not in uploaded:
            continue
        fid = uploaded[slot.managed_key]
        mapping[slot.managed_key] = {
            "chapter_id": fid,
            "file_id": fid,
            "upload_id": fid,
            "title": slot.song.chapter_title,
            "pending": True,
            "after": managed[i - 1].managed_key if i > 0 else None,
            "before": managed[i + 1].managed_key if i + 1 < len(managed) else None,
        }
    return mapping


def mapping_after_patch(
    plan: Plan, uploaded: dict[str, str], result_chapters: list[dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Mapping after the PATCH; takes over rewritten IDs from the response if unambiguous."""
    mapping = new_refs(plan, uploaded)
    if not result_chapters:
        return mapping
    _, resolved, _, _ = resolve_mapping(mapping, result_chapters, plan.foreign_ids)
    mapping.update(resolved)
    return mapping


def resolve_pending(
    pending: dict[str, Any], chapters: list[dict[str, Any]]
) -> dict[str, dict[str, Any]] | None:
    """Resolve an interrupted PATCH.

    pending = {"mapping": full mapping including new uploads, "uploaded": key → file_id}.
    If at least one uploaded file is on the Tonie (id/file or part of it), the PATCH
    arrived (it is atomic) → return the mapping, otherwise None.
    """
    uploaded: dict[str, str] = pending.get("uploaded") or {}
    if not uploaded:
        return None
    fids = list(uploaded.values())
    if not any(_contains_id(ch, fids) for ch in chapters):
        return None
    return {k: dict(v) for k, v in (pending.get("mapping") or {}).items()}
