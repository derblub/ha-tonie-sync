"""Adopting existing chapters as a playlist. All artists and titles are made up."""
import asyncio
import copy

import pytest

from adopt import AdoptError, plan_adopt
from engine import NO_CHANGE, run_sync

LIB = [
    {"id": "t1", "artist": "Mia & Ben, Clara Nord", "title": "Little Fox Song", "duration": 138, "album": "X"},
    {"id": "t2", "artist": "4Hamburg", "title": "Captain Bob from Boston", "duration": 135, "album": "Y"},
    {"id": "t3", "artist": "Lisa Sonnenfeld, Jörg Glück & Die Wald-Bären", "title": "Nachts im Wald",
     "duration": 184, "album": "Z"},
]


def ch(cid, title, seconds):
    return {"id": cid, "file": cid, "title": title, "seconds": seconds, "transcoding": False}


CHAPTERS = [
    ch("c1", "Mia & Ben, Clara Nord - Little Fox Song", 137.7),
    ch("c2", "Lights Out - For Mia", 156.9),  # not in the library → foreign
    ch("c3", "4Hamburg - Captain Bob from Boston", 135.3),
    ch("c4", "Lisa Sonnenfeld, Jrg Glck, Die Wald-Bren - Nachts im Wald", 184.0),
    ch("c5", "Mia & Ben, Clara Nord - Little Fox Song", 137.9),  # the same song twice
]


async def search(q):
    from matcher import loose
    return [s for s in LIB if loose(q) in loose(s["title"]) or loose(s["title"]) in loose(q)]


def test_adopt_then_sync_is_unchanged():
    plan = asyncio.run(plan_adopt(copy.deepcopy(CHAPTERS), search))
    assert [m.confidence for m in plan.matches] == ["sure", "none", "sure", "sure", "sure"]
    assert plan.song_ids == ["t1", "t2", "t3", "t1"]
    rep = plan.report()
    assert rep["summary"] == {"sure": 4, "unsure": 0, "none": 1}
    assert rep["sync_after_adopt_unchanged"] is True

    # This is how Navidrome creates the playlist (entries in chapter order)
    entries = [dict(s) for s in plan.simulated_entries()]
    mapping = plan.mapping_for(entries)
    assert mapping["t1"]["chapter_id"] == "c1" and mapping["t1#2"]["chapter_id"] == "c5"

    class Src:
        async def get_playlist(self, pid):
            return {"id": pid, "name": "Tonie X", "entry": entries}

        async def download_song(self, *a):
            raise AssertionError("must not download anything")

    class Tonie:
        patched = False

        async def get(self):
            return {"chapters": copy.deepcopy(CHAPTERS)}

        async def upload(self, *a):
            raise AssertionError("must not upload anything")

        async def patch(self, chapters):
            Tonie.patched = True

    ts = {"mapping": mapping, "excluded": []}

    async def persist():
        pass

    out = asyncio.run(run_sync(source=Src(), tonie=Tonie(), ts=ts, persist=persist, report=lambda *a, **k: None,
                               now=lambda: "now", playlist_id="p"))
    assert out.message.startswith(NO_CHANGE) and not Tonie.patched


def test_mapping_for_rejects_different_playlist():
    plan = asyncio.run(plan_adopt(copy.deepcopy(CHAPTERS), search))
    with pytest.raises(AdoptError):
        plan.mapping_for([LIB[1], LIB[0]])
