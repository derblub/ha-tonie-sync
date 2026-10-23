"""Match the chapters of a Creative Tonie to Navidrome songs (for tonie_sync.adopt). No HA imports.

Conservative: "sure" only with the same core title, duration ±3 s, a matching artist (if the
chapter title contains one) and exactly one such candidate. Everything else stays foreign.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any

DURATION_SURE = 3.0  # seconds
DURATION_NEAR = 10.0

SURE = "sure"
UNSURE = "unsure"
NONE = "none"

# Suffixes that distinguish titles without being a different song ("fassung" = German "version")
_SUFFIX = re.compile(
    r"(single|radio|album|extended|original|short)?\s*(edit|version|mix|remaster(ed)?|fassung)(\s*\d{4})?"
)
_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_UMLAUTS = str.maketrans("", "", "äöüÄÖÜß")  # broken umlauts are missing entirely ("Glck") → drop on both sides


def loose(text: str) -> str:
    """Comparison form: lower case, umlauts and diacritics removed, only a-z0-9, single spaces."""
    t = (text or "").translate(_UMLAUTS)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("&", " ").replace("+", " ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def core_title(text: str) -> str:
    """Title without bracketed additions and "Single Edit" or similar."""
    t = _BRACKETS.sub(" ", text or "")
    parts = [p for p in re.split(r"\s+-\s+", t) if p.strip()]
    # drop "… - Single Edit" as the last part
    while len(parts) > 1 and _SUFFIX.fullmatch(loose(parts[-1]) or "x") is not None:
        parts.pop()
    t = " - ".join(parts)
    return loose(t)


def splits(title: str) -> list[tuple[str | None, str]]:
    """Possible splits "Artist - Title": (artist or None, title)."""
    parts = [p.strip() for p in re.split(r"\s+-\s+", title or "") if p.strip()]
    out: list[tuple[str | None, str]] = [(None, " - ".join(parts))]
    for i in range(1, len(parts)):
        out.append((" - ".join(parts[:i]), " - ".join(parts[i:])))
    return out


def queries(title: str) -> list[str]:
    """Search terms for search3, best first, at most three."""
    out: list[str] = []
    for artist, t in splits(title)[1:] + splits(title)[:1]:
        q = _BRACKETS.sub(" ", t)
        q = " - ".join(p for p in re.split(r"\s+-\s+", q) if _SUFFIX.fullmatch(loose(p) or "x") is None)
        q = re.sub(r"[^\w\s']+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    # fallback for broken umlauts in the title: longest word
    words = sorted(re.findall(r"\w{4,}", splits(title)[-1][1]), key=len, reverse=True)
    if words and words[0] not in out:
        out.append(words[0])
    return out[:3]


def _tokens(text: str) -> set[str]:
    stop = {"die", "der", "das", "und", "the", "and", "feat", "ft", "with", "mit"}
    return {w for w in loose(text).split() if w not in stop}


def artist_matches(chapter_artist: str, song: dict[str, Any]) -> bool:
    names = [str(song.get("artist") or ""), str(song.get("displayArtist") or "")]
    names += [str(a.get("name") or "") for a in song.get("artists") or [] if isinstance(a, dict)]
    want = _tokens(chapter_artist)
    if not want:
        return False
    have: set[str] = set()
    for n in names:
        have |= _tokens(n)
    if not have:
        return False
    common = want & have
    return len(common) >= max(1, round(0.6 * len(want))) or have <= want


@dataclass
class Candidate:
    song: dict[str, Any]
    title_exact: bool
    title_near: bool
    artist_ok: bool | None  # None = chapter title without artist
    diff: float | None  # deviation in seconds, None = unknown

    @property
    def sure(self) -> bool:
        return (
            self.title_exact
            and self.diff is not None
            and self.diff <= DURATION_SURE
            and self.artist_ok is not False
        )


@dataclass
class Match:
    position: int
    chapter_id: str
    title: str
    seconds: float
    confidence: str
    song: dict[str, Any] | None = None
    reason: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "position": self.position,
            "title": self.title,
            "seconds": round(self.seconds, 1),
            "confidence": self.confidence,
            "reason": self.reason,
        }
        if self.song:
            d["match"] = song_summary(self.song)
        if self.alternatives:
            d["candidates"] = self.alternatives
        return d


def song_summary(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": s.get("id"),
        "artist": s.get("artist"),
        "title": s.get("title"),
        "album": s.get("album"),
        "duration": s.get("duration"),
    }


def evaluate(title: str, seconds: float, song: dict[str, Any]) -> Candidate:
    st = core_title(str(song.get("title") or ""))
    try:
        dur = float(song.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    diff = abs(dur - seconds) if dur > 0 and seconds > 0 else None
    best = Candidate(song, False, False, None, diff)
    for artist, t in splits(title):
        ct = core_title(t)
        if not ct or not st:
            continue
        exact = ct == st
        near = not exact and min(len(ct), len(st)) >= 4 and (ct in st or st in ct)
        a_ok = None if artist is None else artist_matches(artist, song)
        cand = Candidate(song, exact, near, a_ok, diff)
        # ranking: exact + artist matches > exact without artist > exact with wrong artist > similar
        rank = (exact, a_ok is True, a_ok is None, near)
        brank = (best.title_exact, best.artist_ok is True, best.artist_ok is None, best.title_near)
        if rank > brank:
            best = cand
    return best


def _album_kind(song: dict[str, Any]) -> int:
    album = loose(str(song.get("album") or ""))
    if not album or album == "unknown album":
        return 2
    return 1 if album == "singles" else 0


def equivalent(songs: list[dict[str, Any]]) -> bool:
    """Same recording in several copies? Same core title, duration ±1 s and
    same artist or same (real) album."""
    titles = {core_title(str(s.get("title") or "")) for s in songs}
    durs = [float(s.get("duration") or 0) for s in songs]
    if len(titles) != 1 or min(durs) <= 0 or max(durs) - min(durs) > 1:
        return False
    artists = {loose(str(s.get("artist") or "")) for s in songs}
    albums = {loose(str(s.get("album") or "")) for s in songs}
    same_album = len(albums) == 1 and _album_kind(songs[0]) == 0
    return len(artists) == 1 or same_album


def _copy_rank(c: Candidate) -> tuple:
    """Preferred copy: real album before "Singles" before unknown, then matching artist, then ID."""
    return (_album_kind(c.song), c.artist_ok is not True, str(c.song.get("id")))


def decide(position: int, chapter: dict[str, Any], songs: list[dict[str, Any]]) -> Match:
    title = str(chapter.get("title") or "")
    try:
        seconds = float(chapter.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    cid = str(chapter.get("id") or chapter.get("file") or "")
    m = Match(position, cid, title, seconds, NONE)
    if not title:
        m.reason = "chapter without title"
        return m
    cands = [evaluate(title, seconds, s) for s in songs]
    relevant = [c for c in cands if c.title_exact or c.title_near]
    if not relevant:
        m.reason = "no song with a matching title found"
        return m
    sure = [c for c in relevant if c.sure]
    # the same song appearing several times in the results (e.g. different search terms) counts once
    uniq = {str(c.song.get("id")): c for c in sure}
    sure = list(uniq.values())
    if len(sure) > 1:
        with_artist = [c for c in sure if c.artist_ok is True]
        if len(with_artist) == 1:
            sure = with_artist
    copies = 0
    if len(sure) > 1 and equivalent([c.song for c in sure]):
        copies = len(sure)
        sure = [min(sure, key=lambda c: _copy_rank(c))]
    if len(sure) == 1:
        m.confidence = SURE
        m.song = sure[0].song
        m.reason = f"same title, duration ±{sure[0].diff:.1f} s" + (
            ", artist matches" if sure[0].artist_ok else ""
        ) + (f", {copies} equivalent copies in Navidrome" if copies else "")
        return m
    m.confidence = UNSURE
    relevant.sort(key=lambda c: (not c.title_exact, c.diff if c.diff is not None else 1e9))
    m.alternatives = [
        {**song_summary(c.song), "diff_s": None if c.diff is None else round(c.diff, 1)} for c in relevant[:3]
    ]
    if len(sure) > 1:
        m.reason = f"ambiguous: {len(sure)} equally good matches"
    else:
        c = relevant[0]
        if not c.title_exact:
            m.reason = "title only similar"
        elif c.diff is None:
            m.reason = "duration unknown"
        elif c.diff > DURATION_SURE:
            m.reason = f"duration differs by {c.diff:.0f} s"
        elif c.artist_ok is False:
            m.reason = "artist does not match"
        else:
            m.reason = "unsure"
    return m


async def match_chapters(
    chapters: list[dict[str, Any]], search: Callable[[str], Awaitable[list[dict[str, Any]]]]
) -> list[Match]:
    """Search and decide per chapter. search(query) → list of songs (search3)."""
    cache: dict[str, list[dict[str, Any]]] = {}
    out: list[Match] = []
    for pos, ch in enumerate(chapters, 1):
        found: dict[str, dict[str, Any]] = {}
        for q in queries(str(ch.get("title") or "")):
            if q not in cache:
                cache[q] = await search(q)
            for s in cache[q]:
                if s.get("id"):
                    found.setdefault(str(s["id"]), s)
            # stop early as soon as there is a sure match
            if decide(pos, ch, list(found.values())).confidence == SURE:
                break
        out.append(decide(pos, ch, list(found.values())))
    return out


def default_playlist_name(tonie_name: str) -> str:
    name = re.sub(r"^\s*(kreativ|creative)-?\s*tonie\s*", "", tonie_name or "", flags=re.IGNORECASE).strip()
    return f"Tonie {name or tonie_name}".strip()


def summary(matches: list[Match]) -> dict[str, int]:
    return {
        SURE: sum(m.confidence == SURE for m in matches),
        UNSURE: sum(m.confidence == UNSURE for m in matches),
        NONE: sum(m.confidence == NONE for m in matches),
    }
