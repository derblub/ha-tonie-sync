"""Adopt the existing chapters of a Tonie as a Navidrome playlist. No HA imports.

Flow: match chapters (matcher) → only sure matches in chapter order →
mapping song ↔ existing chapter. Nothing is written to the Tonie.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

try:
    from .matcher import SURE, Match, match_chapters, summary
    from .planner import MODE_APPEND, build_plan, songs_from_entries
except ImportError:  # tests
    from matcher import SURE, Match, match_chapters, summary
    from planner import MODE_APPEND, build_plan, songs_from_entries


class AdoptError(Exception):
    """Adopting is not possible; nothing changed (or the message says what already happened)."""


@dataclass
class AdoptPlan:
    matches: list[Match]
    chapters: list[dict[str, Any]]

    @property
    def sure(self) -> list[Match]:
        return [m for m in self.matches if m.confidence == SURE]

    @property
    def song_ids(self) -> list[str]:
        return [str(m.song["id"]) for m in self.sure if m.song]

    def mapping_for(self, entries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        """Mapping key → chapter from the actual playlist entries (order = sure matches)."""
        songs = songs_from_entries(entries)
        sure = self.sure
        if [s.song_id for s in songs] != [str(m.song["id"]) for m in sure if m.song]:
            raise AdoptError("Created playlist differs from the matches (order or count)")
        by_pos = {i: ch for i, ch in enumerate(self.chapters, 1)}
        out: dict[str, dict[str, str]] = {}
        for s, m in zip(songs, sure):
            ch = by_pos[m.position]
            out[s.key] = {
                "chapter_id": str(ch.get("id") or ch.get("file")),
                "file_id": str(ch.get("file") or ch.get("id")),
            }
        return out

    def check_unchanged(self, entries: list[dict[str, Any]]) -> bool:
        """Would a sync after adopting leave the Tonie unchanged?"""
        mapping = self.mapping_for(entries)
        plan = build_plan(self.chapters, songs_from_entries(entries), mapping, [], MODE_APPEND)
        return plan.unchanged

    def simulated_entries(self) -> list[dict[str, Any]]:
        return [dict(m.song) for m in self.sure if m.song]

    def report(self) -> dict[str, Any]:
        entries = self.simulated_entries()
        return {
            "chapters": len(self.chapters),
            "summary": summary(self.matches),
            "sync_after_adopt_unchanged": self.check_unchanged(entries) if entries else None,
            "result": [m.as_dict() for m in self.matches],
        }


async def plan_adopt(
    chapters: list[dict[str, Any]], search: Callable[[str], Awaitable[list[dict[str, Any]]]]
) -> AdoptPlan:
    return AdoptPlan(await match_chapters(chapters, search), chapters)
